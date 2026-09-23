from __future__ import annotations

import argparse
import copy
import contextlib
import hashlib
import json
import io
import re
import time
from pathlib import Path
from types import SimpleNamespace

import jd_product_mover as mover
import jd_product_pipeline as pipeline
import jdo_listing_mvp as jingmai
import deferred_publish_batch as batch

ROOT = Path(__file__).resolve().parents[1]


def digest_json(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require_confirmation(confirmation, token, plan):
    if confirmation != "PUBLISH_TO_JD" or token != digest_json(plan):
        raise ValueError("Explicit publication confirmation and matching plan digest required")


def original_fingerprint(detail):
    info = detail.get("productInfo") or {}
    fields = ("productId", "productName", "itemNum", "categoryDetail", "brandInfo", "goodsAttrInfos",
              "productDetailDesc", "logisticsInfo", "productStatus")
    skus = [{key: sku.get(key) for key in ("skuId", "outerId", "priceInfo", "saleAttrs", "goodsAttrInfos",
                                          "skuEnableStatus", "skuStatus", "logo")}
            for sku in detail.get("skuList") or []]
    return digest_json({"product": {key: info.get(key) for key in fields},
                        "skus": sorted(skus, key=lambda item: item["skuId"]), "material": detail.get("material")})


def clone_listing(template, detail):
    result = copy.deepcopy(template)
    online = {str(sku.get("outerId") or ""): sku for sku in detail["skuList"]}
    expected = [str(sku.get("vendorSku") or "").strip() or jingmai.resolved_vendor_sku(template, sku) for sku in template["skus"]]
    if "" in online or len(online) != len(detail["skuList"]) or set(expected) != set(online):
        raise ValueError("Original merchant SKU mapping is incomplete or ambiguous")
    result["source"] = {}
    result["product"]["title"] = detail["productInfo"]["productName"]
    result["product"]["itemNumber"] = f"ABIMG-{detail['productInfo']['productId']}"
    for sku, code in zip(result["skus"], expected):
        current = online[code]
        aliases = {str(attr["attrId"]): attr.get("attrValueAlias", "") for attr in current.get("saleAttrs") or []}
        if not aliases.get("1001080021") or (result["product"].get("includeSizeSaleAttribute", True) and not aliases.get("1001080022")):
            raise ValueError("Original sale attributes missing")
        sku.update(vendorSku=code, jdPrice=current["priceInfo"]["jdPrice"],
                   enabled=int(current.get("skuEnableStatus") or 0) == 1, styleAlias=aliases["1001080021"])
        if result["product"].get("includeSizeSaleAttribute", True):
            sku["sizeAlias"] = aliases["1001080022"]
    return result


def get_detail(product_id):
    return jingmai.get("jdo-product-status", "/rest/sp-product/v0/products/{productId}",
                       {"productId": int(product_id), "scene": "pop"}).get("data") or {}


def template_with_current_codes(template, detail, matched):
    result = copy.deepcopy(template)
    online = {str(sku.get("outerId") or ""): sku for sku in detail["skuList"]}
    source_ids = [sku.get("sourceSku") for sku in result["skus"]]
    if len(set(source_ids)) != len(source_ids) or set(source_ids) != set(matched):
        raise ValueError("Current workbook does not cover exactly the source SKU set")
    if len(online) != len(detail["skuList"]):
        raise ValueError("Live merchant codes are ambiguous")
    for sku in result["skus"]:
        code = matched[sku["sourceSku"]]["vendorSku"]
        if not code or code not in online:
            raise ValueError("Current merchant workbook conflicts with A; repair A under separate authorization before cloning")
        aliases = {str(attribute["attrId"]): attribute["attrValueAlias"] for attribute in online[code]["saleAttrs"]}
        if aliases.get("1001080021") != sku["styleAlias"]:
            raise ValueError("Current merchant code belongs to a different style")
        if result["product"].get("includeSizeSaleAttribute", True) and aliases.get("1001080022") != sku["sizeAlias"]:
            raise ValueError("Current merchant code belongs to a different size")
        sku["vendorSku"] = code
    return result


def prepare(args):
    from jdo_merchant_code_repair import match_workbook
    from pdd_local_export_mvp import workbook_rows

    if not args.source_root:
        raise ValueError("A/B preparation requires the current source-root; archived blank workbooks are not authoritative")
    preflight = mover.read_json(args.preflight)
    store = pipeline.verify_store_identity(mover.read_json(args.store_config), args.profile)
    if store != preflight["store"] or preflight.get("failures"):
        raise ValueError("Preflight store or evidence mismatch")
    records = [row for row in preflight["records"] if row["action"] == "NEW_B_LINK_SAME_MERCHANT_SKUS"]
    records.sort(key=lambda row: (row["sourceSku"] != args.mvp_source_sku, row["sourceSkuCount"], row["sourceSku"]))
    if args.limit:
        records = records[:args.limit]
    inventory = mover.read_json(args.inventory)
    by_source = {row["productId"]: row for row in inventory["products"]}
    reference = mover.read_json(args.reference)
    results = []
    for record in records:
        source_id = record["sourceSku"]
        folder = args.output / "pairs" / source_id
        plan_path = folder / "plan.json"
        if plan_path.is_file():
            existing_plan = mover.read_json(plan_path)
            current_workbook = args.source_root / f"拼多多_商品ID_{source_id}" / "价格计算模板.xlsx"
            if existing_plan.get("merchantWorkbookSha256") != file_hash(current_workbook):
                raise ValueError("Cached plan lacks a current matching merchant workbook; do not reuse it for new publication")
            results.append(existing_plan)
            continue
        state = mover.read_json(args.state_root / f"{source_id}.json")
        original_folder = Path(state["localFolder"])
        template = mover.read_json(original_folder / ".pipeline/listing.uploaded.json")
        original_id = int(record["originalProductId"])
        detail = get_detail(original_id)
        if str(detail["productInfo"].get("itemNum")) != source_id:
            raise ValueError("Original product source identity mismatch")
        current_workbook = args.source_root / f"拼多多_商品ID_{source_id}" / "价格计算模板.xlsx"
        _, current_rows = workbook_rows(current_workbook, source_id)
        manifest = mover.read_json(original_folder / "manifest.json")
        matched = match_workbook(manifest["variants"], current_rows)
        template = template_with_current_codes(template, detail, matched)
        listing = clone_listing(template, detail)
        if (listing.get("video") or {}).get("localPath"):
            raise ValueError("A/B video preservation requires a separate verified plan")
        repaired = Path(record["repairedFolder"])
        audit = mover.read_json(repaired.parent / "audit.json")
        expected_hashes = {row["name"]: row["sha256"] for row in audit["outputs"]}
        source_rows = {int(row["index"]): row for row in by_source[source_id]["rows"]}
        sku_rows = {row["skuKey"]: row for row in listing["skus"]}
        files = {}
        files[str(current_workbook.resolve())] = file_hash(current_workbook)
        replaced = []
        for image in listing["images"]:
            if image["role"] == "sku_main":
                sku = sku_rows[image["skuKey"]]
                match = re.search(r"SKU(\d+)$", str(sku["sourceSku"]))
                if not match:
                    raise ValueError("Cannot resolve repaired image to source SKU")
                name = source_rows[int(match.group(1))]["name"]
                data = (repaired / name).read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if digest != expected_hashes[name]:
                    raise ValueError(f"Repaired image hash changed: {name}")
                destination = args.output / "assets" / f"{digest}.jpg"
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.is_file():
                    destination.write_bytes(data)
                image["localPath"] = str(destination.resolve())
                image["upload"] = {"status": "pending"}
                replaced.append({"skuKey": image["skuKey"], "sourceFile": name, "sha256": digest})
            else:
                image["localPath"] = str(jingmai.resolve_path(original_folder / "listing.json", image["localPath"]))
            files[image["localPath"]] = file_hash(image["localPath"])
        mover.write_json(folder / "baseline.json", detail)
        mover.write_json(folder / "listing.json", listing)
        errors, warnings = jingmai.validate_manifest(folder / "listing.json", listing, reference)
        if errors:
            raise ValueError(f"{source_id}: {errors}")
        plan = {"sourceSku": source_id, "originalProductId": original_id, "store": store,
                "merchantWorkbookSha256": file_hash(current_workbook), "merchantWorkbookPath": str(current_workbook.resolve()),
                "itemNumber": listing["product"]["itemNumber"], "skuCount": len(listing["skus"]),
                "originalFingerprint": original_fingerprint(detail), "listingSha256": file_hash(folder / "listing.json"),
                "files": files, "replacedImages": replaced, "createdAt": mover.now_iso(),
                "invariants": ["same merchant SKUs", "same live JD prices", "same sale attributes", "never modify A"],
                "rollback": "Do not auto-delete either link; stop and retain B product/application ID", "warnings": warnings}
        mover.write_json(plan_path, plan)
        mover.write_json(folder / "dry-run.json", {"ok": True, "confirmToken": digest_json(plan), "remoteWrites": False})
        results.append(plan)
        print(json.dumps({"stage": "PREPARED", "sourceSku": source_id, "skuCount": plan["skuCount"]}, ensure_ascii=False), flush=True)
    mover.write_json(args.output / "prepared.json", {"store": store, "pairCount": len(results), "pairs": results})
    return results


def sku_contract(detail):
    return {str(sku["outerId"]): {"price": str(sku["priceInfo"]["jdPrice"]),
                                 "attributes": sorted((str(attr["attrId"]), attr.get("attrValueAlias"))
                                                      for attr in sku.get("saleAttrs") or [])}
            for sku in detail["skuList"]}


def verify_pair(folder, product_id, args):
    plan = mover.read_json(folder / "plan.json")
    listing_path = folder / "listing.uploaded.json"
    listing = mover.read_json(listing_path)
    baseline = mover.read_json(folder / "baseline.json")
    for attempt in range(1, 25):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                status = jingmai.command_verify_product(SimpleNamespace(
                    input=listing_path, product_id=product_id, output=folder / "verification.json"))
            if status == 0:
                break
        except jingmai.JdoError as error:
            if "未查询到商品信息" not in str(error):
                raise
            rejection = pipeline.product_apply_rejection(product_id)
            if rejection:
                mover.write_json(folder / "rejection.json", rejection)
                pipeline.save_state(folder / "state.json", "PRODUCT_APPLY_REJECTED", productId=product_id,
                                    productApply=rejection)
                raise ValueError(f"B application rejected; preserved ID {product_id}: {rejection}")
        time.sleep(5)
    else:
        raise batch.VerificationPending(f"B product {product_id} verification pending; do not recreate")
    current = get_detail(plan["originalProductId"])
    new_detail = get_detail(product_id)
    if original_fingerprint(current) != plan["originalFingerprint"]:
        raise ValueError("Original A changed; stop batch for independent review")
    if sku_contract(baseline) != sku_contract(new_detail):
        raise ValueError("A/B merchant SKU, price or specification mismatch")
    original_sku_ids = {sku["skuId"] for sku in baseline["skuList"]}
    if original_sku_ids & {sku["skuId"] for sku in new_detail["skuList"]}:
        raise ValueError("A/B platform SKU IDs must be distinct")
    sku_by_key = {sku["skuKey"]: sku for sku in listing["skus"]}
    first_by_style = {sku_by_key[image["skuKey"]]["styleAlias"]: image["upload"]["imgUrl"]
                      for image in listing["images"] if image["role"] == "sku_main"}
    color_ids = jingmai.online_style_color_ids(new_detail)
    groups = {group["uuid"]: group["imageInfoList"] for group in new_detail["material"]["mainImages"]}
    for alias, expected_url in first_by_style.items():
        images = groups[color_ids[jingmai.normalized_sale_alias(alias)]]
        first = next(image for image in images if int(image["orderSort"]) == 1)
        if first["imgUrl"] != expected_url:
            raise ValueError("B repaired first image mismatch")
    for key in ("productName", "categoryDetail", "brandInfo", "productDetailDesc", "logisticsInfo", "goodsAttrInfos"):
        if baseline["productInfo"].get(key) != new_detail["productInfo"].get(key):
            raise ValueError(f"A/B unchanged product field differs: {key}")
    mover.write_json(folder / "b-detail.json", new_detail)
    mover.write_json(folder / "a-after.json", current)
    result = {"ok": True, "originalProductId": plan["originalProductId"], "productId": product_id,
              "skuCount": len(new_detail["skuList"]), "originalUnchanged": True,
              "sameMerchantSkusPricesSpecifications": True, "repairedFirstImagesVerified": True,
              "verifiedAt": mover.now_iso()}
    mover.write_json(folder / "ab-verification.json", result)
    return result


def publish_pair(folder, args, *, defer_verification=False):
    plan = mover.read_json(folder / "plan.json")
    token = mover.read_json(folder / "dry-run.json")["confirmToken"]
    require_confirmation(args.confirm, token, plan)
    if file_hash(folder / "listing.json") != plan["listingSha256"]:
        raise ValueError("Listing changed after dry-run")
    for name, digest in plan["files"].items():
        if file_hash(name) != digest:
            raise ValueError(f"Asset changed after dry-run: {name}")
    store = pipeline.verify_store_identity(mover.read_json(args.store_config), args.profile)
    if store != plan["store"]:
        raise batch.BatchStopError("Confirmed store changed")
    current = get_detail(plan["originalProductId"])
    if original_fingerprint(current) != plan["originalFingerprint"]:
        raise ValueError("A changed since preparation; cannot copy stale controls")
    state_path = folder / "state.json"
    state = mover.read_json(state_path) if state_path.is_file() else {}
    if state.get("stage") == "PRODUCT_APPLY_REJECTED":
        raise ValueError("Previously rejected B application retained; do not recreate")
    product_id = int(state.get("productId") or 0)
    if defer_verification and (product_id or state.get("applyId") or state.get("stage") == "SUBMITTING"):
        return batch.submitted_result(state)
    if not product_id:
        existing = pipeline.find_existing_product(plan["itemNumber"])
        if existing:
            product_id = int(existing["productId"])
            pipeline.save_state(state_path, "EXISTING_B_FOUND", productId=product_id)
        elif state.get("stage") == "SUBMITTING":
            raise ValueError("Prior submission ambiguous; stop without retrying creation")
    if product_id == plan["originalProductId"]:
        raise ValueError("Refusing to use A product as B")
    if not product_id:
        if not plan.get("merchantWorkbookSha256") or file_hash(plan["merchantWorkbookPath"]) != plan["merchantWorkbookSha256"]:
            raise ValueError("New B publication requires a hash-bound current merchant workbook")
        uploaded = folder / "listing.uploaded.json"
        if not uploaded.is_file() or any(not (image.get("upload") or {}).get("imgUrl")
                                        for image in mover.read_json(uploaded)["images"]):
            with contextlib.redirect_stdout(io.StringIO()):
                status = jingmai.command_upload(SimpleNamespace(input=folder / "listing.json", reference=args.reference,
                                                               output=uploaded, confirm="UPLOAD_TO_JD"))
            if status:
                raise ValueError("Repaired image upload failed")
        jingmai.write_publish_payloads(uploaded, folder / "publish")
        body = folder / "publish/publish-body.json"
        payload = mover.read_json(body)
        if payload["productApplyDTO"].get("productId") or '"componentCode":"SkuId"' in payload["schema"]:
            raise ValueError("New B payload must never contain original platform IDs")
        response = jingmai.run_jdo_large(url="/rest/sp-product/v0/product-schemas/validations", method="POST",
                                        body_path=folder / "publish/validation-body.json", skill_id="jdo-product-publish", yes=True)
        mover.write_json(folder / "publish/validation-response.json", response)
        if any(int(item.get("detectResult") or 0) == 1 for item in pipeline.validation_violations(response)):
            raise ValueError("JD validation rejected B images or content")
        pipeline.save_state(state_path, "SUBMITTING", originalProductId=plan["originalProductId"],
                            confirmToken=token, payloadSha256=file_hash(body), submittedAt=mover.now_iso())
        response = jingmai.run_jdo_large(url="/rest/sp-product/v0/product-applies", method="POST", body_path=body,
                                        skill_id="jdo-product-publish", yes=True)
        product_id = batch.record_response(folder, response)
        print(json.dumps({"stage": "PUBLISHED", "sourceSku": plan["sourceSku"], "productId": product_id}), flush=True)
    if defer_verification:
        return batch.submitted_result(batch.read_state(folder))
    result = verify_pair(folder, product_id, args)
    pipeline.save_state(state_path, "VERIFIED", **{key: value for key, value in result.items() if key != "ok"})
    return result


def run(args, *, audit_only=False):
    if not audit_only and args.confirm != "PUBLISH_TO_JD":
        raise ValueError("Explicit publication confirmation required")
    prepared = mover.read_json(args.output / "prepared.json")
    return batch.run_batch(args, prepared["pairs"], "pairs", ".ab-publish.lock",
                           lambda folder: publish_pair(folder, args, defer_verification=True),
                           lambda folder, product_id: verify_pair(folder, product_id, args),
                           skip_failures=args.skip_failures, audit_only=audit_only)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "publish", "verify"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--state-root", type=Path, default=ROOT / "outputs/migrations/state")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--mvp-source-sku", default="")
    parser.add_argument("--store-config", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--skip-failures", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    args.output = args.output.resolve()
    prepare(args) if args.command == "prepare" else run(args, audit_only=args.command == "verify")


if __name__ == "__main__":
    main()
