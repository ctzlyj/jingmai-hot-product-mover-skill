# -*- coding: utf-8 -*-
"""无货源模式批次上架构建与发布（通用版）。

阶段1：prepare 只做本地构建与静态校验，不上传图片、不 validate、不 publish。
阶段2：validate / publish / run 需要 --confirm PUBLISH_TO_JD。

批次根目录、店铺 Profile、运费模板、允许类目和定价参数全部来自
<batch-root>/batch-config.json；本脚本不携带任何店铺/类目/批次硬编码。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from PIL import Image

SKILL_SCRIPTS = Path(__file__).resolve().parents[1]
NI_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_SCRIPTS))
sys.path.insert(0, str(NI_SCRIPTS))

import jd_product_mover as mover  # noqa: E402
import jd_product_pipeline as pipeline  # noqa: E402
import jdo_listing_mvp as cli  # noqa: E402
from source_pricing import component_source_audit, component_source_plan  # noqa: E402
import batch as ni_batch  # noqa: E402

ROOT: Path | None = None
CONFIG: dict = {}
PROFILE = ""
FREIGHT_TEMPLATE_ID = 0
STOCK = 200
SEVEN_DAY_RETURN = "1"

CATEGORY_RULES: dict[int, dict] = {}


def set_root(value) -> dict:
    """Bind this module to a batch root and load its batch-config.json.

    Must run before any batch operation; companion scripts call set_root with
    their own --root argument before touching ledger/candidates/reports.
    """
    global ROOT, CONFIG, PROFILE, FREIGHT_TEMPLATE_ID, STOCK, SEVEN_DAY_RETURN
    global CATEGORY_RULES, LIQUID_HINTS, SOLID_HINTS
    ROOT = ni_batch.resolve_root(value)
    CONFIG = ni_batch.load_config(ROOT)
    PROFILE = str(CONFIG.get("profile") or "")
    if not PROFILE or PROFILE == "default":
        raise SystemExit("batch-config.json 缺少店铺专属 profile（禁止 default Profile）")
    FREIGHT_TEMPLATE_ID = int(CONFIG.get("freightTemplateId") or 0)
    if not FREIGHT_TEMPLATE_ID:
        raise SystemExit("batch-config.json 缺少 freightTemplateId")
    STOCK = int(CONFIG.get("stock") or 200)
    SEVEN_DAY_RETURN = str(CONFIG.get("sevenDayReturn") or "1")
    CATEGORY_RULES = {int(key): value for key, value in (CONFIG.get("categories") or {}).items()}
    if not CATEGORY_RULES:
        raise SystemExit("batch-config.json 缺少允许类目 categories（JoySpace 类目表为唯一范围）")
    hints = CONFIG.get("saleUnitHints") or {}
    LIQUID_HINTS = tuple(hints.get("liquid") or DEFAULT_LIQUID_HINTS)
    SOLID_HINTS = tuple(hints.get("solid") or DEFAULT_SOLID_HINTS)
    return CONFIG

BAD_TERMS = (
    "日本", "德国", "进口", "原装进口", "旗舰", "官方", "专卖店", "专营店", "商标",
    "淘宝", "天猫", "淘金币", "1688", "拼多多", "抖音", "小红书", "快手", "唯品会",
    "厂家直销", "工厂直销", "直销", "国补", "国家补贴", "正品", "包邮", "秒杀",
    "限时", "特价", "立减", "领券", "优惠券", "券后", "抢购", "爆款", "热销",
    "店长推荐", "镇店之宝", "免费送", "包退", "包换", "专利", "认证", "权威",
    "除醛率", "除尽", "百分百", "全网最低", "最低价", "史上最低", "国家级",
    "世界级", "冠军", "独家", "王牌", "首选", "万能", "神器", "极致", "顶级",
    "绝对", "唯一", "最好", "最强", "最佳", "极品", "销量第一", "全网第一",
    "价格说明", "划线价", "被比较价格", "专柜价", "吊牌价", "指导价", "原价",
    "标价", "销售价", "促销价", "活动价", "到手价", "券后价", "折扣价", "限时价",
    "特惠价", "退换", "郑重声明", "版权申明", "著作权",
)

ALIAS_LIMIT_UNITS = 48
CLASS_UNIT_WORDS = (("瓶", "瓶"), ("盒", "盒"), ("袋", "袋"), ("包", "包"),
                    ("箱", "箱"), ("罐", "罐"), ("桶", "桶"), ("个", "个"), ("套", "套"))

DEFAULT_LIQUID_HINTS = ("液体", "精油", "香水", "ml", "ML", "毫升", "喷")
DEFAULT_SOLID_HINTS = ("石膏", "香膏", "蜡烛", "摆件", "挂件")
LIQUID_HINTS = DEFAULT_LIQUID_HINTS
SOLID_HINTS = DEFAULT_SOLID_HINTS


def now() -> str:
    return datetime.now().astimezone().isoformat()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def units(value: str) -> int:
    return sum(1 if ord(char) < 128 else 2 for char in value)


def clean_alias(value: str) -> str:
    result = str(value or "")
    for term in BAD_TERMS:
        result = re.sub(re.escape(term), " ", result, flags=re.IGNORECASE)
    result = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9\s]", " ", result)
    return re.sub(r"\s+", " ", result).strip()


def truncate_units(value: str, limit: int = ALIAS_LIMIT_UNITS) -> str:
    output: list[str] = []
    used = 0
    for char in value:
        width = 1 if ord(char) < 128 else 2
        if used + width > limit:
            break
        output.append(char)
        used += width
    return "".join(output).rstrip()


def split_dimensions(source_attribute: str) -> list[str]:
    return [part for part in re.split(r"[;；]", str(source_attribute or "")) if part.strip()]


def stable_mapping_id(product_key: str, field: str, value: str) -> str:
    return hashlib.sha256(f"{product_key}|{field}|{value}".encode()).hexdigest()[:32]


def category_attribute(driver: str, product: dict) -> dict:
    """Evidence-based 商品属性 payload for CategoryGroupList."""
    text = " ".join([product.get("title") or ""]
                    + [s.get("sourceAttribute") or "" for s in product.get("skus") or []])
    if driver != "类别":
        raise KeyError(driver)
    solid = any(word in text for word in SOLID_HINTS) and not any(
        word in text for word in ("ml", "ML", "毫升", "精油", "香水"))
    hint = "固体" if solid else "液体"
    return {"componentCode": "prop-product-426474", "valueIds": [{"固体": "4268033", "液体": "4268032"}[hint]],
            "hint": hint}


def sale_unit(product: dict) -> str:
    counts: Counter = Counter()
    for sku in product.get("skus") or []:
        text = str(sku.get("sourceAttribute") or "")
        for word, label in CLASS_UNIT_WORDS:
            if word in text:
                counts[label] += 1
    return counts.most_common(1)[0][0] if counts else "个"


def normalize_image(path: Path, destination: Path, role: str, notes: list[str]) -> Path:
    with Image.open(path) as source:
        width, height = source.size
        suffix = path.suffix.lower()
        encoded = str(source.format or "").upper()
        if role in {"spu_main", "sku_main"}:
            if (width == height and 480 < width <= 1500 and suffix in {".jpg", ".jpeg", ".png"}
                    and encoded in {"JPEG", "PNG"} and path.stat().st_size < 3 * 1024 * 1024):
                return path
            notes.append(f"{role} {path.name} 非1:1或超尺寸（{width}x{height}），已按1024x1024白底PNG归一化")
            destination.parent.mkdir(parents=True, exist_ok=True)
            image = source.convert("RGB")
            image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (1024, 1024), "white")
            canvas.paste(image, ((1024 - image.width) // 2, (1024 - image.height) // 2))
            canvas.save(destination, format="PNG", optimize=True)
            return destination
        errors, warnings = cli.validate_image({"role": role}, path)
        notes.extend(warnings)
        return path

def build_candidate(item_id: str, product: dict, group: dict, codes: dict) -> tuple[dict, dict]:
    category_id = int(product["categoryId"])
    rule = CATEGORY_RULES.get(category_id)
    if not rule:
        raise RuntimeError(f"{item_id} 类目 {category_id} 不在允许的 JoySpace 类目表内")
    product_key = f"JB{category_id}-{item_id}"
    notes: list[str] = []
    candidate_dir = ROOT / "candidates" / item_id

    main_paths = [Path(row["compliant"]) for row in group["groups"]["spu_main"]]
    sku_image_queue = [Path(row["compliant"]) for row in group["groups"]["sku_main"]]
    detail_paths = [Path(row["compliant"]) for row in group["groups"]["detail"]]
    if not main_paths:
        raise RuntimeError(f"{item_id} 缺少主图")
    if not detail_paths:
        raise RuntimeError(f"{item_id} 缺少真实详情图（禁止用主图兜底）")
    for path in [*main_paths, *detail_paths, *sku_image_queue]:
        if not path.is_file():
            raise RuntimeError(f"{item_id} 合规图缺失：{path}")

    main_images = [str(normalize_image(path, candidate_dir / "normalized" / f"main-{i:02d}.png",
                                       "spu_main", notes))
                   for i, path in enumerate(main_paths, 1)]
    detail_images = [str(normalize_image(path, candidate_dir / "normalized" / f"detail-{i:03d}.png",
                                         "detail", notes))
                     for i, path in enumerate(detail_paths, 1)]

    alias_rows = []
    for sku in product["skus"]:
        dims = split_dimensions(sku.get("sourceAttribute"))
        style_raw = dims[0] if dims else ""
        size_raw = dims[1] if len(dims) > 1 else ""
        alias_rows.append({
            "source": sku,
            "style": truncate_units(clean_alias(style_raw)),
            "size": truncate_units(clean_alias(size_raw)) if size_raw else "",
        })
    include_size = any(row["size"] for row in alias_rows)

    repairs = []
    counts = Counter((row["style"], row["size"]) for row in alias_rows)
    seen: Counter = Counter()
    for row in alias_rows:
        key = (row["style"], row["size"])
        if counts[key] > 1:
            seen[key] += 1
            if seen[key] > 1:
                target = "size" if row["size"] else "style"
                before = row[target]
                budget = ALIAS_LIMIT_UNITS - units(f" {seen[key]}")
                row[target] = f"{truncate_units(before, budget)} {seen[key]}"
                repairs.append({"sourceIndex": row["source"]["sourceIndex"], "field": target,
                                "before": before, "after": row[target],
                                "reason": "源表存在完全相同的销售属性组合，为保留全部真实SKU做技术性区分"})

    style_order = {alias: index for index, alias in enumerate(
        dict.fromkeys(row["style"] for row in alias_rows), start=1)}
    size_order = {alias: index for index, alias in enumerate(
        dict.fromkeys(row["size"] for row in alias_rows if row["size"]), start=1)}

    sku_rows = []
    for row in alias_rows:
        source = row["source"]
        source_index = int(source["sourceIndex"])
        spu_code = codes.get(f"spu:{item_id}")
        vendor_sku = codes.get(f"sku:{item_id}:{source_index}")
        if not spu_code or not vendor_sku:
            raise RuntimeError(f"{item_id}/{source_index} 缺少已生成的随机编码")
        fallback = True
        image_path = None
        if source.get("skuImage") and sku_image_queue:
            image_path = sku_image_queue.pop(0)
            fallback = False
        plan = component_source_plan(source["sourcePrice"], product["pricing"])
        sku_rows.append({
            "sourceIndex": source_index,
            "sourceSku": source_index,
            "sourceAttribute": source.get("sourceAttribute"),
            "sourcePrice": str(source["sourcePrice"]),
            "targetArrivalPrice": str(plan.target_final_price),
            "jdPrice": str(plan.jd_price),
            "stock": STOCK,
            "vendorSku": vendor_sku,
            "saleAlias": row["style"],
            "styleAlias": row["style"],
            "sizeAlias": row["size"],
            "styleOrder": style_order[row["style"]],
            "sizeOrder": size_order.get(row["size"], 0),
            "styleMappingId": stable_mapping_id(product_key, "style", row["style"]),
            "sizeMappingId": stable_mapping_id(product_key, "size", row["size"]) if row["size"] else "",
            "skuImageFallback": fallback,
            "mainImage": (main_images[0] if fallback else str(normalize_image(
                image_path, candidate_dir / "normalized" / f"sku-{source_index:03d}.png",
                "sku_main", notes))),
        })
    if any(row["skuImageFallback"] for row in sku_rows):
        notes.append("部分SKU源表没有规格图，按类目常规做法使用首张SPU主图作为该SKU主图")

    attribute_props = rule.get("categoryAttributeProps") or []
    attributes = [category_attribute(prop, product) for prop in attribute_props]
    attribute = attributes[0] if attributes else None
    unit = sale_unit(product)
    candidate = {
        "sourceItemId": item_id,
        "sourceUrl": product.get("sourceUrl"),
        "sourceRow": product.get("sourceRow"),
        "categoryId": category_id,
        "categoryName": rule["name"],
        "keyword": product["keyword"],
        "title": product["title"],
        "itemNumber": codes.get(f"spu:{item_id}"),
        "brandId": -1,
        "sevenDayReturnCode": SEVEN_DAY_RETURN,
        "mainImages": main_images,
        "detailImages": detail_images,
        "detailImagesFallbackUsed": False,
        "saleComponentCode": rule["sale"]["style"]["componentCode"],
        "saleComponents": rule["sale"],
        "includeSizeSaleAttribute": include_size,
        "categoryAttributes": attributes,
        "saleUnit": unit,
        "logisticsValues": {"WareWeight": "0.500", "WareLength": "300",
                            "WareWide": "200", "WareHeight": "80"},
        "fieldEvidence": {
            "WareWeight": "本批默认包装估算值，非实测",
            "WareLength": "按类目实时Schema上限约束的默认包装估算值，非实测",
            "WareWide": "按类目实时Schema上限约束的默认包装估算值，非实测",
            "WareHeight": "按类目实时Schema上限约束的默认包装估算值，非实测",
            "SaleUnit": f"由源表SKU规格文字推导为 {unit}",
            "categoryAttributes": "prop-product-426474 类别为除味香氛类目实时Schema必填项，按标题与SKU文字推断",
        },
        "freightTemplateId": FREIGHT_TEMPLATE_ID,
        "omittedComponents": ["WarePromise"],
        "imageCompliance": {
            "status": "REVIEWED_PASS",
            "finalReport": str(ROOT / "reports" / "final-image-compliance-report.json"),
            "compliantMap": str(ROOT / "reports" / "compliant-image-map.json"),
            "originalFilesPreserved": True,
            "imageCountAndOrderPreserved": True,
        },
        "pricing": product["pricing"],
        "skus": sku_rows,
        "normalizationNotes": sorted(set(notes)),
    }
    evidence = {
        "sourceItemId": item_id,
        "sellableSkuCount": len(sku_rows),
        "aliasRepairs": repairs,
        "sourceExcludedRows": product.get("excludedSourceRows") or [],
        "dimensionCount": 2 if include_size else 1,
        "skuImageFallbackCount": sum(1 for row in sku_rows if row["skuImageFallback"]),
        "categoryAttribute": attribute,
    }
    return candidate, evidence

def static_validate(candidate: dict) -> list[str]:
    errors: list[str] = []
    if candidate["categoryId"] not in CATEGORY_RULES:
        errors.append("category-not-allowed")
    if not candidate["title"].startswith(candidate["keyword"] + " "):
        errors.append("title-prefix")
    if units(candidate["title"]) > 60:
        errors.append("title-length")
    for term in BAD_TERMS:
        if term.lower() in candidate["title"].lower():
            errors.append(f"title-term:{term}")
    if int(candidate["brandId"]) != -1:
        errors.append("brand")
    if candidate["detailImagesFallbackUsed"]:
        errors.append("detail-fallback")
    pairs = set()
    for sku in candidate["skus"]:
        key = (sku["styleAlias"], sku["sizeAlias"])
        if key in pairs:
            errors.append(f"duplicate-sale-pair:{sku['sourceIndex']}")
        pairs.add(key)
        for field in ("styleAlias", "sizeAlias"):
            value = sku[field]
            if not value:
                continue
            if units(value) > 50:
                errors.append(f"alias-length:{field}:{sku['sourceIndex']}")
            if re.search(r"[^\u4e00-\u9fffa-zA-Z0-9\s]", value):
                errors.append(f"alias-chars:{field}:{sku['sourceIndex']}")
        if not sku["vendorSku"] or not re.fullmatch(r"[A-Za-z0-9\-]+", sku["vendorSku"]):
            errors.append(f"vendor-sku:{sku['sourceIndex']}")
        audit = component_source_audit(sku["sourcePrice"], sku["jdPrice"], candidate["pricing"])
        if not audit["ok"] or str(audit["expectedArrivalPrice"]) != str(sku["targetArrivalPrice"]):
            errors.append(f"price:{sku['sourceIndex']}")
    for text in [*candidate["mainImages"], *[row["mainImage"] for row in candidate["skus"]]]:
        path = Path(text)
        if not path.is_file():
            errors.append(f"image-missing:{text}")
            continue
        with Image.open(path) as image:
            if image.width != image.height or not 480 < image.width <= 1500:
                errors.append(f"main-image-size:{text}")
        if path.stat().st_size >= 3 * 1024 * 1024:
            errors.append(f"main-image-bytes:{text}")
    for text in candidate["detailImages"]:
        if not Path(text).is_file():
            errors.append(f"detail-missing:{text}")
    return errors


def prepare() -> None:
    ledger = load(ROOT / "ledger.json")
    codes = ledger["codes"]
    compliant = load(ROOT / "reports" / "compliant-image-map.json")
    results = []
    for item_id, group in sorted(compliant["items"].items()):
        source = ROOT / "candidates" / item_id / "source-normalized.json"
        if not source.is_file():
            continue
        product = load(source)
        destination = ROOT / "candidates" / item_id
        try:
            candidate, evidence = build_candidate(item_id, product, group, codes)
            errors = static_validate(candidate)
            save(destination / "candidate.json", candidate)
            save(destination / "source-eligibility.json", evidence)
            save(destination / "local-validation.json",
                 {"ok": not errors, "errors": errors, "validatedAt": now()})
            results.append({
                "sourceItemId": item_id,
                "status": "LOCAL_VALIDATED" if not errors else "FAILED_LOCAL_VALIDATION",
                "categoryId": candidate["categoryId"],
                "categoryName": candidate["categoryName"],
                "skuCount": len(candidate["skus"]),
                "dimensions": evidence["dimensionCount"],
                "mainImages": len(candidate["mainImages"]),
                "detailImages": len(candidate["detailImages"]),
                "aliasRepairs": len(evidence["aliasRepairs"]),
                "title": candidate["title"],
                "errors": errors,
            })
        except Exception as error:  # noqa: BLE001
            results.append({"sourceItemId": item_id, "status": "FAILED_LOCAL_BUILD", "error": str(error)})
    skipped_path = ROOT / "reports" / "skipped.json"
    video_path = ROOT / "reports" / "video-violations.json"
    summary = {
        "preparedAt": now(),
        "shop": CONFIG.get("shopName"),
        "venderId": CONFIG.get("venderId"),
        "profile": PROFILE,
        "candidateCount": sum(row["status"] == "LOCAL_VALIDATED" for row in results),
        "failedLocalValidationCount": sum(row["status"] == "FAILED_LOCAL_VALIDATION" for row in results),
        "failedLocalBuildCount": sum(row["status"] == "FAILED_LOCAL_BUILD" for row in results),
        "categoryCounts": dict(Counter(f"{row.get('categoryId')} {row.get('categoryName')}"
                                       for row in results if row.get("categoryId"))),
        "skuTotal": sum(row.get("skuCount") or 0 for row in results),
        "maxSkuPerItem": max([row.get("skuCount") or 0 for row in results] or [0]),
        "skipped": load(skipped_path) if skipped_path.exists() else {},
        "videoFindings": load(video_path).get("summary") if video_path.exists() else {},
        "results": results,
    }
    save(ROOT / "reports" / "candidate-preparation.json", summary)
    print(json.dumps({key: summary[key] for key in summary if key != "results"}, ensure_ascii=False, indent=2))
    for row in results:
        if row["status"] != "LOCAL_VALIDATED":
            print(json.dumps(row, ensure_ascii=False))

def identity() -> dict:
    if ROOT is None:
        raise SystemExit("请先通过 --root 绑定批次目录（listing_batch.set_root）")
    return cli.verify_configured_store_identity(ROOT / "store-config.json", PROFILE)


def image_component(url: str, order: int, mapping_id: str = "0000000000") -> dict:
    return {
        "componentCode": "img",
        "properties": [
            cli.property_item("imgRefId", mapping_id),
            cli.property_item("colorId", mapping_id),
            cli.property_item("orderSort", str(order)),
            cli.property_item("imgUrl", url),
        ],
    }


def sku_image_component(url: str, order: int, mapping_id: str) -> dict:
    component = image_component(url, order, mapping_id)
    component["properties"].extend([
        cli.property_item("isGgt", 1 if order == 1 else 0),
        cli.property_item("isPrimary", 1 if order == 1 else 0),
    ])
    return component


def upload_path(path_text: str, cache: dict, cache_path: Path, reference: dict) -> str:
    path = Path(path_text)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest not in cache:
        response = cli.upload(reference["media"]["zoneId"], reference["media"]["defaultCategoryId"], path)
        if not response.get("imgUrl"):
            raise RuntimeError(f"图片上传未返回地址：{path}")
        cache[digest] = {**response, "localPath": str(path), "sha256": digest}
        save(cache_path, cache)
    return str(cache[digest]["imgUrl"])


def build_remote_schema(candidate: dict, reference: dict, cache: dict, cache_path: Path) -> list[dict]:
    main_images = [upload_path(path, cache, cache_path, reference) for path in candidate["mainImages"]]
    details = [upload_path(path, cache, cache_path, reference) for path in candidate["detailImages"]]
    components = [
        {"componentCode": "WareTitle", "value": candidate["title"]},
        {"componentCode": "WareBrand", "value": candidate["brandId"]},
        {"componentCode": "WareItemnum", "value": candidate["itemNumber"]},
        {"componentCode": "WareSevenDayReturn", "value": candidate["sevenDayReturnCode"]},
        {"componentCode": "WareSkuImg",
         "value": [image_component(url, index) for index, url in enumerate(main_images, 1)]},
        {
            "componentCode": "WareEditor",
            "value": [
                {
                    "componentCode": "detail",
                    "properties": [
                        cli.property_item("desc", "".join(
                            f"<p><img src='{cli.detail_image_url(url)}'/></p>" for url in details)),
                        cli.property_item("zhuangBaUseFlag", "0"),
                        cli.property_item("userAgent", "pc"),
                    ],
                }
            ],
        },
        {"componentCode": "WareTransport", "value": str(candidate["freightTemplateId"])},
        {"componentCode": "SaleUnit", "value": candidate["saleUnit"]},
    ]
    category_values = [{"componentCode": row["componentCode"],
                       "properties": [cli.property_item("attrValues", ",".join(row["valueIds"]))]}
                      for row in candidate.get("categoryAttributes") or []]
    if category_values:
        components.append({"componentCode": "CategoryGroupList", "value": category_values})
    for code, value in candidate["logisticsValues"].items():
        components.append({"componentCode": code, "value": str(value)})
    sale_definitions = {key: {"componentCode": value["componentCode"], "attrName": value["attrName"]}
                        for key, value in candidate["saleComponents"].items()}
    sku_components = []
    for sku in candidate["skus"]:
        sku_first = upload_path(sku["mainImage"], cache, cache_path, reference)
        gallery = [sku_first, *main_images[1:]]
        values = [
            {"componentCode": "SkuJdprice", "value": sku["jdPrice"]},
            {"componentCode": "SkuStockNum", "value": sku["stock"]},
            {"componentCode": "SkuVendorSku", "value": sku["vendorSku"]},
            {"componentCode": "SkuEnableStatus", "value": "1"},
            {"componentCode": "SkuMainImage",
             "value": [sku_image_component(url, index, sku.get("styleMappingId") or "0000000000")
                       for index, url in enumerate(gallery, 1)]},
        ]
        if candidate.get("saleComponentCode"):
            sale_values = cli.sale_attribute_components(
                {
                    "styleAlias": sku["styleAlias"],
                    "styleMappingId": sku["styleMappingId"],
                    "styleOrder": sku["styleOrder"],
                    "sizeAlias": sku["sizeAlias"],
                    "sizeMappingId": sku["sizeMappingId"],
                    "sizeOrder": sku["sizeOrder"],
                },
                bool(candidate.get("includeSizeSaleAttribute") and sku["sizeAlias"]),
                sale_definitions,
            )
            values.append({"componentCode": "VenderCategoryValue", "value": sale_values})
        sku_components.append({"componentCode": "SKU", "value": values})
    components.append({"componentCode": "SKUS", "value": sku_components})
    return components


def exact_remote_matches(candidate: dict) -> list[dict]:
    try:
        rows = pipeline.list_products(item_num=candidate["itemNumber"])
    except pipeline.PipelineError:
        response = cli.get(
            "jdo-product-status",
            "/rest/sp-product/v0/products",
            {
                "scopeSet": "productId,productName,productStatus,categoryId,itemNum,created,modified",
                "page": 1, "pageSize": 50, "sortBy": "created", "sortType": "asc",
                "itemNum": candidate["itemNumber"],
            },
        )
        pagination = response.get("paginationData") or {}
        if response.get("success") is True and not response.get("data") and int(pagination.get("totalItems") or 0) == 0:
            rows = []
        else:
            raise
    return [row for row in rows if str(row.get("itemNum") or "") == candidate["itemNumber"]]


def validate_one(source_id: str, confirm: str) -> dict:
    if confirm != "PUBLISH_TO_JD":
        raise RuntimeError("缺少发布确认字符串 PUBLISH_TO_JD；未上传图片")
    destination = ROOT / "candidates" / source_id
    candidate = load(destination / "candidate.json")
    local = load(destination / "local-validation.json")
    if not local.get("ok"):
        raise RuntimeError(f"本地校验未通过：{local.get('errors')}")
    identity()
    matches = exact_remote_matches(candidate)
    if matches:
        raise RuntimeError(f"远端已存在技术货号 {candidate['itemNumber']}：{matches}")
    reference = load(ROOT / CATEGORY_RULES[candidate["categoryId"]]["reference"])
    cache_path = ROOT / "image-upload-cache.json"
    cache = load(cache_path) if cache_path.exists() else {}
    components = build_remote_schema(candidate, reference, cache, cache_path)
    save(destination / "schema.json", components)
    body = {
        "scene": "pop",
        "schema": json.dumps(components, ensure_ascii=False, separators=(",", ":")),
        "productApplyDTO": {
            "brandId": candidate["brandId"],
            "categoryDTO": {"lastCategoryId": candidate["categoryId"]},
        },
    }
    body_path = destination / "validation-body.json"
    save(body_path, body)
    response = cli.run_jdo_large(
        url="/rest/sp-product/v0/product-schemas/validations",
        method="POST", body_path=body_path, skill_id="jdo-product-publish", yes=True,
    )
    save(destination / "validation-response.json", response)
    violations = (response.get("data") or {}).get("wareKeeperDetectResultList") or []
    blocking = [row for row in violations if int(row.get("detectResult") or 0) == 1]
    state = {
        "sourceItemId": source_id,
        "stage": "JD_VALIDATED" if not blocking else "JD_VALIDATION_BLOCKED",
        "validatedAt": now(),
        "blockingViolations": blocking,
    }
    save(destination / "state.json", state)
    return state


def publish_one(source_id: str, confirm: str) -> dict:
    if confirm != "PUBLISH_TO_JD":
        raise RuntimeError("缺少发布确认字符串 PUBLISH_TO_JD")
    destination = ROOT / "candidates" / source_id
    candidate = load(destination / "candidate.json")
    state_path = destination / "state.json"
    state = load(state_path)
    if state.get("stage") != "JD_VALIDATED":
        raise RuntimeError(f"{source_id} 尚未通过京东校验：{state.get('stage')}")
    identity()
    matches = exact_remote_matches(candidate)
    if matches:
        state.update(stage="REMOTE_EXISTING_REQUIRES_VERIFICATION", remoteMatches=matches, updatedAt=now())
        save(state_path, state)
        return state
    body = load(destination / "validation-body.json")
    body["productApplyDTO"] = {
        "categoryDTO": {"lastCategoryId": candidate["categoryId"]},
        "saveActionType": 1,
    }
    body_path = destination / "publish-body.json"
    save(body_path, body)
    state.update(stage="SUBMITTING", attemptedAt=now(),
                 payloadSha256=hashlib.sha256(body_path.read_bytes()).hexdigest(),
                 itemNumber=candidate["itemNumber"])
    save(state_path, state)
    try:
        response = cli.run_jdo_large(
            url="/rest/sp-product/v0/product-applies",
            method="POST", body_path=body_path, skill_id="jdo-product-publish", yes=True,
        )
    except Exception as error:  # noqa: BLE001
        state.update(stage="SUBMISSION_RESULT_REQUIRES_RECONCILIATION", error=str(error), updatedAt=now())
        save(state_path, state)
        return state
    save(destination / "publish-response.json", response)
    remote = response.get("data") or {}
    state.update(stage="SUBMITTED_AWAITING_VERIFICATION", response=response,
                 productId=remote.get("productId"), applyId=remote.get("applyId"), updatedAt=now())
    save(state_path, state)
    return state

def normalized_url(url: str) -> str:
    return "jfs/" + str(url).split("jfs/", 1)[1] if "jfs/" in str(url) else str(url)


def verify_one(source_id: str, attempts: int = 8, delay: int = 10) -> dict:
    destination = ROOT / "candidates" / source_id
    candidate = load(destination / "candidate.json")
    state_path = destination / "state.json"
    state = load(state_path)
    product_id = int(state.get("productId")
                     or ((state.get("response") or {}).get("data") or {}).get("productId") or 0)
    if not product_id:
        matches = exact_remote_matches(candidate)
        if len(matches) == 1:
            product_id = int(matches[0]["productId"])
            state.update(productId=product_id, stage="REMOTE_RECONCILED_AWAITING_VERIFICATION")
            save(state_path, state)
        else:
            raise RuntimeError(f"{source_id} 没有唯一远端商品可核验")
    identity()
    apply_id = str(state.get("applyId") or product_id)
    application = cli.get("jdo-product-update", "/rest/sp-product/v0/product-apply/{applyId}",
                          {"applyId": apply_id, "scene": "pop"})
    save(destination / "application-detail.json", application)
    apply_info = (application.get("data") or {}).get("applyInfo") or {}
    if int(apply_info.get("draftComputeStatus") or 0) == 3:
        state.update(stage="PRODUCT_APPLY_REJECTED", rejection=apply_info, updatedAt=now())
        save(state_path, state)
        return state
    response = None
    last_error = None
    for attempt in range(attempts):
        try:
            response = cli.get("jdo-product-status", "/rest/sp-product/v0/products/{productId}",
                               {"productId": product_id, "scene": "pop"})
            if response.get("data"):
                break
        except Exception as error:  # noqa: BLE001
            last_error = str(error)
        if attempt + 1 < attempts:
            time.sleep(delay)
    if not response or not response.get("data"):
        state.update(stage="PRODUCT_PROPAGATION_PENDING", lastReadError=last_error, updatedAt=now())
        save(state_path, state)
        return state
    save(destination / "product-detail.json", response)
    detail = response["data"]
    info = detail["productInfo"]
    skus = detail["skuList"]
    stocks = cli.query_sku_stocks([int(sku["skuId"]) for sku in skus])
    save(destination / "stock-response.json", stocks)
    stock_by_sku = {int(row["skuId"]): int(row["stockNum"]) for row in stocks}
    actual_by_code = {str(sku.get("outerId") or ""): sku for sku in skus}
    errors: list[str] = []
    if info.get("itemNum") != candidate["itemNumber"] or int(info["brandInfo"]["brandId"]) != -1:
        errors.append("货号或品牌不一致")
    if (int(info["categoryDetail"]["lastCategoryId"]) != candidate["categoryId"]
            or info["productName"] != candidate["title"]):
        errors.append("类目或标题不一致")
    if int(info["productStatus"].get("productStatusNew") or 0) != 103:
        errors.append("商品未上架")
    if set(actual_by_code) != {row["vendorSku"] for row in candidate["skus"]}:
        errors.append("SKU数量或商家编码不一致")
    price_audits = []
    for expected in candidate["skus"]:
        actual = actual_by_code.get(expected["vendorSku"])
        if not actual:
            continue
        audit = component_source_audit(expected["sourcePrice"], actual["priceInfo"]["jdPrice"], candidate["pricing"])
        price_audits.append({"skuId": actual["skuId"], **audit})
        if not audit["ok"]:
            errors.append(f"价格不一致:{actual['skuId']}")
        if stock_by_sku.get(int(actual["skuId"])) != STOCK:
            errors.append(f"库存不一致:{actual['skuId']}")
        if (int(actual.get("skuEnableStatus") or 0) != 1
                or int((actual.get("skuStatus") or {}).get("onOffShelfStatus") or 0) != 1):
            errors.append(f"SKU未启用或未上架:{actual['skuId']}")
        if candidate.get("saleComponentCode"):
            aliases = cli.sale_aliases(actual)
            style_attr = candidate["saleComponents"]["style"]["componentCode"].removeprefix("sale-prop-")
            if cli.normalized_sale_alias(aliases.get(style_attr, "")) != cli.normalized_sale_alias(expected["styleAlias"]):
                errors.append(f"颜色别名不一致:{actual['skuId']}")
            if candidate.get("includeSizeSaleAttribute") and expected["sizeAlias"]:
                size_attr = candidate["saleComponents"]["size"]["componentCode"].removeprefix("sale-prop-")
                if cli.normalized_sale_alias(aliases.get(size_attr, "")) != cli.normalized_sale_alias(expected["sizeAlias"]):
                    errors.append(f"尺码别名不一致:{actual['skuId']}")
    cache = load(ROOT / "image-upload-cache.json")

    def uploaded(path_text: str) -> str:
        digest = hashlib.sha256(Path(path_text).read_bytes()).hexdigest()
        return normalized_url(cache[digest]["imgUrl"])

    expected_details = [uploaded(path) for path in candidate["detailImages"]]
    description = info.get("productDetailDesc") or {}
    for field in ("desc", "mobileDesc"):
        urls = re.findall("".join(["<img[^>]+src=[\x27\x22]([^\x27\x22]+)[\x27\x22]"]), description.get(field, ""))
        if [normalized_url(url) for url in urls] != expected_details:
            errors.append(f"详情图数量或顺序不一致:{field}")
    dimensions = {"WareWeight": "weight", "WareLength": "length", "WareWide": "width", "WareHeight": "height"}
    for code, expected in candidate["logisticsValues"].items():
        if float(info.get(dimensions[code], -1)) != float(expected):
            errors.append(f"包装字段不一致:{code}")
    result = {
        "sourceItemId": source_id,
        "productId": product_id,
        "verifiedAt": now(),
        "ok": not errors,
        "skuCount": len(skus),
        "skuIds": [sku["skuId"] for sku in skus],
        "errors": errors,
        "priceAudits": price_audits,
        "detailImageCount": len(expected_details),
    }
    save(destination / "verification.json", result)
    state.update(stage="VERIFIED" if result["ok"] else "VERIFICATION_MISMATCH",
                 productId=product_id, verification=result, updatedAt=now())
    save(state_path, state)
    return state


AWAITING_VERIFICATION = {
    "SUBMITTED_AWAITING_VERIFICATION",
    "REMOTE_EXISTING_REQUIRES_VERIFICATION",
    "REMOTE_RECONCILED_AWAITING_VERIFICATION",
    "PRODUCT_PROPAGATION_PENDING",
    "VERIFICATION_MISMATCH",
    "PRODUCT_APPLY_REJECTED",
}


def run_batch(confirm: str) -> None:
    if confirm != "PUBLISH_TO_JD":
        raise RuntimeError("请使用 --confirm PUBLISH_TO_JD；未上传或发布任何内容")
    identity()
    preparation = load(ROOT / "reports" / "candidate-preparation.json")
    source_ids = [row["sourceItemId"] for row in preparation["results"] if row["status"] == "LOCAL_VALIDATED"]
    submission_results = []
    for source_id in source_ids:
        try:
            state_path = ROOT / "candidates" / source_id / "state.json"
            state = load(state_path) if state_path.exists() else {}
            if state.get("stage") == "VERIFIED":
                submission_results.append({"sourceItemId": source_id, "stage": "VERIFIED", "resumed": True})
                continue
            if state.get("stage") in {"SUBMITTING", "SUBMISSION_RESULT_REQUIRES_RECONCILIATION"}:
                candidate = load(ROOT / "candidates" / source_id / "candidate.json")
                matches = exact_remote_matches(candidate)
                if len(matches) == 1:
                    state.update(stage="REMOTE_RECONCILED_AWAITING_VERIFICATION",
                                 productId=int(matches[0]["productId"]),
                                 applyId=str(matches[0]["productId"]), updatedAt=now())
                    save(state_path, state)
                else:
                    state.update(stage="SUBMISSION_RESULT_REQUIRES_RECONCILIATION", updatedAt=now())
                    save(state_path, state)
                    submission_results.append({"sourceItemId": source_id, "stage": state["stage"],
                                               "error": "写入结果尚不明确，未重放提交"})
                    print(json.dumps({"phase": "submit", **submission_results[-1]}, ensure_ascii=False), flush=True)
                    continue
            if not state.get("stage") or state.get("stage") in {"JD_VALIDATION_BLOCKED", "JD_VALIDATED"}:
                state = validate_one(source_id, confirm)
            if state.get("stage") == "JD_VALIDATED":
                state = publish_one(source_id, confirm)
            submission_results.append({"sourceItemId": source_id, "stage": state.get("stage"),
                                       "productId": state.get("productId")})
            print(json.dumps({"phase": "submit", **submission_results[-1]}, ensure_ascii=False), flush=True)
        except Exception as error:  # noqa: BLE001
            result = {"sourceItemId": source_id, "stage": "FAILED_ITEM", "error": str(error)}
            submission_results.append(result)
            print(json.dumps({"phase": "submit", **result}, ensure_ascii=False), flush=True)
    save(ROOT / "reports" / "publish-batch-submissions.json",
         {"updatedAt": now(), "results": submission_results})

    verification_results = []
    for source_id in source_ids:
        try:
            state_path = ROOT / "candidates" / source_id / "state.json"
            if not state_path.exists():
                raise RuntimeError("没有可核验的提交状态")
            state = load(state_path)
            if state.get("stage") == "VERIFIED":
                verification_results.append({"sourceItemId": source_id, "stage": "VERIFIED", "resumed": True})
                continue
            if state.get("stage") not in AWAITING_VERIFICATION:
                raise RuntimeError(f"当前阶段不可核验：{state.get('stage')}")
            state = verify_one(source_id)
            verification_results.append({"sourceItemId": source_id, "stage": state.get("stage"),
                                         "productId": state.get("productId")})
            print(json.dumps({"phase": "verify", **verification_results[-1]}, ensure_ascii=False), flush=True)
        except Exception as error:  # noqa: BLE001
            result = {"sourceItemId": source_id, "stage": "FAILED_ITEM", "error": str(error)}
            verification_results.append(result)
            print(json.dumps({"phase": "verify", **result}, ensure_ascii=False), flush=True)
    save(ROOT / "reports" / "publish-batch-results.json",
         {"updatedAt": now(), "submissions": submission_results, "verifications": verification_results})
    print(json.dumps({
        "submittedPhaseCount": len(submission_results),
        "verified": sum(row.get("stage") == "VERIFIED" for row in verification_results),
        "verificationCount": len(verification_results),
    }, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "validate", "publish", "verify", "run"])
    parser.add_argument("--source-id")
    parser.add_argument("--confirm", default="")
    ni_batch.add_root_argument(parser)
    args = parser.parse_args()
    set_root(args.root)
    if args.command == "prepare":
        prepare()
    elif args.command == "validate":
        print(json.dumps(validate_one(args.source_id, args.confirm), ensure_ascii=False, indent=2))
    elif args.command == "publish":
        print(json.dumps(publish_one(args.source_id, args.confirm), ensure_ascii=False, indent=2))
    elif args.command == "verify":
        print(json.dumps(verify_one(args.source_id), ensure_ascii=False, indent=2))
    else:
        run_batch(args.confirm)


if __name__ == "__main__":
    main()
