# -*- coding: utf-8 -*-
"""TOP销量SKU聚合草稿模式（只保存草稿，绝不上架）。

按类目近30天订单销量（orderQty30d）汇总排名选 TOP N SPU，各取最高销量 SKU，
聚合成一条新链接草稿：单一「颜色」维度、每 SKU 多图、价格严格等于源 SKU、
每 SKU 独立详情（默认生成 HTML+映射+京麦客户端人工步骤）。

硬边界：
- 只保存草稿：saveActionType=0，回读 productStatus=105；绝不点「提交并上架」。
- SKU 维度详情仅京麦客户端人工设置；本脚本不包含网页自动化/Playwright/CDP。
- 提交前必须有确认清单哈希；已有 applyId、SUBMITTING、CREATED 一律停止。
- 结果未知先按 applyId/标题+时间回查，确认未创建才允许 --retry。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable

SKILL_VERSION = "1.3.0"
MODE = "TOP_SKU_AGGREGATION_DRAFT"
DRAFT_STATUS = 105
ON_SALE_STATUS = 103
REQUIRED_CONFIG = ("venderId", "shopName", "jdoProfile", "categoryId", "title", "topSkus")
LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile")
THIRD_PARTY_MARKERS = (
    "taobao.com", "tmall.com", "pinduoduo", "yangkeduo.com",
    "拼多多", "淘宝", "天猫", "抖音", "快手", "小红书",
    "微信", "二维码", "扫码", "qq.com",
)


class AggregationError(RuntimeError):
    pass


class ConfigError(AggregationError):
    pass


class BuildError(AggregationError):
    pass


class ConsistencyError(AggregationError):
    pass


class SubmitBlocked(AggregationError):
    pass


class VerifyError(AggregationError):
    pass


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _read_json(path: Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise AggregationError(f"JSON 解析失败: {path}: {exc}") from None


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise AggregationError(f"非法金额: {value!r}") from None


def normalize_image_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if text.startswith("//"):
        return "https:" + text
    return "https://img10.360buyimg.com/imgzone/" + text.lstrip("/")


def feature_value(features: list[dict] | None, key: str) -> Any:
    for item in features or []:
        if item.get("key") == key:
            return item.get("value")
    return None


def public_images(product: dict) -> list[dict]:
    for group in (product.get("material") or {}).get("mainImages") or []:
        if str(group.get("uuid")) == "0000000000":
            return sorted(group.get("imageInfoList") or [],
                          key=lambda item: int(item.get("orderSort") or 0))
    return []


def color_attr_id(sku: dict) -> str:
    for attr in sku.get("saleAttrs") or []:
        if attr.get("attrName") == "颜色":
            return str(attr.get("attrId"))
    raise BuildError(f"参照 SKU {sku.get('skuId')} 缺少「颜色」销售属性，无法挂 SKU 图")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_mover():
    """Import the shared jdo bridge lazily so build/verify tests never need the CLI."""
    import win_no_window  # noqa: F401  (must precede jdo_listing_mvp usage)
    import jdo_listing_mvp as jm
    return jm


def _normalize_top_skus(top: list, top_n: int) -> list[dict]:
    if not top:
        raise ConfigError("topSkus 必须是非空数组")
    if len(top) != top_n:
        raise ConfigError(f"topSkus 数量 {len(top)} 与 topN {top_n} 不一致")
    seen_specs: set[str] = set()
    seen_skus: set[str] = set()
    seen_n: set[int] = set()
    normalized: list[dict] = []
    for index, item in enumerate(top, 1):
        if not isinstance(item, dict):
            raise ConfigError(f"topSkus[{index}] 必须是对象")
        for key in ("n", "pid", "skuId", "price", "spec"):
            if item.get(key) in (None, ""):
                raise ConfigError(f"topSkus[{index}] 缺少 {key}")
        try:
            number = int(item["n"])
        except (TypeError, ValueError):
            raise ConfigError(f"topSkus[{index}] n 必须是整数") from None
        if number in seen_n:
            raise ConfigError(f"topSkus 序号重复: {number}")
        seen_n.add(number)
        spec = str(item["spec"]).strip()
        if not spec:
            raise ConfigError(f"topSkus[{index}] spec 不能为空")
        if any(ch in spec for ch in "[]\r\n,"):
            raise ConfigError(f"规格值不能包含方括号、逗号或换行: {spec}")
        if len(spec) > 60:
            raise ConfigError(f"规格值过长（>60 字）: {spec}")
        if spec in seen_specs:
            raise ConfigError(f"规格值重复: {spec}")
        seen_specs.add(spec)
        sku_id = str(item["skuId"]).strip()
        if sku_id in seen_skus:
            raise ConfigError(f"参照 SKU 重复: {sku_id}")
        seen_skus.add(sku_id)
        try:
            price = money(item["price"])
        except AggregationError:
            raise ConfigError(f"topSkus[{index}] price 不是合法金额: {item['price']}") from None
        if price <= 0:
            raise ConfigError(f"topSkus[{index}] price 必须大于 0")
        normalized.append({
            "n": number,
            "pid": str(item["pid"]).strip(),
            "skuId": sku_id,
            "price": f"{price:.2f}",
            "spec": spec,
        })
    return normalized

def validate_config(cfg: dict, *, require_top_skus: bool = True) -> dict:
    if not isinstance(cfg, dict):
        raise ConfigError("task_config.json 必须是 JSON 对象")
    required = [key for key in REQUIRED_CONFIG if key != "title"]
    missing = [key for key in required if key not in cfg]
    if missing:
        raise ConfigError("task_config.json 缺少字段: " + ", ".join(missing))
    if require_top_skus and "title" not in cfg:
        raise ConfigError("task_config.json 缺少字段: title")
    profile = str(cfg["jdoProfile"]).strip().lower()
    if not profile:
        raise ConfigError("jdoProfile 不能为空")
    if profile == "default":
        raise ConfigError("禁止使用 default Profile，请使用店铺专属 Profile")
    shop_name = str(cfg["shopName"]).strip()
    if not shop_name:
        raise ConfigError("shopName 不能为空")
    try:
        vender_id = int(cfg["venderId"])
        category_id = int(cfg["categoryId"])
        top_n = int(cfg.get("topN", 5))
        stock = int(cfg.get("stock", 100))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"venderId/categoryId/topN/stock 必须是整数: {exc}") from None
    if vender_id <= 0 or category_id <= 0:
        raise ConfigError("venderId/categoryId 必须为正整数")
    if not 1 <= top_n <= 20:
        raise ConfigError("topN 必须在 1..20")
    if stock <= 0:
        raise ConfigError("stock 必须为正整数")
    title = str(cfg.get("title") or "").strip()
    if require_top_skus and not title:
        raise ConfigError("title 不能为空")
    if cfg.get("draftOnly", True) is not True:
        raise ConfigError("本模式只保存草稿，draftOnly 必须为 true")
    top = cfg.get("topSkus") or []
    if not isinstance(top, list):
        raise ConfigError("topSkus 必须是数组")
    normalized = _normalize_top_skus(top, top_n) if require_top_skus else []
    result = dict(cfg)
    result.update({
        "venderId": vender_id,
        "categoryId": category_id,
        "topN": top_n,
        "stock": stock,
        "title": title,
        "jdoProfile": profile,
        "shopName": shop_name,
        "topSkus": normalized,
        "draftOnly": True,
    })
    return result


def _attr_signature(product_info: dict) -> list[list[Any]]:
    rows = []
    for group in product_info.get("goodsAttrInfos") or []:
        values = sorted(str(value.get("attrValue")) for value in group.get("values") or [])
        rows.append([int(group.get("attrId") or 0), values])
    return sorted(rows)


def consistency_report(sources: dict, cfg: dict) -> dict:
    rows = []
    for pid in sorted(sources, key=lambda value: int(value)):
        product_info = (sources[pid] or {}).get("productInfo") or {}
        rows.append({
            "pid": str(pid),
            "brandId": int((product_info.get("brandInfo") or {}).get("brandId", -1)),
            "categoryId": int((product_info.get("categoryDetail") or {}).get("lastCategoryId") or 0),
            "attrs": _attr_signature(product_info),
        })
    brand_ids = sorted({row["brandId"] for row in rows})
    category_ids = sorted({row["categoryId"] for row in rows})
    attr_signatures = sorted({_canonical(row["attrs"]) for row in rows})
    problems = []
    if len(brand_ids) > 1:
        problems.append(f"brandId 不一致: {brand_ids}")
    if len(category_ids) > 1:
        problems.append(f"categoryId 不一致: {category_ids}")
    if category_ids and category_ids != [int(cfg["categoryId"])]:
        problems.append(f"源商品类目 {category_ids} 与配置 categoryId {cfg['categoryId']} 不一致")
    if len(attr_signatures) > 1:
        problems.append("关键类目属性 goodsAttrInfos 不一致")
    return {
        "rows": rows,
        "brandIds": brand_ids,
        "categoryIds": category_ids,
        "categoryAttrsConsistent": len(attr_signatures) <= 1,
        "problems": problems,
    }


def assert_consistency(report: dict, cfg: dict) -> None:
    if not report.get("problems"):
        return
    override = cfg.get("consistencyOverride") or {}
    if override.get("confirmed") is True and str(override.get("reason") or "").strip():
        return
    raise ConsistencyError(
        "聚合前业务一致性检查未通过：\n- " + "\n- ".join(report["problems"]) +
        "\n如确认仍要聚合，请在 task_config.json 写入 "
        'consistencyOverride={"confirmed": true, "reason": "..."} 后重跑 build。'
    )

def prop(key: str, value: Any) -> dict:
    return {"key": key, "value": value}


def build_components(cfg: dict, sources: dict, mapping: dict) -> dict:
    top = cfg["topSkus"]
    category_id = int(cfg["categoryId"])
    stock = int(cfg["stock"])
    title = cfg["title"]
    warnings: list[str] = []
    if len(title) > 60:
        warnings.append(f"标题长度 {len(title)} 超过 60，平台可能拒绝")

    index = {}
    for pid, source in sources.items():
        for sku in (source or {}).get("skuList") or []:
            index[str(sku["skuId"])] = (str(pid), sku)

    for item in top:
        mapping.setdefault(f"spec:{item['spec']}", str(uuid.uuid4()))
        mapping.setdefault(f"skuuuid:{item['skuId']}", str(uuid.uuid4()))

    if str(top[0]["pid"]) not in sources:
        raise BuildError(f"TOP1 源商品 {top[0]['pid']} 不在 source_applies.json")
    top1 = sources[str(top[0]["pid"])]
    top1_info = top1.get("productInfo") or {}
    brand_id = int((top1_info.get("brandInfo") or {}).get("brandId", -1))
    if brand_id < 0:
        raise BuildError("TOP1 源商品缺少有效 brandId")
    logistics = top1_info.get("logisticsInfo") or {}
    freight = str(logistics.get("transportId") or feature_value(top1_info.get("features"), "fare") or "")
    if not freight:
        raise BuildError("TOP1 源商品缺少运费模板 transportId")
    promise_id = logistics.get("promiseId")
    if promise_id in (None, ""):
        raise BuildError("TOP1 源商品缺少 promiseId（承诺）")
    seven_day = feature_value(top1_info.get("features"), "is7ToReturn") or "1"
    dims = {
        "WareLength": top1_info.get("length"),
        "WareWide": top1_info.get("width"),
        "WareHeight": top1_info.get("height"),
        "WareWeight": top1_info.get("weight"),
    }
    for key, value in dims.items():
        if value in (None, ""):
            raise BuildError(f"TOP1 源商品缺少 {key}")

    category_group = [
        {"componentCode": f"prop-product-{group.get('attrId')}",
         "properties": [prop("attrValues", ",".join(str(value["attrValue"]) for value in group.get("values") or []))]}
        for group in (top1_info.get("goodsAttrInfos") or [])
    ]
    if not category_group:
        raise BuildError("TOP1 源商品缺少类目属性 goodsAttrInfos")

    spu_images = [normalize_image_url(image.get("imgUrl")) for image in public_images(top1)]
    spu_images = [url for url in spu_images if url]
    if len(spu_images) < 5:
        raise BuildError(f"TOP1 公共主图不足 5 张: {len(spu_images)}")

    pc_parts, mobile_parts, per_sku = [], [], {}
    for item in top:
        source = sources.get(str(item["pid"]))
        if not source:
            raise BuildError(f"源商品 {item['pid']} 不在 source_applies.json")
        detail = (source.get("productInfo") or {}).get("productDetailDesc") or {}
        pc = str(detail.get("desc") or "")
        mobile = str(detail.get("mobileDesc") or pc)
        if not pc.strip():
            raise BuildError(f"源商品 {item['pid']} 商品详情为空")
        per_sku[item["spec"]] = {"pc": pc, "mobile": mobile, "pid": str(item["pid"])}
        pc_parts.append(pc)
        mobile_parts.append(mobile)

    components = [
        {"componentCode": "WareTitle", "value": title},
        {"componentCode": "WareBrand", "value": brand_id},
        {"componentCode": "WareSkuImg", "value": [
            {"componentCode": "img", "properties": [
                prop("imgRefId", "0000000000"), prop("colorId", "0000000000"),
                prop("orderSort", str(position)), prop("imgUrl", url),
            ]} for position, url in enumerate(spu_images, start=1)]},
        {"componentCode": "WareLength", "value": str(dims["WareLength"])},
        {"componentCode": "WareWide", "value": str(dims["WareWide"])},
        {"componentCode": "WareHeight", "value": str(dims["WareHeight"])},
        {"componentCode": "WareWeight", "value": str(dims["WareWeight"])},
        {"componentCode": "WareEditor", "value": [
            {"componentCode": "detail", "properties": [
                prop("desc", "".join(pc_parts)), prop("zhuangBaUseFlag", "0"),
                prop("userAgent", "pc")]},
            {"componentCode": "detail", "properties": [
                prop("desc", "".join(mobile_parts)), prop("zhuangBaUseFlag", "0"),
                prop("userAgent", "mobile")]},
        ]},
        {"componentCode": "WareSevenDayReturn", "value": seven_day},
        {"componentCode": "WarePromise", "value": promise_id},
        {"componentCode": "CategoryGroupList", "value": category_group},
        {"componentCode": "WareTransport", "value": freight},
    ]

    sku_values, manifest_skus, csv_rows, detail_files = [], [], [], {}
    for item in top:
        if item["skuId"] not in index:
            raise BuildError(f"参照 SKU {item['skuId']} 不在源商品详情中")
        source_pid, source_sku = index[item["skuId"]]
        if source_pid != str(item["pid"]):
            raise BuildError(f"参照 SKU {item['skuId']} 归属 {source_pid} 与配置 {item['pid']} 不符")
        source_price = money((source_sku.get("priceInfo") or {}).get("jdPrice"))
        configured_price = money(item["price"])
        if source_price != configured_price:
            raise BuildError(
                f"价格与参照 SKU 不一致: {item['skuId']} 源={source_price} 配置={configured_price}")
        color_prop_id = color_attr_id(source_sku)
        spec_map = mapping[f"spec:{item['spec']}"]
        sku_uuid = mapping[f"skuuuid:{item['skuId']}"]

        sku_attr_value = []
        for group in source_sku.get("goodsAttrInfos") or []:
            values = ",".join(str(value["attrValue"]) for value in group.get("values") or [])
            sku_attr_value.append({
                "componentCode": f"prop-sku-{group.get('attrId')}",
                "properties": [prop("attrValues", f"[{values}]")]})
        if not sku_attr_value:
            raise BuildError(f"参照 SKU {item['skuId']} 缺少 SKU 级类目属性，当前类目可能不支持聚合")

        gallery = [normalize_image_url(image.get("imgUrl")) for image in public_images(sources[source_pid])]
        gallery = [url for url in gallery if url]
        logo = normalize_image_url(str(source_sku.get("logo") or ""))
        if logo and (not gallery or gallery[0] != logo):
            gallery = [logo] + [url for url in gallery if url != logo]
        if len(gallery) < 5:
            raise BuildError(f"{item['spec']} 图片不足 5 张: {len(gallery)}")

        sku_components = [
            {"componentCode": "SkuUUid", "value": sku_uuid},
            {"componentCode": "SkuJdprice", "value": f"{configured_price:.2f}"},
            {"componentCode": "SkuStockNum", "value": stock},
        ]
        short_title = feature_value(source_sku.get("features"), "shortTitle")
        if short_title:
            sku_components.append({"componentCode": "SkuShortTitle", "value": short_title})
        sku_components.extend([
            {"componentCode": "SkuAttr", "value": sku_attr_value},
            {"componentCode": "SkuEnableStatus", "value": "1"},
            {"componentCode": "SkuMainImage", "value": [
                {"componentCode": "img", "properties": [
                    prop("imgUrl", url), prop("isGgt", 1 if position == 1 else 0),
                    prop("isPrimary", 1 if position == 1 else 0), prop("imgRefId", spec_map),
                    prop("colorId", spec_map), prop("orderSort", position),
                ]} for position, url in enumerate(gallery, start=1)]},
            {"componentCode": "VenderCategoryValue", "value": [
                {"componentCode": f"sale-prop-{color_prop_id}", "properties": [
                    prop("attrValueAlias", f"[{item['spec']}]"),
                    prop("attrName", "颜色"), prop("mappingId", spec_map),
                ]}]},
        ])
        sku_values.append({"componentCode": "SKU", "value": sku_components})

        detail = per_sku[item["spec"]]
        pc_file = f"sku{item['n']}_detail_pc_{item['pid']}.html"
        mobile_file = f"sku{item['n']}_detail_mobile_{item['pid']}.html"
        detail_files[item["spec"]] = {
            "pc_name": pc_file, "mobile_name": mobile_file,
            "pc": detail["pc"], "mobile": detail["mobile"],
        }
        manifest_skus.append({
            "n": item["n"], "pid": str(item["pid"]), "skuId": item["skuId"],
            "price": f"{configured_price:.2f}", "spec": item["spec"],
            "detailFile": pc_file, "detailMobileFile": mobile_file,
            "detailLen": len(detail["pc"]), "images": len(gallery),
        })
        csv_rows.append([
            str(item["n"]), item["skuId"], str(item["pid"]),
            f"{source_price:.2f}", f"{configured_price:.2f}", item["spec"],
            str(len(gallery)), str(len(detail["pc"])), pc_file,
        ])

    components.append({"componentCode": "SKUS", "value": sku_values})
    schema_text = json.dumps(components, ensure_ascii=False, separators=(",", ":"))
    publish_body = {
        "scene": "pop",
        "schema": schema_text,
        "productApplyDTO": {
            "categoryDTO": {"lastCategoryId": category_id},
            "saveActionType": 0,
        },
    }
    validation_body = {
        "scene": "pop",
        "schema": schema_text,
        "productApplyDTO": {
            "brandId": brand_id,
            "categoryDTO": {"lastCategoryId": category_id},
        },
    }
    return {
        "components": components,
        "publish_body": publish_body,
        "validation_body": validation_body,
        "manifest_skus": manifest_skus,
        "csv_rows": csv_rows,
        "detail_files": detail_files,
        "warnings": warnings,
        "top1": {
            "pid": str(top[0]["pid"]),
            "brandId": brand_id,
            "categoryId": category_id,
            "length": dims["WareLength"], "width": dims["WareWide"],
            "height": dims["WareHeight"], "weight": dims["WareWeight"],
            "transportId": freight, "promiseId": promise_id,
            "sevenDayReturn": seven_day,
        },
    }


def scan_detail_markers(detail_files: dict) -> list[dict]:
    hits = []
    for spec, files in detail_files.items():
        text = (files.get("pc") or "") + "\n" + (files.get("mobile") or "")
        lowered = text.lower()
        for marker in THIRD_PARTY_MARKERS:
            if marker.lower() in lowered:
                hits.append({"spec": spec, "marker": marker})
    return hits


def _checklist_payload(cfg: dict, result: dict) -> dict:
    return {
        "mode": MODE,
        "skillVersion": SKILL_VERSION,
        "venderId": cfg["venderId"],
        "shopName": cfg["shopName"],
        "jdoProfile": cfg["jdoProfile"],
        "categoryId": cfg["categoryId"],
        "topN": cfg["topN"],
        "title": cfg["title"],
        "stock": cfg["stock"],
        "draftOnly": True,
        "saveActionType": 0,
        "skus": [
            {"n": sku["n"], "pid": sku["pid"], "skuId": sku["skuId"],
             "price": sku["price"], "spec": sku["spec"], "images": sku["images"]}
            for sku in result["manifest_skus"]
        ],
        "top1": result["top1"],
        "consistency": {
            "brandIds": result["consistency"]["brandIds"],
            "categoryIds": result["consistency"]["categoryIds"],
            "categoryAttrsConsistent": result["consistency"]["categoryAttrsConsistent"],
        },
        "detailMarkers": result["detail_review"]["markers"],
    }


def _checklist_markdown(cfg: dict, result: dict, digest: str) -> str:
    top1 = result["top1"]
    lines = [
        "# TOP销量SKU聚合草稿 — 确认清单",
        "",
        f"- 模式：{MODE}",
        f"- Skill 版本：{SKILL_VERSION}",
        f"- 店铺：{cfg['shopName']}（venderId={cfg['venderId']}）",
        f"- Profile：{cfg['jdoProfile']}",
        f"- 类目：{cfg['categoryId']}",
        f"- TOP N：{cfg['topN']}",
        f"- 标题：{cfg['title']}",
        f"- 每 SKU 库存：{cfg['stock']}",
        "- 只保存草稿：是（saveActionType=0；绝不点「提交并上架」）",
        f"- 清单哈希：`{digest}`",
        "",
        "## 逐 SKU",
        "",
        "| 序号 | 规格（颜色） | 源商品ID | 参照SKU | 价格(元) | 图片数 | 详情文件 |",
        "|---|---|---|---|---|---|---|",
    ]
    for sku in result["manifest_skus"]:
        lines.append(
            f"| {sku['n']} | {sku['spec']} | {sku['pid']} | {sku['skuId']} | "
            f"{sku['price']} | {sku['images']} | {sku['detailFile']} |")
    lines += [
        "",
        "## TOP1 继承项（必须在确认时明示）",
        "",
        f"- 品牌 brandId：{top1['brandId']}",
        f"- 类目 categoryId：{top1['categoryId']}",
        f"- 包装尺寸：长 {top1['length']} / 宽 {top1['width']} / 高 {top1['height']}",
        f"- 重量：{top1['weight']}",
        f"- 运费模板 transportId：{top1['transportId']}",
        f"- 承诺 promiseId：{top1['promiseId']}",
        f"- 7天退货：{top1['sevenDayReturn']}",
        "",
        "## 业务一致性",
        "",
        f"- brandId：{result['consistency']['brandIds']}",
        f"- categoryId：{result['consistency']['categoryIds']}",
        f"- 关键类目属性一致：{result['consistency']['categoryAttrsConsistent']}",
    ]
    if result["consistency"]["problems"]:
        lines.append("- 覆盖确认：" + "；".join(result["consistency"]["problems"]))
    lines += ["", "## 源详情审核", ""]
    markers = result["detail_review"]["markers"]
    if markers:
        lines.append("- 命中第三方平台/联系方式/二维码等标记：")
        for hit in markers:
            lines.append(f"  - {hit['spec']}：{hit['marker']}")
        lines.append(f"- 已确认接受：{result['detail_review']['accepted']}")
    else:
        lines.append("- 未命中第三方平台/联系方式/二维码标记")
    lines += [
        "",
        "## SKU 维度详情（人工待办）",
        "",
        "- 详情模式：SPU",
        "- skuDetailManualPending：true",
        "- 待设置 SKU：" + "、".join(sku["spec"] for sku in result["manifest_skus"]),
        "- 客户端步骤见 `sku-detail-manual.md`；人工完成后运行 `verify` 回读 "
        "`skuList[].productDetailDesc.desc`。",
        "",
        "## 提交前确认",
        "",
        "请逐项确认：TOP N、规格命名、标题、库存、是否只保存草稿。",
        "确认后运行：`python -X utf8 scripts/top_sku_aggregation.py submit "
        f"--config task_config.json --confirm {digest[:8]}`",
    ]
    return "\n".join(lines) + "\n"


def _manual_markdown(cfg: dict, result: dict) -> str:
    lines = [
        "# 逐 SKU 详情 — 京麦客户端人工待办",
        "",
        "> SKU 维度详情（详情随 SKU 切换）仅京麦客户端人工设置。本模式不包含网页自动化、"
        "Playwright、CDP，也不探测 descModel。",
        "",
        "## 客户端步骤",
        "",
        "1. 在京麦客户端打开目标商品编辑页。",
        "2. 进入详情编辑器，切换到「详情随 SKU 切换」模式。",
        "3. 按下方清单逐个选择对应详情内容（HTML 文件已生成）。",
        "4. 保存后运行回读命令，确认每个 SKU 的 `skuList[].productDetailDesc.desc` 非空。",
        "5. 只有回读全部通过后，才把 `skuDetailManualPending` 视为完成。",
        "",
        "## 逐 SKU 映射",
        "",
        "| 序号 | 规格（颜色） | 源商品ID | 参照SKU | PC详情文件 | 手机详情文件 | 详情字符数 |",
        "|---|---|---|---|---|---|---|",
    ]
    for sku in result["manifest_skus"]:
        lines.append(
            f"| {sku['n']} | {sku['spec']} | {sku['pid']} | {sku['skuId']} | "
            f"{sku['detailFile']} | {sku['detailMobileFile']} | {sku['detailLen']} |")
    lines += [
        "",
        "## 回读命令",
        "",
        "```powershell",
        "python -X utf8 scripts/top_sku_aggregation.py verify --config task_config.json",
        "```",
        "",
        "回读字段：`productInfo.productDetailDesc` 与 `skuList[].productDetailDesc.desc`。",
        "接口返回 success 不能证明 SKU 详情已生效。",
    ]
    return "\n".join(lines) + "\n"


def write_build_outputs(out_dir: Path, cfg: dict, result: dict, mapping: dict) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = _checklist_payload(cfg, result)
    digest = _sha256_text(_canonical(payload))
    _atomic_json(out_dir / "mapping.json", mapping)
    _atomic_json(out_dir / "publish_body.json", result["publish_body"])
    _atomic_json(out_dir / "validation_body.json", result["validation_body"])
    _atomic_json(out_dir / "schema_debug.json", result["components"])
    for files in result["detail_files"].values():
        (out_dir / files["pc_name"]).write_text(files["pc"], encoding="utf-8")
        (out_dir / files["mobile_name"]).write_text(files["mobile"], encoding="utf-8")
    _atomic_json(out_dir / "consistency_report.json", result["consistency"])
    _atomic_json(out_dir / "detail_review.json", result["detail_review"])
    _atomic_json(out_dir / "sku_detail_mapping.json", {
        "detailMode": "SPU",
        "skuDetailManualPending": True,
        "items": [
            {"n": sku["n"], "spec": sku["spec"], "sourcePid": sku["pid"],
             "sourceSkuId": sku["skuId"], "pcFile": sku["detailFile"],
             "mobileFile": sku["detailMobileFile"], "detailLen": sku["detailLen"]}
            for sku in result["manifest_skus"]
        ],
    })
    (out_dir / "sku-detail-manual.md").write_text(_manual_markdown(cfg, result), encoding="utf-8")
    (out_dir / "确认清单.md").write_text(_checklist_markdown(cfg, result, digest), encoding="utf-8")
    csv_lines = ["序号,参照SKU,参照商品ID,参照价(元),新链接价(元),颜色规格,SKU图片数,SKU详情(字符),源商品详情文件"]
    csv_lines.extend(",".join(row) for row in result["csv_rows"])
    (out_dir / "新链接对照表.csv").write_bytes(
        b"\xef\xbb\xbf" + "\r\n".join(csv_lines).encode("utf-8"))
    manifest = {
        "mode": MODE,
        "skillVersion": SKILL_VERSION,
        "venderId": cfg["venderId"],
        "shopName": cfg["shopName"],
        "jdoProfile": cfg["jdoProfile"],
        "categoryId": cfg["categoryId"],
        "topN": cfg["topN"],
        "title": cfg["title"],
        "stock": cfg["stock"],
        "draftOnly": True,
        "saveActionType": 0,
        "detailMode": "SPU",
        "skuDetailManualPending": True,
        "skuDetailPendingKeys": [sku["spec"] for sku in result["manifest_skus"]],
        "skuDetailManualSteps": [
            "京麦客户端打开商品编辑页",
            "详情编辑器切换「详情随 SKU 切换」",
            "按 sku_detail_mapping.json 逐个选择详情",
            "保存后运行 verify 回读 skuList[].productDetailDesc.desc",
        ],
        "skuDetailMappingFile": "sku_detail_mapping.json",
        "checklistHash": digest,
        "top1": result["top1"],
        "consistency": result["consistency"],
        "detailReview": result["detail_review"],
        "skus": result["manifest_skus"],
        "applyId": None,
        "productStatus": None,
        "createdAt": _now_iso(),
    }
    _atomic_json(out_dir / "aggregate_manifest.json", manifest)
    return {
        "status": "BUILT",
        "checklistHash": digest,
        "checklistPath": str(out_dir / "确认清单.md"),
        "manifestPath": str(out_dir / "aggregate_manifest.json"),
        "skuCount": len(result["manifest_skus"]),
        "warnings": result["warnings"],
        "detailMarkers": result["detail_review"]["markers"],
        "skuDetailManualPending": True,
    }


def build(cfg_path: Path) -> dict:
    cfg = validate_config(_read_json(cfg_path))
    out_dir = Path(cfg_path).parent
    sources_path = out_dir / str(cfg.get("sourceAppliesPath") or "source_applies.json")
    sources = _read_json(sources_path)
    if not isinstance(sources, dict) or not sources:
        raise BuildError(f"缺少源商品详情 {sources_path}，请先运行 collect")
    report = consistency_report(sources, cfg)
    assert_consistency(report, cfg)
    mapping_path = out_dir / "mapping.json"
    mapping = _read_json(mapping_path, {}) or {}
    result = build_components(cfg, sources, mapping)
    result["consistency"] = report
    result["detail_review"] = {
        "markers": scan_detail_markers(result["detail_files"]),
        "accepted": bool((cfg.get("review") or {}).get("detailAccepted")),
    }
    return write_build_outputs(out_dir, cfg, result, mapping)

def verify_store(jm, cfg: dict) -> dict:
    jm.configure_jdo_profile(cfg["jdoProfile"])
    info = jm.get("jdo-seller-info", "/rest/sp-seller/v0/venderinfo",
                  {"scopeSet": "baseInfo"}).get("data") or {}
    actual_vender = int(info.get("venderId") or 0)
    actual_shop = str((info.get("shopBaseInfo") or {}).get("shopName") or "").strip()
    if actual_vender != int(cfg["venderId"]):
        raise AggregationError(f"venderId 不符: 远端 {actual_vender} != 配置 {cfg['venderId']}")
    if actual_shop != cfg["shopName"]:
        raise AggregationError(f"shopName 不符: 远端 {actual_shop!r} != 配置 {cfg['shopName']!r}")
    jm.mark_jdo_store_verified(cfg["jdoProfile"], actual_vender, actual_shop)
    return {"venderId": actual_vender, "shopName": actual_shop}


def list_products(jm, cfg: dict) -> list[dict]:
    rows, page = [], 1
    while True:
        params = {
            "scopeSet": ["productId", "productName", "productStatus", "categoryId",
                         "itemNum", "created", "modified", "salesVolume", "brandDTO"],
            "page": page, "pageSize": 50, "sortBy": "salesVolume", "sortType": "desc",
        }
        resp = jm.get("jdo-product-status", "/rest/sp-product/v0/products", params)
        batch = resp.get("data") or []
        rows.extend(batch)
        total = int((resp.get("paginationData") or {}).get("totalItems") or 0)
        print(f"products page={page} batch={len(batch)} total={total} got={len(rows)}", flush=True)
        if not batch or len(rows) >= total:
            return rows
        page += 1


def fetch_details(jm, cfg: dict, products: list[dict], cache_path: Path) -> dict:
    details = _read_json(cache_path, {}) or {}
    for row in products:
        pid = str(row["productId"])
        if pid in details:
            continue
        resp = jm.get("jdo-product-status", "/rest/sp-product/v0/products/{productId}",
                      {"productId": int(pid), "scene": "pop"})
        details[pid] = resp.get("data") or {}
        _atomic_json(cache_path, details)
        print(pid, "skuCount=", len(details[pid].get("skuList") or []), flush=True)
        time.sleep(0.3)
    return details


def scan_orders(jm, cfg: dict, category_skus: dict) -> tuple[dict, dict]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    sku_qty: dict[str, int] = defaultdict(int)
    order_count = scanned = 0
    for order_type in (22, 112, 21, 18):
        page = 1
        scanned = 0
        while True:
            params = {
                "orderStatus": "NOT_PAY,WAIT_STOCK_OUT,WAIT_GOODS_RECEIVE_CONFIRM,FINISHED,LOCKED,PAUSE",
                "orderType": order_type, "startTime": start_ms, "endTime": end_ms,
                "sortBy": "createdTime", "sortOrder": "ASC",
                "page": page, "pageSize": 100, "scopeSet": ["skuItemInfoList"],
            }
            resp = jm.get("jdo-order-list", "/rest/sp-order/v0/orders", params)
            batch = resp.get("data") or []
            total = int((resp.get("paginationData") or {}).get("totalItems") or 0)
            for order in batch:
                if str(order.get("orderStatus") or "") in ("TRADE_CANCELED", "NOT_PAY"):
                    continue
                order_count += 1
                for item in order.get("skuItemInfoList") or []:
                    sku_id = str(item.get("skuId") or "")
                    if sku_id in category_skus:
                        sku_qty[sku_id] += int(item.get("itemNum") or item.get("quantity") or 1)
            scanned += len(batch)
            print(f"orderType={order_type} page={page} batch={len(batch)} total={total} "
                  f"scanned={scanned} orders={order_count}", flush=True)
            if not batch or scanned >= total:
                break
            page += 1
            time.sleep(0.2)
    return sku_qty, {"startMs": start_ms, "endMs": end_ms, "scannedOrders": order_count}


def rank_spus(ranked: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in ranked:
        grouped.setdefault(str(row["productId"]), []).append(row)
    result = []
    for pid, rows in grouped.items():
        best = max(rows, key=lambda row: (int(row.get("orderQty30d") or 0),
                                          -int(row.get("skuId") or 0)))
        result.append({
            "pid": pid,
            "best": best,
            "orderQty30d": sum(int(row.get("orderQty30d") or 0) for row in rows),
            "productSales30d": int(best.get("productSales30d") or 0),
        })
    result.sort(key=lambda item: (-item["orderQty30d"], -item["productSales30d"], int(item["pid"])))
    return result


def collect(cfg_path: Path, from_sales_file: str | None = None) -> dict:
    cfg = validate_config(_read_json(cfg_path), require_top_skus=False)
    out_dir = Path(cfg_path).parent
    jm = load_mover()
    verify_store(jm, cfg)
    if from_sales_file:
        sales = _read_json(Path(from_sales_file))
        if not isinstance(sales, dict) or not sales.get("top"):
            raise AggregationError(f"销量文件缺少 top: {from_sales_file}")
        ranked = sales["top"]
        print("复用销量文件，跳过商品盘点与订单扫描")
    else:
        products = list_products(jm, cfg)
        _atomic_json(out_dir / "category_products.json", {
            "shop": {"venderId": cfg["venderId"], "shopName": cfg["shopName"]},
            "products": products,
        })
        details = fetch_details(jm, cfg, products, out_dir / "product_details.json")
        product_sales = {str(row["productId"]): row.get("salesVolume") for row in products}
        category_skus = {}
        for pid, detail in details.items():
            category_detail = (detail.get("productInfo") or {}).get("categoryDetail") or {}
            if int(category_detail.get("lastCategoryId") or 0) != int(cfg["categoryId"]):
                continue
            for sku in detail.get("skuList") or []:
                category_skus[str(sku["skuId"])] = {
                    "productId": pid,
                    "productName": (detail.get("productInfo") or {}).get("productName") or "",
                    "skuId": sku["skuId"],
                    "skuName": sku.get("skuName") or "",
                    "jdPrice": (sku.get("priceInfo") or {}).get("jdPrice"),
                    "productSales30d": product_sales.get(pid),
                    "saleAttrs": [{"name": attr.get("attrName"), "value": attr.get("attrValueAlias")}
                                  for attr in sku.get("saleAttrs") or []],
                }
        print("category skus:", len(category_skus), flush=True)
        sku_qty, window = scan_orders(jm, cfg, category_skus)
        ranked = [{**category_skus[sku_id], "orderQty30d": qty}
                  for sku_id, qty in sku_qty.items()]
        ranked.sort(key=lambda row: (-int(row["orderQty30d"]),
                                     -int(row.get("productSales30d") or 0),
                                     str(row.get("skuId"))))
        _atomic_json(out_dir / "sku_sales_top.json", {
            "rankingBasis": "orderQty30d",
            "salesVolumeNote": "平台商品列表 salesVolume 仅作参考，不用于 TOP N 排名",
            "window": window,
            "categorySkuCount": len(category_skus),
            "top": ranked,
            "allCategorySkus": list(category_skus.values()),
        })
    ranked_spu = rank_spus(ranked)
    sources = _read_json(out_dir / "source_applies.json", {}) or {}
    for item in ranked_spu[: cfg["topN"]]:
        pid = item["pid"]
        if pid in sources:
            continue
        resp = jm.get("jdo-product-update", "/rest/sp-product/v0/product-apply/{applyId}",
                      {"applyId": pid, "scene": "pop"})
        sources[pid] = resp.get("data") or {}
        _atomic_json(out_dir / "source_applies.json", sources)
        time.sleep(0.3)
    if len(ranked_spu) < cfg["topN"]:
        print(f"WARNING: 类目只有 {len(ranked_spu)} 个 SPU，少于 topN={cfg['topN']}；请下调 topN 后再填配置")
    top_skus = []
    for index, item in enumerate(ranked_spu[: cfg["topN"]], 1):
        best = item["best"]
        alias = "".join(str(attr.get("value") or "") for attr in best.get("saleAttrs") or [])
        try:
            price_text = f"{money(best['jdPrice']):.2f}"
        except AggregationError:
            raise AggregationError(f"SKU {best.get('skuId')} 缺少有效 jdPrice") from None
        top_skus.append({
            "n": index,
            "pid": item["pid"],
            "skuId": str(best["skuId"]),
            "price": price_text,
            "specSuggest": alias,
            "spuOrderQty30d": item["orderQty30d"],
            "skuOrderQty30d": best["orderQty30d"],
            "productSales30d": item["productSales30d"],
        })
    _atomic_json(out_dir / "top_skus.json", top_skus)
    return {"topSkus": top_skus, "rankingBasis": "orderQty30d"}

def load_state(out_dir: Path) -> dict:
    return _read_json(Path(out_dir) / "submit_state.json", {}) or {}


def save_state(out_dir: Path, state: dict) -> None:
    _atomic_json(Path(out_dir) / "submit_state.json", state)


def confirm_matches(supplied: str, expected: str) -> bool:
    supplied = str(supplied or "").strip().lower()
    expected = str(expected or "").strip().lower()
    if not supplied or not expected:
        return False
    if len(supplied) >= 8:
        return expected.startswith(supplied)
    return supplied == expected


def parse_created_ms(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number * 1000) if number < 10_000_000_000 else int(number)
    text = str(value).strip()
    if text.isdigit():
        number = int(text)
        return number * 1000 if number < 10_000_000_000 else number
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def title_matches(product_name: Any, title: Any) -> bool:
    left = str(product_name or "").strip()
    right = str(title or "").strip()
    if not left or not right:
        return False
    return left == right or right in left or left in right


def reconcile(jm, cfg: dict, out_dir: Path, state: dict | None = None) -> dict:
    state = state or load_state(out_dir)
    manifest = _read_json(Path(out_dir) / "aggregate_manifest.json", {}) or {}
    apply_id = str(state.get("applyId") or manifest.get("applyId") or "")
    if apply_id:
        try:
            data = jm.get("jdo-product-update", "/rest/sp-product/v0/product-apply/{applyId}",
                          {"applyId": apply_id, "scene": "pop"}).get("data") or {}
            if data.get("productInfo") or data.get("skuList"):
                return {"found": True, "timeVerified": True, "applyId": apply_id, "source": "applyId"}
        except Exception:
            pass
    title = str(state.get("title") or manifest.get("title") or cfg.get("title") or "")
    started = int(state.get("attemptStartedAtMs") or 0)
    resp = jm.get("jdo-product-status", "/rest/sp-product/v0/products", {
        "scopeSet": ["productId", "productName", "productStatus", "created"],
        "page": 1, "pageSize": 50, "sortBy": "created", "sortType": "desc",
    })
    rows = resp.get("data") or []
    matches = [row for row in rows if title_matches(row.get("productName"), title)]
    if not matches:
        return {"found": False}
    for row in matches:
        created = parse_created_ms(row.get("created"))
        if created is None:
            return {"found": True, "timeVerified": False,
                    "productId": row.get("productId"),
                    "note": "标题命中但创建时间无法解析，需人工确认"}
        if started and created >= started - 60000:
            return {"found": True, "timeVerified": True,
                    "productId": row.get("productId"),
                    "applyId": str(row.get("productId"))}
    return {"found": False, "olderSameTitle": [row.get("productId") for row in matches]}


def recheck_source_prices(jm, cfg: dict, out_dir: Path) -> list[dict]:
    mismatches = []
    cache: dict[str, list[dict]] = {}
    for item in cfg["topSkus"]:
        pid = str(item["pid"])
        if pid not in cache:
            resp = jm.get("jdo-product-update", "/rest/sp-product/v0/product-apply/{applyId}",
                          {"applyId": pid, "scene": "pop"})
            cache[pid] = (resp.get("data") or {}).get("skuList") or []
        sku = next((row for row in cache[pid]
                    if str(row.get("skuId")) == str(item["skuId"])), None)
        if sku is None:
            mismatches.append({"spec": item["spec"], "skuId": item["skuId"],
                               "configured": item["price"], "current": None,
                               "reason": "源 SKU 未找到"})
            continue
        current = (sku.get("priceInfo") or {}).get("jdPrice")
        try:
            current_money = money(current)
        except AggregationError:
            mismatches.append({"spec": item["spec"], "skuId": item["skuId"],
                               "configured": item["price"], "current": current,
                               "reason": "源 SKU 无有效价格"})
            continue
        if current_money != money(item["price"]):
            mismatches.append({"spec": item["spec"], "skuId": item["skuId"],
                               "configured": item["price"], "current": f"{current_money:.2f}",
                               "reason": "价格不一致"})
    return mismatches


def reconcile_command(cfg_path: Path) -> dict:
    cfg = validate_config(_read_json(cfg_path), require_top_skus=False)
    out_dir = Path(cfg_path).parent
    state = load_state(out_dir)
    if not state:
        return {"status": "NO_STATE", "found": False,
                "message": "没有 submit_state.json，尚未提交过"}
    jm = load_mover()
    verify_store(jm, cfg)
    found = reconcile(jm, cfg, out_dir, state)
    if found.get("found") and found.get("timeVerified"):
        apply_id = str(found.get("applyId") or found.get("productId") or "")
        state.update({"state": "CREATED", "applyId": apply_id,
                      "reconciledAt": _now_iso(), "lastUpdate": _now_iso()})
        save_state(out_dir, state)
        manifest = _read_json(out_dir / "aggregate_manifest.json", {}) or {}
        manifest["applyId"] = apply_id
        _atomic_json(out_dir / "aggregate_manifest.json", manifest)
        return {"status": "CREATED", "found": True, "applyId": apply_id,
                "message": "回查发现草稿已创建，已写 CREATED"}
    if found.get("found"):
        return {"status": "FOUND_UNVERIFIED", "found": True,
                "productId": found.get("productId"),
                "message": "标题命中但创建时间无法确认；保持原状态，请人工核对"}
    return {"status": "NOT_FOUND", "found": False,
            "message": "未发现对应草稿；保持原状态"}


def submit(cfg_path: Path, confirm: str, retry: bool = False) -> dict:
    cfg = validate_config(_read_json(cfg_path))
    out_dir = Path(cfg_path).parent
    manifest = _read_json(out_dir / "aggregate_manifest.json")
    if not manifest:
        raise SubmitBlocked("缺少 aggregate_manifest.json，请先运行 build")
    expected = str(manifest.get("checklistHash") or "")
    if not expected:
        raise SubmitBlocked("manifest 缺少 checklistHash，请重新运行 build")
    if not confirm_matches(confirm, expected):
        raise SubmitBlocked(
            "确认哈希不匹配。请核对 确认清单.md 后，用 build 输出的 checklistHash 前 8 位提交；"
            f"当前清单哈希={expected[:8]}")
    if manifest.get("saveActionType") != 0:
        raise SubmitBlocked("saveActionType 必须为 0（只保存草稿）")
    state = load_state(out_dir)
    if state.get("applyId") or manifest.get("applyId"):
        raise SubmitBlocked(
            f"已存在 applyId={state.get('applyId') or manifest.get('applyId')}，禁止重复提交")
    if state.get("state") == "CREATED":
        raise SubmitBlocked("提交状态为 CREATED，禁止重复提交")
    review = manifest.get("detailReview") or {}
    if review.get("markers") and not review.get("accepted"):
        raise SubmitBlocked(
            "源详情命中第三方平台/联系方式/二维码等标记，未确认；请在 task_config.json 设置 "
            "review.detailAccepted=true 并复核后重跑 build")
    jm = load_mover()
    verify_store(jm, cfg)
    if state.get("state") in ("SUBMITTING", "UNKNOWN"):
        found = reconcile(jm, cfg, out_dir, state)
        if found.get("found"):
            if not found.get("timeVerified"):
                raise SubmitBlocked(
                    "回查标题命中但创建时间无法确认，禁止自动重投；请人工核对商品/申请单后再处理。"
                    f" 候选 productId={found.get('productId')}")
            apply_id = str(found.get("applyId") or found.get("productId") or "")
            state.update({"state": "CREATED", "applyId": apply_id,
                          "reconciledAt": _now_iso(), "lastUpdate": _now_iso()})
            save_state(out_dir, state)
            manifest["applyId"] = apply_id
            _atomic_json(out_dir / "aggregate_manifest.json", manifest)
            raise SubmitBlocked(f"回查发现草稿已创建 applyId={apply_id}，不再重复提交")
        if not retry:
            raise SubmitBlocked(
                "上次提交处于 SUBMITTING/UNKNOWN，回查未发现草稿；确认后加 --retry 重新提交")
        state["state"] = "PREPARED"
        state["lastUpdate"] = _now_iso()
        save_state(out_dir, state)
    mismatches = recheck_source_prices(jm, cfg, out_dir)
    if mismatches:
        raise SubmitBlocked(
            "源 SKU 价格已变化，禁止提交旧 payload；请重新 build 并确认。差异: " +
            "；".join(f"{item['spec']} 配置={item['configured']} 当前={item['current']}"
                     for item in mismatches))
    body = _read_json(out_dir / "publish_body.json")
    if not body or (body.get("productApplyDTO") or {}).get("saveActionType") != 0:
        raise SubmitBlocked("publish_body.json 的 saveActionType 必须为 0")
    val_resp = jm.run_jdo_large(
        url="/rest/sp-product/v0/product-schemas/validations",
        method="POST", body_path=out_dir / "validation_body.json",
        skill_id="jdo-product-publish", yes=True,
    )
    _atomic_json(out_dir / "validation_resp.json", val_resp)
    data = val_resp.get("data")
    if not val_resp.get("success") or (isinstance(data, dict) and data.get("passFlag") is False):
        state.update({"state": "PREPARED", "lastValidation": "FAILED", "lastUpdate": _now_iso()})
        save_state(out_dir, state)
        return {"status": "VALIDATION_FAILED", "response": val_resp}
    started = int(time.time() * 1000)
    attempts = list(state.get("attempts") or [])
    attempts.append({"at": _now_iso(), "startedAtMs": started})
    state.update({
        "state": "SUBMITTING",
        "attemptStartedAtMs": started,
        "title": cfg["title"],
        "venderId": cfg["venderId"],
        "checklistHash": expected,
        "attempts": attempts,
        "lastUpdate": _now_iso(),
    })
    save_state(out_dir, state)
    try:
        resp = jm.run_jdo_large(
            url="/rest/sp-product/v0/product-applies", method="POST",
            body_path=out_dir / "publish_body.json",
            skill_id="jdo-product-publish", yes=True,
        )
    except Exception as exc:
        state.update({"state": "UNKNOWN", "lastError": str(exc)[:600], "lastUpdate": _now_iso()})
        save_state(out_dir, state)
        return {"status": "UNKNOWN", "error": str(exc)[:600],
                "next": "运行 reconcile 只读回查；确认未创建后才可用 --retry 重投"}
    _atomic_json(out_dir / "create_response.json", resp)
    data = resp.get("data") or resp
    apply_id = data.get("applyId") if isinstance(data, dict) else None
    if not apply_id:
        state.update({"state": "UNKNOWN", "lastError": "未取到 applyId", "lastUpdate": _now_iso()})
        save_state(out_dir, state)
        return {"status": "UNKNOWN", "error": "未取到 applyId",
                "next": "运行 reconcile 只读回查；确认未创建后才可用 --retry 重投"}
    state.update({"state": "CREATED", "applyId": str(apply_id), "lastUpdate": _now_iso()})
    save_state(out_dir, state)
    manifest["applyId"] = str(apply_id)
    _atomic_json(out_dir / "aggregate_manifest.json", manifest)
    return {"status": "CREATED", "applyId": str(apply_id)}


def verify(cfg_path: Path) -> dict:
    cfg = validate_config(_read_json(cfg_path))
    out_dir = Path(cfg_path).parent
    manifest = _read_json(out_dir / "aggregate_manifest.json")
    if not manifest:
        raise VerifyError("缺少 aggregate_manifest.json")
    apply_id = str(manifest.get("applyId") or "")
    if not apply_id:
        raise VerifyError("manifest 缺少 applyId，请先运行 submit")
    jm = load_mover()
    verify_store(jm, cfg)
    resp = jm.get("jdo-product-update", "/rest/sp-product/v0/product-apply/{applyId}",
                  {"applyId": apply_id, "scene": "pop"})
    _atomic_json(out_dir / "verify_apply_detail.json", resp)
    data = resp.get("data") or {}
    product_info = data.get("productInfo") or {}
    skus = data.get("skuList") or []
    expected = {sku["spec"]: sku for sku in manifest.get("skus") or []}
    problems: list[str] = []
    pending: list[str] = []
    actual_specs: list[str] = []
    priced = single = multi = detail_ok = True
    for sku in skus:
        attrs = {attr.get("attrName"): attr.get("attrValueAlias")
                 for attr in (sku.get("saleAttrs") or [])}
        price = (sku.get("priceInfo") or {}).get("jdPrice")
        detail_desc = sku.get("productDetailDesc")
        desc = detail_desc.get("desc") if isinstance(detail_desc, dict) else detail_desc
        alias = list(attrs.values())
        spec = alias[0] if alias else "?"
        actual_specs.append(spec)
        if price in (None, ""):
            priced = False
            problems.append(f"{spec} 无价格")
        if set(attrs) != {"颜色"}:
            single = False
            problems.append(f"{spec} 销售属性异常: {set(attrs)}")
        if spec in expected:
            item = expected[spec]
            if price not in (None, "") and money(price) != money(item["price"]):
                priced = False
                problems.append(f"{spec} 价格 {price} != 期望 {item['price']}")
            if not desc:
                detail_ok = False
                pending.append(spec)
            elif abs(len(desc) - int(item["detailLen"])) > 600:
                problems.append(f"{spec} SKU详情长度 {len(desc)} 与源 {item['detailLen']} 偏差大")
        elif not desc:
            detail_ok = False
            pending.append(spec)
    expected_specs = set(expected)
    actual_set = set(actual_specs)
    missing_specs = sorted(expected_specs - actual_set)
    extra_specs = sorted(actual_set - expected_specs)
    sku_count_matches = len(skus) == len(expected) and not missing_specs and not extra_specs
    if len(skus) != len(expected):
        problems.append(f"SKU 数量不一致: 实际 {len(skus)} != 期望 {len(expected)}")
    if missing_specs:
        problems.append("缺少规格: " + "、".join(missing_specs))
    if extra_specs:
        problems.append("多出规格: " + "、".join(extra_specs))
    groups = (data.get("material") or {}).get("mainImages") or []
    if not groups:
        multi = False
        problems.append("回读没有主图组")
    for group in groups:
        if len(group.get("imageInfoList") or []) < 5:
            multi = False
            problems.append(f"图片组 {group.get('uuid')} 少于5张")
    status = None
    status_query_id = product_info.get("productId") or apply_id
    try:
        pid_value = int(status_query_id) if str(status_query_id).isdigit() else status_query_id
        listing = jm.get("jdo-product-status", "/rest/sp-product/v0/products", {
            "productIdList": [pid_value],
            "scopeSet": ["productId", "productName", "productStatus"],
            "page": 1, "pageSize": 10,
        })
        rows = listing.get("data") or []
        if rows:
            status = rows[0].get("productStatus")
            try:
                status = int(status) if status is not None else None
            except (TypeError, ValueError):
                pass
        else:
            problems.append(f"未查到商品状态（查询ID={status_query_id}）")
    except Exception as exc:
        problems.append(f"商品状态回读失败: {exc}")
    draft_ok = status == DRAFT_STATUS
    if status == ON_SALE_STATUS:
        draft_ok = False
        problems.append("商品已在售(productStatus=103)，预期草稿(105)")
    elif status is not None and status != DRAFT_STATUS:
        problems.append(f"商品状态 productStatus={status}，预期 105")
    checks = {
        "priced": priced,
        "singleColorDimension": single,
        "multiImage": multi,
        "skuDetailPerSku": detail_ok,
        "skuCountMatches": sku_count_matches,
        "draftNotOnSale": draft_ok,
    }
    report = {
        "applyId": apply_id,
        "productId": product_info.get("productId"),
        "productStatus": status,
        "wareStatus": product_info.get("wareStatus"),
        "skuCount": len(skus),
        "checks": checks,
        "skuDetailManualPending": not detail_ok,
        "skuDetailPendingKeys": pending,
        "problems": problems,
        "checkedAt": _now_iso(),
    }
    _atomic_json(out_dir / "verify_report.json", report)
    return report


def profile_lock_status(profile_dir: str | Path,
                        process_lister: Callable[[str], list[dict]] | None = None) -> dict:
    path = Path(profile_dir)
    if not path.is_dir():
        return {"exists": False, "locked": False, "locks": [], "processes": [],
                "advice": "Profile 目录不存在；请先确认店铺专用浏览器 Profile 路径"}
    locks = [name for name in LOCK_NAMES if (path / name).exists()]
    lister = process_lister or list_chrome_processes_for_profile
    try:
        processes = lister(str(path.resolve()))
    except Exception:
        processes = []
    locked = bool(locks or processes)
    return {
        "exists": True,
        "locked": locked,
        "locks": locks,
        "processes": processes,
        "advice": ("检测到 Profile 被占用。请关闭使用该 Profile 的浏览器窗口后重试；"
                   "不要按 PID 强杀进程，也不要删除别人的锁文件。") if locked else "未检测到 Profile 占用",
    }


def list_chrome_processes_for_profile(profile_dir: str) -> list[dict]:
    if os.name != "nt":
        return []
    script = ("Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
              "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, check=False,
        )
    except Exception:
        return []
    text = (completed.stdout or "").strip()
    if not text:
        return []
    try:
        rows = json.loads(text)
    except ValueError:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    target = str(Path(profile_dir).resolve()).lower()
    hits = []
    for row in rows:
        command = str(row.get("CommandLine") or "")
        if target in command.lower():
            hits.append({"pid": row.get("ProcessId"), "commandLineHasProfile": True})
    return hits


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="TOP销量SKU聚合草稿（只保存草稿，不上架）")
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect", help="只读采集 TOP SKU")
    collect_parser.add_argument("--config", required=True)
    collect_parser.add_argument("--from-sales-file", default=None)
    build_parser = sub.add_parser("build", help="本地构建 payload/清单/逐SKU详情")
    build_parser.add_argument("--config", required=True)
    submit_parser = sub.add_parser("submit", help="提交草稿（需确认清单哈希）")
    submit_parser.add_argument("--config", required=True)
    submit_parser.add_argument("--confirm", required=True)
    submit_parser.add_argument("--retry", action="store_true")
    verify_parser = sub.add_parser("verify", help="只读回读验证")
    verify_parser.add_argument("--config", required=True)
    reconcile_parser = sub.add_parser("reconcile", help="只读回查提交结果")
    reconcile_parser.add_argument("--config", required=True)
    profile_parser = sub.add_parser("profile-check", help="只读检测浏览器 Profile 占用")
    profile_parser.add_argument("--profile-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            print(json.dumps(collect(args.config, args.from_sales_file), ensure_ascii=False, indent=2))
            return 0
        if args.command == "build":
            print(json.dumps(build(args.config), ensure_ascii=False, indent=2))
            return 0
        if args.command == "submit":
            result = submit(args.config, args.confirm, retry=args.retry)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return {"CREATED": 0, "VALIDATION_FAILED": 1, "UNKNOWN": 2}.get(result.get("status"), 2)
        if args.command == "verify":
            report = verify(args.config)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            checks = report.get("checks") or {}
            if all(checks.values()):
                return 0
            if not checks.get("skuDetailPerSku") and not report.get("problems"):
                return 3
            return 2
        if args.command == "reconcile":
            result = reconcile_command(args.config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return {"CREATED": 0, "NOT_FOUND": 1,
                    "FOUND_UNVERIFIED": 2, "NO_STATE": 1}.get(result.get("status"), 2)
        if args.command == "profile-check":
            result = profile_lock_status(args.profile_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if not result.get("locked") else 4
    except SubmitBlocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3
    except AggregationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
