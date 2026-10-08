# -*- coding: utf-8 -*-
"""Batch-set the brand on existing POP products via verified full-edit schema.

Proven live 2026-10-03 on jianxitang (116/116) + putuo-huacang (41/42; the
single remaining item was blocked by platform activity protection).

Hard-won invariants (do not "simplify" these away):
1. Atomic PATCH /rest/sp-product/v0/product-applies/{applyId} REJECTS the
   WareBrand component; the only supported path is full-edit
   POST /rest/sp-product/v0/product-applies.
2. sale-prop mappingId MUST be the attrValues binding value. apply-detail
   returns mappingId="" and submitting it empty makes the platform DELETE all
   color-specific image groups from material.mainImages (live incident).
3. Every SKU component needs SkuUUid = str(skuId) in addition to SkuId.
4. Existing-product full edits must NOT submit SkuStockNum (stock has its own
   dedicated API).
5. Image entries must carry isPrimary/isGgt and the original orderSort.
6. Sale attribute aliases containing "xxx:" prefixes are rejected with
   1010300506; strip the prefix before submitting and record the change.
7. Products enrolled in platform activities (e.g. 便宜包邮) reject ALL edits
   with 1010300506; classify as activity-blocked, retry only after the activity
   ends.
8. Transient failures (SSL EOF, WinError 32 file lock, "系统异常!更新SKU维度
   价格验证失败") are retried with backoff; a failed submit changes nothing,
   so retries are idempotent.
9. Setting a brand makes the platform prepend the brand name to product and
   SKU titles. This is expected platform behavior, not a defect.

Usage:
  python set_product_brand.py --profile jianxitang --shop-name 见喜堂装饰画店 \
      --brand-id 1886203 --brand-name 鸿运鸟 [--run-dir ./brand-run]
  Modes: default = full run; --dry-run = validate only (no submit);
         --audit-only = verify current brand/images without editing;
         --product-ids id1,id2 = restrict scope; --limit N = first N products.

Outputs in --run-dir: before_<pid>.json backups, ledger.jsonl (resumable),
failures.json, and (audit mode) final_audit.json.
"""
from __future__ import annotations
import argparse
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import jdo_listing_mvp as cli  # noqa: E402


# ---------------------------------------------------------------------------
# Verified full-edit converter (keep in sync with the invariants above).
# ---------------------------------------------------------------------------

def prop(key: str, value: Any) -> dict:
    return {"key": key, "value": value}


def feature_map(holder: dict) -> dict[str, str]:
    return {str(f.get("key")): str(f.get("value")) for f in (holder.get("features") or [])}


def image_groups(material: dict) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for group in material.get("mainImages") or []:
        uuid = str(group.get("uuid") or "0000000000")
        images = sorted(group.get("imageInfoList") or [], key=lambda x: int(x.get("orderSort") or 0))
        groups.setdefault(uuid, []).extend(images)
    return groups


def order_sort_or_position(image: dict, position: int) -> int:
    try:
        return int(image.get("orderSort"))
    except (TypeError, ValueError):
        return position


def spu_image_entries(images: list[dict], color_id: str) -> list[dict]:
    entries = []
    for position, image in enumerate(images, 1):
        order_sort = order_sort_or_position(image, position)
        entries.append({
            "componentCode": "img",
            "properties": [
                prop("imgRefId", color_id),
                prop("colorId", color_id),
                prop("orderSort", order_sort),
                prop("imgUrl", str(image.get("imgUrl") or "")),
                prop("isPrimary", int(bool(image.get("primaryFlag")))),
                prop("isGgt", 1 if order_sort == 1 else 0),
            ],
        })
    return entries


def sku_image_entries(images: list[dict], color_id: str) -> list[dict]:
    entries = []
    for position, image in enumerate(images, 1):
        order_sort = order_sort_or_position(image, position)
        entries.append({
            "componentCode": "img",
            "properties": [
                prop("imgUrl", str(image.get("imgUrl") or "")),
                prop("isGgt", 1 if order_sort == 1 else 0),
                prop("isPrimary", int(bool(image.get("primaryFlag")))),
                prop("imgRefId", color_id),
                prop("colorId", color_id),
                prop("orderSort", order_sort),
            ],
        })
    return entries


def primary_sale_value(sku: dict) -> str:
    best = None
    for attr in sku.get("saleAttrs") or []:
        if int(attr.get("indexId") or attr.get("dim") or 0) == 1:
            best = attr
            break
    if best is None and sku.get("saleAttrs"):
        best = sorted(sku["saleAttrs"], key=lambda a: int(a.get("indexId") or a.get("dim") or 99))[0]
    return str((best or {}).get("attrValues") or "") or "0000000000"


def attr_values_ids(attr: dict) -> list[str]:
    return [str(v.get("attrValue")) for v in (attr.get("values") or []) if v.get("attrValue")]


def attr_extra_props(attr: dict) -> list[dict]:
    props = []
    values = attr.get("values") or []
    if any(v.get("expand") for v in values):
        props.append(prop("expands", ",".join(str(v.get("expand") or "") for v in values)))
    if any(v.get("unit") for v in values):
        props.append(prop("units", ",".join(str(v.get("unit") or "") for v in values)))
    return props


def vender_category_values(sku: dict) -> list[dict]:
    values = []
    for attr in sku.get("saleAttrs") or []:
        index = str(attr.get("indexId") or attr.get("dim") or "")
        values.append({
            "componentCode": f"sale-prop-{attr.get('attrId')}",
            "properties": [
                prop("attrValueAlias", f"[{attr.get('attrValueAlias')}]"),
                prop("attrName", str(attr.get("attrName") or "")),
                prop("mappingId", str(attr.get("attrValues") or "")),  # invariant 2
                prop("index", index),
                prop("attrValuesSeqNo", int(attr.get("attrValuesSeqNo") or 1)),
            ],
        })
    return values


def category_group_list(product_info: dict) -> dict | None:
    values = []
    for attr in product_info.get("goodsAttrInfos") or []:
        ids = attr_values_ids(attr)
        if not ids:
            continue
        values.append({
            "componentCode": f"prop-product-{attr.get('attrId')}",
            "properties": [prop("attrValues", ",".join(ids))] + attr_extra_props(attr),
        })
    return {"componentCode": "CategoryGroupList", "value": values} if values else None


def sku_attr_component(sku: dict) -> dict | None:
    values = []
    for attr in sku.get("goodsAttrInfos") or []:
        ids = attr_values_ids(attr)
        if not ids:
            continue
        values.append({
            "componentCode": f"prop-sku-{attr.get('attrId')}",
            "properties": [prop("attrValues", f"[{','.join(ids)}]")] + attr_extra_props(attr),
        })
    return {"componentCode": "SkuAttr", "value": values} if values else None


def normalize_colon_aliases(apply: dict) -> list[list[str]]:
    """Invariant 6: strip 'xxx:' prefixes; return [[old, new], ...]."""
    changed = []
    for sku in apply.get("skuList") or []:
        for attr in sku.get("saleAttrs") or []:
            alias = str(attr.get("attrValueAlias") or "")
            if ":" in alias:
                new = alias.split(":", 1)[1]
                attr["attrValueAlias"] = new
                changed.append([alias, new])
    return changed


def build_edit_schema(apply: dict, brand_id: int) -> list[dict]:
    product_info = apply.get("productInfo") or {}
    material = apply.get("material") or {}
    features = feature_map(product_info)
    logistics = product_info.get("logisticsInfo") or {}
    groups = image_groups(material)
    shared_images = groups.get("0000000000") or []
    if not shared_images:
        for uuid_key in sorted(groups):
            shared_images.extend(groups[uuid_key])

    components: list[dict] = [
        {"componentCode": "WareTitle", "value": str(product_info.get("productName") or "")},
        {"componentCode": "WareBrand", "value": brand_id},
    ]
    item_num = str(product_info.get("itemNum") or "").strip()
    if item_num:
        components.append({"componentCode": "WareItemnum", "value": item_num})
    if shared_images:
        components.append({"componentCode": "WareSkuImg", "value": spu_image_entries(shared_images, "0000000000")})
    desc = str(((product_info.get("productDetailDesc") or {}).get("desc")) or "")
    if desc:
        components.append({
            "componentCode": "WareEditor",
            "value": [{"componentCode": "detail", "properties": [
                prop("desc", desc), prop("zhuangBaUseFlag", "0"), prop("userAgent", "pc"),
            ]}],
        })
    if "is7ToReturn" in features:
        components.append({"componentCode": "WareSevenDayReturn", "value": features["is7ToReturn"]})
    if logistics.get("promiseId"):
        components.append({"componentCode": "WarePromise", "value": str(logistics["promiseId"])})
    if logistics.get("transportId"):
        components.append({"componentCode": "WareTransport", "value": str(logistics["transportId"])})
    if logistics.get("delivery"):
        components.append({"componentCode": "WareShopAddress", "value": str(logistics["delivery"])})
    if "isPayFirst" in features:
        components.append({"componentCode": "PayMethodLimit", "properties": [prop("isPayFirst", features["isPayFirst"])]})
    category_group = category_group_list(product_info)
    if category_group:
        components.append(category_group)
    rectangle_images = []
    for image in material.get("rectangleImages") or []:
        rectangle_images.extend(sorted(image.get("imageInfoList") or [image], key=lambda x: int(x.get("orderSort") or 0)))
    if rectangle_images:
        components.append({"componentCode": "LongImg", "value": spu_image_entries(rectangle_images, "0000000000")})

    sku_components = []
    for sku in apply.get("skuList") or []:
        sku_id = int(sku["skuId"])
        values: list[dict] = [
            {"componentCode": "SkuUUid", "value": str(sku_id)},  # invariant 3
            {"componentCode": "SkuId", "value": sku_id},
            {"componentCode": "SkuJdprice", "value": str((sku.get("priceInfo") or {}).get("jdPrice"))},
        ]
        outer_id = str(sku.get("outerId") or "").strip()
        if outer_id:
            values.append({"componentCode": "SkuVendorSku", "value": outer_id})
        values.append({"componentCode": "SkuEnableStatus", "value": str(sku.get("skuEnableStatus") or 1)})
        binding = primary_sale_value(sku)
        gallery = groups.get(binding) or shared_images
        values.append({"componentCode": "SkuMainImage", "value": sku_image_entries(gallery, binding)})
        sku_features = feature_map(sku)
        if sku_features.get("shortTitle"):
            values.append({"componentCode": "SkuShortTitle", "value": sku_features["shortTitle"]})
        upc = str(sku.get("upcCode") or "").strip()
        if upc:
            values.append({"componentCode": "SkuWareUpccode", "value": upc})
        sku_promise = str(((sku.get("logistics") or {}).get("promiseId")) or "")
        if sku_promise:
            values.append({"componentCode": "SkuPromise", "value": sku_promise})
        sku_attr = sku_attr_component(sku)
        if sku_attr:
            values.append(sku_attr)
        sale_values = vender_category_values(sku)
        if sale_values:
            values.append({"componentCode": "VenderCategoryValue", "value": sale_values})
        sku_components.append({"componentCode": "SKU", "value": values})
    if not sku_components:
        raise ValueError("apply has no skuList")
    components.append({"componentCode": "SKUS", "value": sku_components})  # invariant 4: no SkuStockNum
    return components


def build_edit_body(apply: dict, brand_id: int) -> dict:
    product_info = apply.get("productInfo") or {}
    category = product_info.get("categoryDetail") or {}
    last_category_id = int(category.get("lastCategoryId") or 0)
    product_id = int(product_info.get("productId") or 0)
    if not last_category_id:
        raise ValueError("missing lastCategoryId")
    if not product_id:
        raise ValueError("missing productId")
    schema = build_edit_schema(apply, brand_id)
    return {
        "scene": "pop",
        "schema": json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "productApplyDTO": {
            "productId": product_id,
            "saveActionType": 0,
            "categoryDTO": {"lastCategoryId": last_category_id},
        },
    }


# ---------------------------------------------------------------------------
# Batch pipeline.
# ---------------------------------------------------------------------------

TRANSIENT_MARKERS = ("SSL", "WinError 32", "非 JSON")
ACTIVITY_MARKER = "便宜包邮"
PRICE_RETRY_MARKER = "更新SKU维度价格验证失败"
BRAND_NAME_ARG = [""]  # populated from --brand-name after argparse


def is_transient(message: str) -> bool:
    return any(marker in message for marker in TRANSIENT_MARKERS) or PRICE_RETRY_MARKER in message


def with_retries(action, attempts: int, base_delay: float, label: str):
    last = None
    for attempt in range(attempts):
        try:
            return action()
        except Exception as exc:
            last = exc
            if attempt == attempts - 1 or not is_transient(str(exc)):
                raise
            print(f"    {label} transient ({str(exc)[:100]}), retry {attempt + 1}/{attempts - 1}", flush=True)
            time.sleep(base_delay * (attempt + 1))
    raise last


def verify_identity(profile: str, expected_shop: str) -> dict:
    cli.configure_jdo_profile(profile)
    data = with_retries(
        lambda: cli.get("jdo-seller-info", "/rest/sp-seller/v0/venderinfo", {"scopeSet": "baseInfo"}),
        4, 5, "seller-info",
    ).get("data") or {}
    shop = str((data.get("shopBaseInfo") or {}).get("shopName") or "").strip()
    vender_id = int(data.get("venderId") or 0)
    if shop != expected_shop or vender_id <= 0:
        raise RuntimeError(f"identity mismatch: profile={profile} venderId={vender_id} shop={shop} expected={expected_shop}")
    cli.mark_jdo_store_verified(profile, vender_id, shop)
    return {"profile": profile, "venderId": vender_id, "shopName": shop}


def check_brand_available(category_id: int, brand_id: int) -> None:
    """Preflight via brandName search; the full list has 2000+ entries, so
    page-scanning is impractical. brandName + queryType=2 returns exact matches."""
    result = with_retries(
        lambda: cli.get("jdo-seller-info", "/rest/sp-seller/v0/vender-brands",
                        {"categoryId": category_id, "queryType": 2, "brandName": BRAND_NAME_ARG[0]}),
        4, 5, "vender-brands",
    )
    data = result.get("data")
    brands = data if isinstance(data, list) else ((data or {}).get("brandList") or (data or {}).get("list") or [])
    for brand in brands:
        if int(brand.get("brandId") or 0) == brand_id:
            print(f"brand preflight ok: {brand_id} {brand.get('brandName')}")
            return
    raise RuntimeError(f"brand {brand_id} not selectable for category {category_id}: {json.dumps(result, ensure_ascii=False)[:300]}")


def list_all_products() -> list[dict]:
    products, seen, page = [], set(), 1
    while True:
        def fetch():
            return cli.get("jdo-product-status", "/rest/sp-product/v0/products", {
                "scopeSet": ["productId", "productName", "productStatus", "brandId"],
                "page": page, "pageSize": 50,
            })
        result = with_retries(fetch, 5, 5, f"list page {page}")
        data = result.get("data")
        batch = data if isinstance(data, list) else ((data or {}).get("productList") or [])
        if not batch:
            break
        added = 0
        for item in batch:
            pid = int(item["productId"])
            if pid not in seen:
                seen.add(pid)
                products.append(item)
                added += 1
        total = int(((data or {}).get("paginationData") or {}).get("totalItems") or 0) if not isinstance(data, list) else 0
        if (total and len(products) >= total) or added == 0 or page >= 200:
            break
        page += 1
    return products


def apply_detail(product_id: int) -> dict:
    def fetch():
        return cli.get("jdo-product-update", "/rest/sp-product/v0/product-apply/{applyId}",
                       {"applyId": product_id, "scene": "pop"})
    return with_retries(fetch, 4, 5, f"apply-detail {product_id}").get("data") or {}


def group_map(detail: dict) -> dict[str, list[str]]:
    out = {}
    for group in ((detail.get("material") or {}).get("mainImages") or []):
        out[str(group.get("uuid"))] = [str(i.get("imgUrl")) for i in (group.get("imageInfoList") or [])]
    return out


def verify_preservation(before: dict, after: dict, brand_id: int) -> list[str]:
    problems = []
    before_groups, after_groups = group_map(before), group_map(after)
    if set(before_groups) != set(after_groups):
        problems.append(f"image groups changed: {sorted(before_groups)} -> {sorted(after_groups)}")
    else:
        for uuid, urls in before_groups.items():
            if urls != after_groups.get(uuid):
                problems.append(f"image group {uuid} urls changed")
    brand_after = ((after.get("productInfo") or {}).get("brandInfo") or {})
    if int(brand_after.get("brandId") or 0) != brand_id:
        problems.append(f"brand not set: {brand_after}")
    before_skus = {str(s.get("skuId")): s for s in (before.get("skuList") or [])}
    after_skus = {str(s.get("skuId")): s for s in (after.get("skuList") or [])}
    if set(before_skus) != set(after_skus):
        problems.append("sku set changed")
    for sku_id, sku in before_skus.items():
        other = after_skus.get(sku_id)
        if not other:
            continue
        if (sku.get("priceInfo") or {}).get("jdPrice") != (other.get("priceInfo") or {}).get("jdPrice"):
            problems.append(f"sku {sku_id} price changed")
        if sorted(str(a.get("attrValueAlias")) for a in (sku.get("saleAttrs") or [])) != \
           sorted(str(a.get("attrValueAlias")) for a in (other.get("saleAttrs") or [])):
            problems.append(f"sku {sku_id} sale alias changed")
    before_desc = str(((before.get("productInfo") or {}).get("productDetailDesc") or {}).get("desc") or "")
    after_desc = str(((after.get("productInfo") or {}).get("productDetailDesc") or {}).get("desc") or "")
    if before_desc != after_desc:
        problems.append("detail desc changed")
    if len((before.get("material") or {}).get("whiteBackGroundImages") or []) != \
       len((after.get("material") or {}).get("whiteBackGroundImages") or []):
        problems.append("white background image count changed")
    return problems


def load_ledger_ok(ledger: Path) -> set[int]:
    done = set()
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("ok"):
                done.add(int(record["productId"]))
    return done


def process_product(profile: str, product_id: int, brand_id: int, run_dir: Path, dry_run: bool) -> dict:
    record = {"profile": profile, "productId": product_id}
    before = apply_detail(product_id)
    (run_dir / f"before_{product_id}.json").write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")
    current_brand = int(((before.get("productInfo") or {}).get("brandInfo") or {}).get("brandId") or -1)
    if current_brand == brand_id:
        record.update(ok=True, skipped="already-branded")
        return record
    if current_brand not in (-1, 0):
        record.update(ok=False, error=f"existing brand {current_brand} differs; refusing overwrite")
        return record

    normalized = normalize_colon_aliases(before)
    if normalized:
        record["aliasNormalized"] = normalized
    body = build_edit_body(before, brand_id)
    body_path = run_dir / f"body_{product_id}.json"
    body_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    try:
        if dry_run:
            val_body = dict(body)
            val_body["imgUrlList"] = []
            val_path = run_dir / f"validate_{product_id}.json"
            val_path.write_text(json.dumps(val_body, ensure_ascii=False), encoding="utf-8")
            result = with_retries(
                lambda: cli.run_jdo_large(url="/rest/sp-product/v0/product-schemas/validations", method="POST",
                                         body_path=val_path, skill_id="jdo-product-publish", yes=True),
                3, 5, f"validate {product_id}",
            )
            val_path.unlink(missing_ok=True)
            record.update(ok=True, skipped="dry-run", validation=result.get("success"))
            return record
        result = with_retries(
            lambda: cli.run_jdo_large(url="/rest/sp-product/v0/product-applies", method="POST",
                                      body_path=body_path, skill_id="jdo-product-publish", yes=True),
            4, 10, f"submit {product_id}",
        )
        record["submit"] = result.get("data")
    finally:
        body_path.unlink(missing_ok=True)

    after = apply_detail(product_id)
    (run_dir / f"after_{product_id}.json").write_text(json.dumps(after, ensure_ascii=False), encoding="utf-8")
    problems = verify_preservation(before, after, brand_id)
    if problems:
        record.update(ok=False, error="; ".join(problems)[:500])
    else:
        brand_after = ((after.get("productInfo") or {}).get("brandInfo") or {})
        record.update(ok=True, brandName=brand_after.get("brandName"), imageGroups=len(group_map(after)))
    return record


def classify_failure(message: str) -> str:
    if ACTIVITY_MARKER in message:
        return "activity-blocked"
    if "1010300506" in message and "销售属性值名称必填" in message:
        return "sale-attr-name-rejected"
    if is_transient(message):
        return "transient"
    return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-set brand on existing POP products (verified full-edit).")
    parser.add_argument("--profile", required=True, help="store-exclusive jdo-cli profile")
    parser.add_argument("--shop-name", required=True, help="expected shop name for identity verification")
    parser.add_argument("--brand-id", type=int, required=True)
    parser.add_argument("--brand-name", required=False, default="", help="brand name; required for preflight in run/dry-run modes")
    parser.add_argument("--product-ids", default="", help="comma-separated productId filter")
    parser.add_argument("--run-dir", default=None, help="output dir; default <cwd>/brand-run-<brand_id>")
    parser.add_argument("--dry-run", action="store_true", help="validate bodies only, no submit")
    parser.add_argument("--audit-only", action="store_true", help="verify brand/images without editing")
    parser.add_argument("--limit", type=int, default=0, help="process only first N products")
    args = parser.parse_args()

    identity = verify_identity(args.profile, args.shop_name)
    print(f"[identity] {identity['shopName']} venderId={identity['venderId']}", flush=True)

    run_dir = Path(args.run_dir) if args.run_dir else Path.cwd() / f"brand-run-{args.brand_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger = run_dir / "ledger.jsonl"

    wanted = {int(x) for x in args.product_ids.split(",") if x.strip().isdigit()} if args.product_ids else None
    if wanted is not None:
        # Scoped mode: the paginated list endpoint is unstable (may return only
        # the first ~89 items); build the scope directly from product IDs.
        products = [{"productId": pid, "productName": ""} for pid in sorted(wanted)]
    else:
        products = list_all_products()
    if args.limit:
        products = products[:args.limit]
    print(f"[scope] products={len(products)} mode={'audit' if args.audit_only else 'dry-run' if args.dry_run else 'full'}", flush=True)

    if not args.audit_only and products:
        if not args.brand_name.strip():
            raise SystemExit("--brand-name is required for preflight in run/dry-run modes")
        BRAND_NAME_ARG[0] = args.brand_name.strip()
        first_detail = apply_detail(int(products[0]["productId"]))
        category_id = int(((first_detail.get("productInfo") or {}).get("categoryDetail") or {}).get("lastCategoryId") or 0)
        if category_id:
            check_brand_available(category_id, args.brand_id)

    done = load_ledger_ok(ledger) if not args.audit_only else set()
    if args.audit_only:
        report = {"total": len(products), "branded": 0, "imagesIntact": 0, "problems": []}
        for index, product in enumerate(products, 1):
            pid = int(product["productId"])
            try:
                current = apply_detail(pid)
            except Exception as exc:
                report["problems"].append({"productId": pid, "error": f"fetch failed: {str(exc)[:120]}"})
                continue
            if int((((current.get("productInfo") or {}).get("brandInfo") or {}).get("brandId") or 0)) == args.brand_id:
                report["branded"] += 1
            backup = run_dir / f"before_{pid}.json"
            if backup.exists():
                before = json.loads(backup.read_text(encoding="utf-8"))
                problems = verify_preservation(before, current, args.brand_id)
                if problems:
                    report["problems"].append({"productId": pid, "error": "; ".join(problems)[:300]})
                else:
                    report["imagesIntact"] += 1
            if index % 20 == 0:
                print(f"  [audit] {index}/{len(products)} branded={report['branded']} problems={len(report['problems'])}", flush=True)
            time.sleep(0.2)
        (run_dir / "final_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[audit] total={report['total']} branded={report['branded']} imagesIntact={report['imagesIntact']} problems={len(report['problems'])}")
        for item in report["problems"]:
            print("   PROBLEM", item["productId"], item["error"][:150])
        return

    log = io.open(ledger, "a", encoding="utf-8")
    ok = fail = skip = 0
    failures = []
    for index, product in enumerate(products, 1):
        pid = int(product["productId"])
        if pid in done:
            skip += 1
            continue
        try:
            record = process_product(args.profile, pid, args.brand_id, run_dir, args.dry_run)
        except Exception as exc:
            message = str(exc)
            record = {"profile": args.profile, "productId": pid, "ok": False, "error": message[:400],
                      "class": classify_failure(message)}
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.flush()
        if record.get("ok"):
            ok += 1
            mark = "OK" + (f" ({record.get('skipped')})" if record.get("skipped") else "")
        else:
            fail += 1
            failures.append(record)
            mark = f"FAIL[{record.get('class', 'unknown')}]"
        print(f"  [{index}/{len(products)}] {mark} {pid} {str(product.get('productName'))[:28]} {record.get('error', '')}", flush=True)
        time.sleep(0.4)
    log.close()
    (run_dir / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"SUMMARY ok={ok} fail={fail} skip={skip} total={len(products)} failures={len(failures)} -> {run_dir / 'failures.json'}")


if __name__ == "__main__":
    main()
