#!/usr/bin/env python3
"""同款矩阵换主图上架：沿用原链接商品数据，按多种视觉风格重做主图后各建一条新链接。

Run directory layout (one source product per run):

    <run>/variants.json                 风格定义（用户定稿，见 references/matrix-main-image-listing.md）
    <run>/source-apply.json             原链接 product-apply 快照（不是 products 接口）
    <run>/source-main/<file>            原链接主图上传原件（imgzone，不带水印）
    <run>/generated/<v>/<stem>.jpg      生成图；ledger.json 原子落盘
    <run>/generated/<v>/uploads.json    图片空间回执
    <run>/publish/<v>/...               schema / 校验 / 提交回执 / state / 回查 / 视频

Every Jingmai write re-checks profile + venderId + shopName. Publishing needs
``--confirm PUBLISH_TO_JD``; a run that reached SUBMITTING is reconciled, never resubmitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import win_no_window  # noqa: F401,E402  (hide jdo-cli consoles)
import hot_replica_mode as hrm  # noqa: E402
import jdo_listing_mvp as cli  # noqa: E402

MODELS = ("GPT-Image-2.5-Flare-joybuilder", "GPT-Image-2.5-Sunburst-joybuilder")
SHARED_GROUP = "0000000000"
IMAGE_HOSTS = ("https://img10.360buyimg.com/imgzone/", "https://img10.360buyimg.com/image/")
VARIANT_PATTERN = re.compile(r"^[a-z0-9]{1,8}$")
PUBLISH_CONFIRM = "PUBLISH_TO_JD"
VIDEO_CONFIRM = "BIND_SOURCE_VIDEO"
PATCH_CONFIRM = "PATCH_PRODUCT_IMAGES"

# Shared hard requirements proven on the 2026-11-03 matrix batch. Appended after
# the user-finalized dynamic prompt and the per-link style suffix; never edit the
# dynamic prompt itself.
COMMON_HARD_RULES = (
    "通用硬性要求：商品本身的外形结构、颜色、材质、配件、数量和比例必须与原图完全一致，不得增删零件或改变款式；"
    "如果原图只拍摄了商品的局部特写，必须保持同样的局部视角与裁切范围，严禁补画或延伸出原图画面之外的任何商品部件；"
    "原图文字卖点的内容要逐字保留、准确无错别字，只改变字体、颜色和位置；"
    "不要新增原图没有的功效、材质、数据、服务或承诺类文字。"
)
TEXT_LOCK_TEMPLATE = (
    "【本张文字锁定】画面中只允许出现以下文字，逐字准确：{lock}；"
    "删除原图中的外文小字，不要出现任何其他文字、编号或卖点标签。"
)
IMAGE_NOTE_TEMPLATE = "【本张特别要求】{note}"


class MatrixError(RuntimeError):
    pass


# ------------------------------------------------------------------ json I/O
def save_json_atomic(path: Path, value: Any, sleep: Callable[[float], None] = time.sleep) -> None:
    """tmp + os.replace with retries: Windows AV/backup locks raise WinError 5/13."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=1)
    last: Exception | None = None
    for attempt in range(10):
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
            return
        except OSError as exc:
            last = exc
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            sleep(0.3 * (attempt + 1))
    raise MatrixError(f"无法写入 {path.name}：{last}")


def load_json(path: Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def url_stem(url: str) -> str:
    return str(url).rsplit("/", 1)[-1].rsplit(".", 1)[0]


# ------------------------------------------------------------- source images
def group_image_urls(apply: dict[str, Any]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for group in (apply.get("material") or {}).get("mainImages") or []:
        images = sorted(group.get("imageInfoList") or [], key=lambda item: int(item.get("orderSort") or 0))
        groups[str(group.get("uuid"))] = [str(item.get("imgUrl") or "") for item in images if item.get("imgUrl")]
    return groups


def source_image_urls(apply: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    for urls in group_image_urls(apply).values():
        for url in urls:
            if url not in ordered:
                ordered.append(url)
    return ordered


def original_image_urls(url: str) -> list[str]:
    """imgzone returns the uploaded original; /n0/ is watermarked and never used."""
    return [host + str(url).lstrip("/") for host in IMAGE_HOSTS]


# ------------------------------------------------------------------- prompts
def load_variants(path: Path) -> tuple[dict[str, dict[str, Any]], set[str]]:
    data = load_json(path)
    if not isinstance(data, dict):
        raise MatrixError(f"缺少风格定义：{path}")
    if isinstance(data.get("variants"), dict):
        variants = data["variants"]
    else:
        variants = {key: value for key, value in data.items() if isinstance(value, dict)}
    keep = {str(item) for item in data.get("keep") or []}
    for name, variant in variants.items():
        if not VARIANT_PATTERN.fullmatch(name):
            raise MatrixError(f"风格编号只能是 1-8 位小写字母或数字：{name}")
        if not str(variant.get("suffix") or "").strip():
            raise MatrixError(f"风格 {name} 缺少 suffix")
    return variants, keep


def build_variant_prompt(title: str, variant: dict[str, Any], stem: str) -> str:
    suffix = str(variant.get("suffix") or "")
    prompt = hrm.build_dynamic_prompt(title) + suffix
    if "通用硬性要求" not in suffix:  # verified suffixes may carry their own wording
        prompt += COMMON_HARD_RULES
    note = (variant.get("imageNotes") or {}).get(stem)
    if note:
        prompt += IMAGE_NOTE_TEMPLATE.format(note=note)
    lock = (variant.get("textLocks") or {}).get(stem)
    if lock:
        prompt += TEXT_LOCK_TEMPLATE.format(lock=lock)
    return prompt


def model_order(index: int) -> tuple[str, str]:
    return MODELS[index % 2], MODELS[1 - index % 2]


# ------------------------------------------------------------------- ledgers
def rebuild_ledger(ledger: dict[str, Any], out_dir: Path, stems: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Mark images already on disk as done so a crashed run never re-bills them."""
    from PIL import Image

    recovered: list[str] = []
    for stem in stems:
        if (ledger.get(stem) or {}).get("status") == "ok":
            continue
        path = Path(out_dir) / f"{stem}.jpg"
        if not path.exists():
            continue
        try:
            with Image.open(path) as image:
                image.verify()
        except Exception:  # noqa: BLE001 - truncated or not an image
            path.replace(path.with_suffix(".broken"))
            continue
        ledger[stem] = {"status": "ok", "recovered": True, "bytes": path.stat().st_size}
        recovered.append(stem)
    return ledger, recovered


# ------------------------------------------------------------------- listing
def matrix_item_number(variant: str, source_product_id: int | str) -> str:
    return f"MX{variant.upper()}-{source_product_id}"


def matrix_mapping_id(source_product_id: int | str, variant: str, source_attr_values: str) -> str:
    return hashlib.sha256(f"{source_product_id}|{variant}|{source_attr_values}".encode("utf-8")).hexdigest()[:32]


def _prop(key: str, value: Any) -> dict[str, Any]:
    return {"key": key, "value": value}


def _upload_url(uploads: dict[str, Any], stem: str) -> str:
    value = uploads.get(stem)
    if isinstance(value, dict):
        value = value.get("imgUrl")
    return str(value or "")


def white_background_by_sku(apply: dict[str, Any]) -> dict[str, str]:
    result = {}
    for item in (apply.get("material") or {}).get("whiteBackGroundImages") or []:
        image = item.get("whiteBackGroundImage") or {}
        if image.get("imageUrl") and int(image.get("status") or 0) == 4:
            result[str(item.get("uuid"))] = str(image["imageUrl"])
    return result


def _image_components(urls: list[str], ref: str) -> list[dict[str, Any]]:
    return [{"componentCode": "img", "properties": [
        _prop("imgUrl", url), _prop("isGgt", 1 if order == 1 else 0),
        _prop("isPrimary", 1 if order == 1 else 0), _prop("imgRefId", ref),
        _prop("colorId", ref), _prop("orderSort", order),
    ]} for order, url in enumerate(urls, 1)]


def _product_components(info: dict[str, Any], item_number: str, shared: list[str]) -> list[dict[str, Any]]:
    features = {item["key"]: item["value"] for item in info.get("features") or []}
    desc = info.get("productDetailDesc") or {}
    components: list[dict[str, Any]] = [
        {"componentCode": "WareTitle", "value": info["productName"]},
        {"componentCode": "WareBrand", "value": int((info.get("brandInfo") or {}).get("brandId") or -1)},
        {"componentCode": "WareItemnum", "value": item_number},
        {"componentCode": "WareSkuImg", "value": [
            {"componentCode": "img", "properties": [
                _prop("imgRefId", SHARED_GROUP), _prop("colorId", SHARED_GROUP),
                _prop("orderSort", str(order)), _prop("imgUrl", url),
            ]} for order, url in enumerate(shared, 1)
        ]},
        {"componentCode": "WareEditor", "value": [{"componentCode": "detail", "properties": [
            _prop("desc", desc.get("desc") or ""),
            _prop("zhuangBaUseFlag", str(desc.get("zhuangBaUseFlag") or "0")),
            _prop("userAgent", "pc"),
        ]}]},
        {"componentCode": "WareLength", "value": str(info["length"])},
        {"componentCode": "WareWide", "value": str(info["width"])},
        {"componentCode": "WareHeight", "value": str(info["height"])},
        {"componentCode": "WareWeight", "value": str(info["weight"])},
        {"componentCode": "WareSevenDayReturn", "value": features.get("is7ToReturn", "1")},
        {"componentCode": "PayMethodLimit", "properties": [_prop("isPayFirst", features.get("isPayFirst", "1"))]},
        {"componentCode": "CategoryGroupList", "value": [
            {"componentCode": f"prop-product-{attr['attrId']}", "properties": [
                _prop("attrValues", ",".join(str(value["attrValue"]) for value in attr.get("values") or [])),
            ]} for attr in info.get("goodsAttrInfos") or [] if attr.get("values")
        ]},
    ]
    transport = (info.get("logisticsInfo") or {}).get("transportId")
    if transport:
        components.append({"componentCode": "WareTransport", "value": str(transport)})
    return components


def build_matrix_listing(
    apply: dict[str, Any], variant: str, uploads: dict[str, Any], keep_stems: set[str] | None = None,
    white_background: str = "copy",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    keep_stems = set(keep_stems or ())
    info = apply["productInfo"]
    product_id = info["productId"]
    groups = group_image_urls(apply)
    missing = sorted({
        url_stem(url) for url in source_image_urls(apply)
        if url_stem(url) not in keep_stems and not _upload_url(uploads, url_stem(url))
    })
    if missing:
        raise MatrixError(f"风格 {variant} 还有 {len(missing)} 张主图未生成或未上传：{missing[:5]}")

    def new_url(url: str) -> str:
        stem = url_stem(url)
        return url if stem in keep_stems else _upload_url(uploads, stem)

    item_number = matrix_item_number(variant, product_id)
    shared = [new_url(url) for url in groups.get(SHARED_GROUP, [])]
    components = _product_components(info, item_number, shared)
    whites = white_background_by_sku(apply) if white_background == "copy" else {}
    skus, contract = [], []
    for sku in sorted(apply["skuList"], key=lambda item: int(item["saleAttrs"][0]["attrValuesSeqNo"])):
        if len(sku.get("saleAttrs") or []) != 1:
            raise MatrixError(f"SKU {sku.get('outerId')} 不是单维销售属性，矩阵脚本暂不支持")
        sale = sku["saleAttrs"][0]
        old_group = str(sale["attrValues"])
        if old_group not in groups:
            raise MatrixError(f"SKU {sku.get('outerId')} 找不到主图组 {old_group}")
        mapping_id = matrix_mapping_id(product_id, variant, old_group)
        sku_features = {item["key"]: item["value"] for item in sku.get("features") or []}
        images = [new_url(url) for url in groups[old_group]]
        values: list[dict[str, Any]] = [
            {"componentCode": "SkuJdprice", "value": str(sku["priceInfo"]["jdPrice"])},
            {"componentCode": "SkuStockNum", "value": int(sku.get("stockNum") or 0)},
            {"componentCode": "SkuVendorSku", "value": sku["outerId"]},
            {"componentCode": "SkuEnableStatus", "value": str(sku.get("skuEnableStatus") or 1)},
            {"componentCode": "SkuMainImage", "value": _image_components(images, mapping_id)},
        ]
        white = whites.get(str(sku.get("uuid") or sku.get("skuId")))
        if white:
            values.append({"componentCode": "SkuWhiteBackground", "value": [{"componentCode": "img", "properties": [
                _prop("imgUrl", white), _prop("isPrimary", "1"),
            ]}]})
        if sku_features.get("shortTitle"):
            values.append({"componentCode": "SkuShortTitle", "value": sku_features["shortTitle"]})
        sku_attrs = [
            {"componentCode": f"prop-sku-{attr['attrId']}", "properties": [
                _prop("attrValues", "[" + ",".join(str(value["attrValue"]) for value in attr["values"]) + "]"),
            ]} for attr in sku.get("goodsAttrInfos") or [] if attr.get("values")
        ]
        if sku_attrs:
            values.append({"componentCode": "SkuAttr", "value": sku_attrs})
        values.append({"componentCode": "VenderCategoryValue", "value": [{
            "componentCode": f"sale-prop-{sale['attrId']}",
            "properties": [
                _prop("attrValueAlias", f"[{sale['attrValueAlias']}]"), _prop("attrName", sale["attrName"]),
                _prop("mappingId", mapping_id), _prop("index", "1"),
                _prop("attrValuesSeqNo", int(sale["attrValuesSeqNo"])),
            ],
        }]})
        skus.append({"componentCode": "SKU", "value": values})
        contract.append({
            "outerId": sku["outerId"], "alias": sale["attrValueAlias"], "jdPrice": sku["priceInfo"]["jdPrice"],
            "stockNum": int(sku.get("stockNum") or 0), "seq": sale["attrValuesSeqNo"],
            "mainImages": images, "whiteBackground": white or "",
        })
    components.append({"componentCode": "SKUS", "value": skus})
    plan = {
        "variant": variant, "sourceProductId": product_id, "itemNumber": item_number,
        "title": info["productName"], "category": int((info.get("categoryDetail") or {})["lastCategoryId"]),
        "shared": shared, "skus": contract, "whiteBackground": white_background,
    }
    return components, plan


# ------------------------------------------------------------------ readback
def apply_ready(apply: dict[str, Any]) -> bool:
    status = int((apply.get("applyInfo") or {}).get("draftComputeStatus") or 0)
    return status not in {1, 3} and bool(apply.get("skuList"))


def readback_problems(plan: dict[str, Any], after: dict[str, Any], source: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    online = {str(sku.get("outerId")): sku for sku in after.get("skuList") or []}
    if len(online) != len(plan["skus"]):
        problems.append(f"sku count {len(online)} != {len(plan['skus'])}")
    groups = group_image_urls(after)
    if groups.get(SHARED_GROUP, []) != plan["shared"]:
        problems.append("shared images mismatch")
    whites = white_background_by_sku(after)
    for entry in plan["skus"]:
        sku = online.get(entry["outerId"])
        if not sku:
            problems.append(f"missing {entry['outerId']}")
            continue
        sale = (sku.get("saleAttrs") or [{}])[0]
        if float((sku.get("priceInfo") or {}).get("jdPrice") or 0) != float(entry["jdPrice"]):
            problems.append(f"price {entry['outerId']}")
        if sale.get("attrValueAlias") != entry["alias"]:
            problems.append(f"alias {entry['outerId']}")
        if "stockNum" in sku and "stockNum" in entry and int(sku.get("stockNum") or 0) != int(entry["stockNum"]):
            problems.append(f"stock {entry['outerId']}")
        images = groups.get(str(sale.get("attrValues"))) or []
        if images[: len(entry["mainImages"])] != entry["mainImages"]:
            problems.append(f"images {entry['outerId']} {len(images)}/{len(entry['mainImages'])}")
        if entry.get("whiteBackground"):
            if whites.get(str(sku.get("uuid") or sku.get("skuId"))) != entry["whiteBackground"]:
                problems.append(f"white background {entry['outerId']}")
    before_desc = ((source.get("productInfo") or {}).get("productDetailDesc") or {}).get("desc")
    after_desc = ((after.get("productInfo") or {}).get("productDetailDesc") or {}).get("desc")
    if before_desc != after_desc:
        problems.append("detail differs")
    return problems


def _prices(apply: dict[str, Any]) -> dict[str, float]:
    return {
        str(sku.get("outerId")): float((sku.get("priceInfo") or {}).get("jdPrice") or 0)
        for sku in apply.get("skuList") or []
    }


def source_unchanged(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    problems = []
    if group_image_urls(before) != group_image_urls(after):
        problems.append("source main images changed")
    if _prices(before) != _prices(after):
        problems.append("source prices changed")
    return problems


def pick_source_video(materials_by_sku: dict[int, list[dict[str, Any]]]) -> dict[str, Any] | None:
    found: dict[int, dict[str, Any]] = {}
    for materials in materials_by_sku.values():
        for item in materials or []:
            video = item.get("videoMaterial") or {}
            if not video.get("videoId"):
                continue
            if int(item.get("status") or 0) != 4:
                return None
            found[int(video["videoId"])] = video
    if len(found) != 1:
        return None
    video = next(iter(found.values()))
    return {
        "videoId": int(video["videoId"]), "tagId": int(video["tagId"]),
        "tagName": str(video["tagName"]), "sort": int(video.get("tagOrder") or 1),
    }


def publish_guard(state: dict[str, Any]) -> None:
    if state.get("productId") or state.get("stage") in {"SUBMITTING", "PUBLISHED"}:
        raise MatrixError(f"已提交过（{state.get('stage')} {state.get('productId') or ''}），只能回查对账，禁止重复提交")


# ------------------------------------------------------------------ jingmai
def verify_store(args: argparse.Namespace) -> dict[str, Any]:
    """Profile + venderId + shopName; explicit flags or store-config, never default."""
    if args.vender_id and args.shop_name:
        profile = cli.configure_jdo_profile(args.profile)
        data = cli.get("jdo-seller-info", "/rest/sp-seller/v0/venderinfo", {"scopeSet": "baseInfo"}).get("data") or {}
        vender_id = int(data.get("venderId") or 0)
        shop_name = str((data.get("shopBaseInfo") or {}).get("shopName") or "").strip()
        if vender_id != int(args.vender_id) or shop_name != args.shop_name.strip():
            raise MatrixError(f"Profile {profile} 对应店铺不一致：venderId={vender_id}, shopName={shop_name}")
        cli.mark_jdo_store_verified(profile, vender_id, shop_name)
        return {"profile": profile, "venderId": vender_id, "shopName": shop_name}
    return cli.verify_configured_store_identity(Path(args.store_config), args.profile)


def read_apply(product_id: int | str) -> dict[str, Any]:
    return cli.get(
        "jdo-product-update", "/rest/sp-product/v0/product-apply/{applyId}",
        {"applyId": str(product_id), "scene": "pop"},
    ).get("data") or {}


def read_product(product_id: int | str) -> dict[str, Any]:
    return cli.get(
        "jdo-product-status", "/rest/sp-product/v0/products/{productId}",
        {"productId": product_id, "scene": "pop"},
    ).get("data") or {}


def image_media_target() -> tuple[str, int]:
    zones = cli.get("jdo-product-material", "/rest/sp-product/v0/media-zones").get("data") or []
    zone = next((item for item in zones if int(item.get("zoneType") or 0) == 1), None)
    if not zone:
        raise MatrixError("当前店铺没有图片空间")
    categories = cli.get(
        "jdo-product-material", "/rest/sp-product/v0/media-zones/{zoneId}/categories",
        {"zoneId": zone["zoneId"], "page": 1, "pageSize": 100},
    ).get("data") or []
    category = next(
        (item for item in categories if item.get("categoryName") == "我的本地文件"),
        next((item for item in categories if int(item.get("status") or 0) == 1), None),
    )
    if not category:
        raise MatrixError("图片空间没有可用分类")
    return str(zone["zoneId"]), int(category["categoryId"])


def resolve_api_key() -> str:
    for name in ("JD_LLM_API_KEY", "GPT_IMAGE2_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, name)[0] or "").strip()
            if value:
                return value
        except Exception:  # noqa: BLE001 - absent value / not Windows
            pass
    return ""


# ----------------------------------------------------------------- commands
def run_dir(args: argparse.Namespace) -> Path:
    return Path(args.run).resolve()


def source_apply_data(run: Path) -> dict[str, Any]:
    data = load_json(run / "source-apply.json")
    if not data:
        raise MatrixError("先执行 snapshot 读取原链接")
    return data


def command_snapshot(args: argparse.Namespace) -> int:
    import requests
    from PIL import Image

    run = run_dir(args)
    verify_store(args)
    apply = read_apply(args.source_product_id)
    if not apply.get("skuList"):
        raise MatrixError("原链接 product-apply 未返回 SKU")
    save_json_atomic(run / "source-apply.json", apply)
    folder = run / "source-main"
    folder.mkdir(parents=True, exist_ok=True)
    manifest = []
    for url in source_image_urls(apply):
        name = url.rsplit("/", 1)[-1]
        target = folder / name
        if not target.exists():
            for candidate in original_image_urls(url):
                response = requests.get(candidate, timeout=60, headers={
                    "Referer": "https://item.jd.com/", "User-Agent": "Mozilla/5.0",
                })
                if response.ok and len(response.content) > 1000:
                    target.write_bytes(response.content)
                    break
        if not target.exists():
            raise MatrixError(f"原图下载失败：{url}")
        with Image.open(target) as image:
            size = list(image.size)
        manifest.append({"url": url, "stem": url_stem(url), "file": name, "size": size,
                         "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    save_json_atomic(run / "source-main-manifest.json", manifest)
    print(json.dumps({"skus": len(apply["skuList"]), "groups": len(group_image_urls(apply)),
                      "images": len(manifest), "whiteBackground": len(white_background_by_sku(apply))},
                     ensure_ascii=False))
    return 0


def command_generate(args: argparse.Namespace) -> int:
    import company_jpeg_edits as company

    run = run_dir(args)
    apply = source_apply_data(run)
    variants, keep = load_variants(Path(args.variants or run / "variants.json"))
    variant = variants.get(args.variant)
    if not variant:
        raise MatrixError(f"variants.json 没有风格 {args.variant}")
    sources = [item for item in load_json(run / "source-main-manifest.json") or [] if item["stem"] not in keep]
    if args.only:
        sources = [item for item in sources if item["stem"] in set(args.only)]
    out = run / "generated" / args.variant
    out.mkdir(parents=True, exist_ok=True)
    ledger_path = out / "ledger.json"
    ledger, recovered = rebuild_ledger(load_json(ledger_path, {}) or {}, out, [item["stem"] for item in sources])
    if args.redo:
        if not args.only:
            raise MatrixError("--redo 只能配合 --only 指定的单张或少量问题图")
        for item in sources:
            path = out / f"{item['stem']}.jpg"
            if path.exists():
                (out / "superseded").mkdir(exist_ok=True)
                shutil.move(str(path), out / "superseded" / f"{item['stem']}-{int(time.time())}.jpg")
            ledger.pop(item["stem"], None)
    save_json_atomic(ledger_path, ledger)
    todo = [item for item in sources if (ledger.get(item["stem"]) or {}).get("status") != "ok"]
    print(json.dumps({"variant": args.variant, "todo": len(todo), "recovered": recovered,
                      "kept": sorted(keep)}, ensure_ascii=False), flush=True)
    if not todo:
        return 0
    key = resolve_api_key()
    if not key:
        raise MatrixError("缺少 JD_LLM_API_KEY；运行 scripts/bootstrap.py --json 查看 nextActions")
    clients = {model: company.CompanyClient(key) for model in MODELS}
    lock = threading.Lock()
    title = apply["productInfo"]["productName"]

    def work(index: int, item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        prompt = build_variant_prompt(title, variant, item["stem"])
        began, last = time.monotonic(), ""
        for model in model_order(index):
            try:
                raw, _ = clients[model].edit(model=model, prompt=prompt, source=run / "source-main" / item["file"])
            except company.SafeGenerationError as exc:
                last = f"{model}:{exc.status}:{exc.category}"
                continue
            (out / f"{item['stem']}.jpg").write_bytes(raw)
            return item["stem"], {"status": "ok", "model": model, "bytes": len(raw),
                                  "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                                  "seconds": round(time.monotonic() - began, 1)}
        return item["stem"], {"status": "failed", "error": last}

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(work, index, item) for index, item in enumerate(todo)]
        for job in as_completed(jobs):
            stem, record = job.result()
            with lock:
                ledger[stem] = record
                save_json_atomic(ledger_path, ledger)
            print(stem, json.dumps(record, ensure_ascii=False), flush=True)
    failed = sorted(stem for stem, record in ledger.items() if record.get("status") != "ok")
    print(json.dumps({"ok": len(ledger) - len(failed), "failed": failed}, ensure_ascii=False))
    return 0 if not failed else 2


def command_upload(args: argparse.Namespace) -> int:
    from PIL import Image

    run = run_dir(args)
    verify_store(args)
    zone_id, category_id = image_media_target()
    folder = run / "generated" / args.variant
    ledger = load_json(folder / "ledger.json", {}) or {}
    uploads_path = folder / "uploads.json"
    uploads = load_json(uploads_path, {}) or {}
    for stem, record in sorted(ledger.items()):
        if record.get("status") != "ok":
            continue
        path = folder / f"{stem}.jpg"
        with Image.open(path) as image:
            needs_jpeg = image.format != "JPEG"
            if needs_jpeg:
                image.convert("RGB").save(folder / f"{stem}.tmp.jpg", "JPEG", quality=93)
        if needs_jpeg:
            os.replace(folder / f"{stem}.tmp.jpg", path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        current = uploads.get(stem) or {}
        if current.get("imgUrl") and current.get("sha256") == digest:
            continue
        upload_copy = folder / "upload" / f"mx-{args.variant}-{stem}.jpg"
        upload_copy.parent.mkdir(exist_ok=True)
        shutil.copy2(path, upload_copy)
        response = cli.upload(zone_id, category_id, upload_copy)
        url = response.get("imgUrl") or response.get("imageUrl")
        if not url:
            raise MatrixError(f"{stem} 上传未返回 imgUrl")
        previous = list(current.get("previous") or [])
        if current.get("imgUrl"):
            previous.append(current["imgUrl"])
        uploads[stem] = {"imgUrl": url, "sha256": digest, "previous": previous}
        save_json_atomic(uploads_path, uploads)
        print(stem, url, flush=True)
    print(json.dumps({"uploaded": sum(1 for item in uploads.values() if item.get("imgUrl"))}))
    return 0


def command_build(args: argparse.Namespace) -> int:
    run = run_dir(args)
    apply = source_apply_data(run)
    _, keep = load_variants(Path(args.variants or run / "variants.json"))
    uploads = load_json(run / "generated" / args.variant / "uploads.json", {}) or {}
    import detail_pages as _detail
    _detail_problems = _detail.source_detail_problems(
        (apply.get("productInfo") or {}).get("productDetailDesc") or {},
        os.environ.get("MATRIX_DETAIL_EXEMPTION_REASON", ""))
    if _detail_problems:
        raise MatrixError("；".join(_detail_problems))
    components, plan = build_matrix_listing(apply, args.variant, uploads, keep, args.white_background)
    schema = json.dumps(components, ensure_ascii=False, separators=(",", ":"))
    out = run / "publish" / args.variant
    brand_id = int((apply["productInfo"].get("brandInfo") or {}).get("brandId") or -1)
    category = {"lastCategoryId": plan["category"]}
    save_json_atomic(out / "schema.json", components)
    save_json_atomic(out / "validation-body.json", {
        "scene": "pop", "schema": schema, "productApplyDTO": {"brandId": brand_id, "categoryDTO": category}})
    save_json_atomic(out / "publish-body.json", {
        "scene": "pop", "schema": schema, "productApplyDTO": {"categoryDTO": category, "saveActionType": 1}})
    save_json_atomic(out / "plan.json", plan)
    print(json.dumps({"skus": len(plan["skus"]), "item": plan["itemNumber"],
                      "whiteBackground": sum(1 for sku in plan["skus"] if sku["whiteBackground"]),
                      "schemaBytes": len(schema.encode("utf-8"))}, ensure_ascii=False))
    return 0


def command_validate(args: argparse.Namespace) -> int:
    import jd_product_pipeline as pipe

    out = run_dir(args) / "publish" / args.variant
    verify_store(args)
    response = cli.run_jdo_large(
        url="/rest/sp-product/v0/product-schemas/validations", method="POST",
        body_path=out / "validation-body.json", skill_id="jdo-product-publish", yes=True,
    )
    save_json_atomic(out / "validation-response.json", response)
    violations = pipe.validation_violations(response)
    strong = [item for item in violations if int(item.get("detectResult") or 0) == 1]
    existing = pipe.find_existing_product(load_json(out / "plan.json")["itemNumber"])
    print(json.dumps({"violations": len(violations), "strong": len(strong), "existing": existing},
                     ensure_ascii=False))
    if violations:
        print(json.dumps(violations, ensure_ascii=False)[:3000])
    return 0 if not strong else 2


def command_publish(args: argparse.Namespace) -> int:
    if args.confirm != PUBLISH_CONFIRM:
        raise SystemExit(f"正式上架必须传 --confirm {PUBLISH_CONFIRM}")
    import jd_product_pipeline as pipe

    out = run_dir(args) / "publish" / args.variant
    state_path = out / "state.json"
    publish_guard(load_json(state_path, {}) or {})
    body = out / "publish-body.json"
    if not body.exists():
        raise MatrixError("先执行 build 和 validate")
    verify_store(args)
    plan = load_json(out / "plan.json")
    if pipe.find_existing_product(plan["itemNumber"]):
        raise MatrixError(f"货号 {plan['itemNumber']} 已存在，先回查对账")
    save_json_atomic(state_path, {"stage": "SUBMITTING", "at": time.strftime("%F %T"),
                                  "payloadSha256": hashlib.sha256(body.read_bytes()).hexdigest()})
    response = cli.run_jdo_large(
        url="/rest/sp-product/v0/product-applies", method="POST", body_path=body,
        skill_id="jdo-product-publish", yes=True,
    )
    save_json_atomic(out / "publish-response.json", response)
    data = response.get("data") or {}
    state = {"stage": "PUBLISHED" if data.get("productId") else "SUBMITTING",
             "productId": data.get("productId"), "applyId": data.get("applyId"), "at": time.strftime("%F %T")}
    save_json_atomic(state_path, state)
    print(json.dumps(state, ensure_ascii=False))
    return 0 if data.get("productId") else 2


def published_product_id(out: Path) -> int:
    state = load_json(out / "state.json", {}) or {}
    if not state.get("productId"):
        raise MatrixError(f"{out.name} 还没有 productId；SUBMITTING 状态请先按货号回查，不要重提")
    return int(state["productId"])


def command_verify(args: argparse.Namespace) -> int:
    run = run_dir(args)
    out = run / "publish" / args.variant
    cli.configure_jdo_profile(args.profile)
    product_id = published_product_id(out)
    after: dict[str, Any] = {}
    for attempt in range(args.attempts):
        try:
            after = read_apply(product_id)
        except cli.JdoError as exc:
            if "未查询到商品信息" not in str(exc):
                raise
            after = {}
        if int((after.get("applyInfo") or {}).get("draftComputeStatus") or 0) == 3:
            raise MatrixError(f"PRODUCT_APPLY_REJECTED：{product_id} draftComputeStatus=3")
        if apply_ready(after):
            break
        if attempt + 1 < args.attempts:
            time.sleep(args.interval)
    else:
        raise MatrixError(f"{product_id} 申请仍在计算或尚未传播，稍后重跑 verify")
    save_json_atomic(out / "readback-apply.json", after)
    source = source_apply_data(run)
    problems = readback_problems(load_json(out / "plan.json"), after, source)
    problems += source_unchanged(source, read_apply(source["productInfo"]["productId"]))
    product = {}
    for attempt in range(args.attempts):
        try:
            product = read_product(product_id)
            break
        except cli.JdoError as exc:
            if "未查询到商品信息" not in str(exc) or attempt + 1 == args.attempts:
                raise
            time.sleep(args.interval)
    result = {"productId": product_id, "problems": problems,
              "productStatus": (product.get("productInfo") or {}).get("productStatus"),
              "skus": len(product.get("skuList") or [])}
    save_json_atomic(out / "verify.json", result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not problems else 2


def command_video(args: argparse.Namespace) -> int:
    if args.confirm != VIDEO_CONFIRM:
        raise SystemExit(f"绑定主图视频必须传 --confirm {VIDEO_CONFIRM}")
    import jd_product_pipeline as pipe

    run = run_dir(args)
    out = run / "publish" / args.variant
    product_id = published_product_id(out)
    verify_store(args)
    source = source_apply_data(run)
    picked = pick_source_video(pipe.existing_video_materials(sorted(int(s["skuId"]) for s in source["skuList"])))
    if not picked:
        raise MatrixError("原链接没有唯一且审核通过的主图视频，不复用")
    zone = pipe.video_zone_id()
    video = pipe.query_video(zone, picked["videoId"])
    if not video or not pipe.video_is_ready(int(video.get("status") or 0)):
        raise MatrixError(f"视频 {picked['videoId']} 不可用：{video}")
    tag = {"tagId": picked["tagId"], "tagName": picked["tagName"], "sort": picked["sort"]}
    sku_ids = sorted(int(sku["skuId"]) for sku in read_product(product_id).get("skuList") or [])
    folder = out / "video"
    folder.mkdir(parents=True, exist_ok=True)
    existing = pipe.existing_video_materials(sku_ids)
    for sku_id in sku_ids:
        bound = {int((item.get("videoMaterial") or {}).get("videoId") or 0) for item in existing.get(sku_id) or []} - {0}
        if bound == {picked["videoId"]}:
            continue
        if bound:
            raise MatrixError(f"SKU {sku_id} 已绑定其他视频 {sorted(bound)}，禁止覆盖")
        body = folder / f"bind-{sku_id}.json"
        save_json_atomic(body, pipe.build_video_binding_body(product_id=product_id, sku_id=sku_id, video=video, tag=tag))
        response = cli.run_jdo_large(url="/rest/sp-product/v0/sku-materials/batch", method="POST",
                                     body_path=body, skill_id="jdo-product-material", yes=True)
        save_json_atomic(folder / f"bind-{sku_id}-response.json", response)
    audit: dict[str, Any] = {}
    for attempt in range(args.attempts):
        audit = pipe.video_relation_audit(pipe.query_video_relations(zone, picked["videoId"]), sku_ids, picked["videoId"])
        if audit["allApproved"] or audit["rejectedSkuIds"]:
            break
        if attempt + 1 < args.attempts:
            time.sleep(args.interval)
    save_json_atomic(folder / "audit.json", audit)
    print(json.dumps({"videoId": picked["videoId"], **audit}, ensure_ascii=False))
    return 0 if audit.get("allApproved") else 2


def command_patch_fix(args: argparse.Namespace) -> int:
    if args.confirm != PATCH_CONFIRM:
        raise SystemExit(f"线上换图必须传 --confirm {PATCH_CONFIRM}")
    import image_patch_update as ipu

    run = run_dir(args)
    out = run / "publish" / args.variant
    product_id = published_product_id(out)
    entry = (load_json(run / "generated" / args.variant / "uploads.json", {}) or {}).get(args.stem) or {}
    if not entry.get("previous"):
        raise MatrixError(f"{args.stem} 没有被替换的旧上传记录；先 generate --only --redo 再 upload")
    old, new = entry["previous"][-1], entry["imgUrl"]
    verify_store(args)
    before = ipu.apply_detail(product_id)
    slots = [url for urls in ipu.group_urls(before).values() for url in urls]
    if old not in slots and new in slots:
        result = {"productId": product_id, "ok": True, "reconciled": True}
    else:
        result = ipu.patch_product(product_id, {old: new}, PATCH_CONFIRM, out / "fix-patch")
        if not result["ok"]:
            time.sleep(30)
            result["problemsRecheck"] = ipu.verify_patch(before, ipu.apply_detail(product_id), {old: new})
            result["ok"] = not result["problemsRecheck"]
    save_json_atomic(out / f"fix-{args.stem}.json", result)
    print(json.dumps({key: result.get(key) for key in ("productId", "ok", "reconciled", "problems", "problemsRecheck")},
                     ensure_ascii=False))
    return 0 if result.get("ok") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, store: bool = True, variant: bool = True) -> argparse.ArgumentParser:
        command = sub.add_parser(name)
        command.add_argument("--run", required=True, help="本批任务目录")
        if variant:
            command.add_argument("--variant", required=True)
        if store:
            command.add_argument("--profile", required=True, help="店铺专属 jdo-cli Profile，禁止 default")
            command.add_argument("--store-config", default=str(cli.STORE_CONFIG))
            command.add_argument("--vender-id", type=int, help="与 --shop-name 一起使用时替代 store-config")
            command.add_argument("--shop-name")
        command.set_defaults(handler=handler)
        return command

    add("snapshot", command_snapshot, variant=False).add_argument("--source-product-id", required=True)
    generate = add("generate", command_generate, store=False)
    generate.add_argument("--variants")
    generate.add_argument("--only", nargs="*", help="只处理这些原图 stem")
    generate.add_argument("--redo", action="store_true", help="把 --only 的旧成品移到 superseded/ 后重生成")
    add("upload", command_upload)
    build = add("build", command_build, store=False)
    build.add_argument("--variants")
    build.add_argument("--white-background", choices=("copy", "skip"), default="copy")
    add("validate", command_validate)
    add("publish", command_publish).add_argument("--confirm", default="")
    verify = add("verify", command_verify)
    verify.add_argument("--attempts", type=int, default=18)
    verify.add_argument("--interval", type=int, default=10)
    video = add("video", command_video)
    video.add_argument("--confirm", default="")
    video.add_argument("--attempts", type=int, default=10)
    video.add_argument("--interval", type=int, default=30)
    fix = add("patch-fix", command_patch_fix)
    fix.add_argument("--stem", required=True)
    fix.add_argument("--confirm", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if getattr(args, "variant", None) and not VARIANT_PATTERN.fullmatch(args.variant):
        raise SystemExit("--variant 只能是 1-8 位小写字母或数字")
    try:
        return args.handler(args)
    except (MatrixError, cli.JdoError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
