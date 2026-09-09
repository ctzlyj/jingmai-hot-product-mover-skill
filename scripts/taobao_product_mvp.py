#!/usr/bin/env python3
"""Convert captured Taobao detail data into the existing Jingmai draft format."""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jd_product_mover as mover
import jd_product_pipeline as pipeline


ROOT = Path(__file__).resolve().parents[1]


def load_bootstrap(script: str) -> dict[str, Any]:
    marker = "var b = "
    start = script.find(marker)
    end = script.find(";for (var k", start)
    if start < 0 or end < 0:
        raise ValueError("淘宝页面数据中未找到 __ICE_APP_CONTEXT__ 引导对象")
    context = json.loads(script[start + len(marker):end])
    return context["loaderData"]["home"]["data"]["res"]


def normalize_url(value: str) -> str:
    source = str(value or "").strip()
    return f"https:{source}" if source.startswith("//") else source


def detail_image_urls(payload: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for image in payload.get("images") or []:
        attributes = image.get("attrs") or {}
        if attributes.get("data-name") != "singleImage":
            continue
        url = normalize_url(attributes.get("data-src") or attributes.get("src") or "")
        if url and url not in rows:
            rows.append(url)
    return rows


def build_source(
    response: dict[str, Any], detail_payload: dict[str, Any], source_url: str,
) -> dict[str, Any]:
    item = response.get("item") or {}
    seller = response.get("seller") or {}
    sku_base = response.get("skuBase") or {}
    sku_info = (response.get("skuCore") or {}).get("sku2info") or {}
    properties = sku_base.get("props") or []
    variable_properties = [item for item in properties if len(item.get("values") or []) > 1]
    if [item.get("name") for item in variable_properties] != ["颜色分类", "尺寸"]:
        raise ValueError(f"尚未适配的淘宝销售属性：{[item.get('name') for item in variable_properties]}")

    value_maps = {
        str(prop["pid"]): {str(value["vid"]): value for value in prop.get("values") or []}
        for prop in properties
    }
    sale_properties = {
        str(prop["pid"]): "颜色" if prop.get("name") == "颜色分类" else str(prop.get("name"))
        for prop in variable_properties
    }
    variants: list[dict[str, Any]] = []
    for sku in sku_base.get("skus") or []:
        sku_id = str(sku.get("skuId") or "")
        values = dict(part.split(":", 1) for part in str(sku.get("propPath") or "").split(";") if ":" in part)
        info = sku_info.get(sku_id) or {}
        price = info.get("subPrice") or info.get("price") or {}
        money = str(price.get("priceMoney") or "")
        if not sku_id or not money.isdigit():
            raise ValueError(f"淘宝真实 SKU 缺少价格：{sku_id or sku}")
        attributes = {
            sale_properties[str(prop["pid"])]: str(value_maps[str(prop["pid"])][values[str(prop["pid"])]].get("name") or "")
            for prop in variable_properties
        }
        style_value = value_maps[str(variable_properties[0]["pid"])][values[str(variable_properties[0]["pid"])]]
        variants.append({
            "sourceSku": sku_id,
            "sourcePrice": f"{Decimal(money) / 100:.2f}",
            "sourceListPrice": f"{Decimal(str((info.get('price') or {}).get('priceMoney') or money)) / 100:.2f}",
            "sourceAttributes": attributes,
            "sourcePropPath": str(sku.get("propPath") or ""),
            "sourceImageUrl": normalize_url(style_value.get("image") or item.get("images", [""])[0]),
        })
    if len(variants) != len(sku_base.get("skus") or []) or len({row["sourceSku"] for row in variants}) != len(variants):
        raise ValueError("淘宝 SKU 列表不完整或存在重复")

    parsed_url = urlparse(source_url)
    query = dict(part.split("=", 1) for part in parsed_url.query.split("&") if "=" in part)
    item_id = str(item.get("itemId") or query.get("id") or "")
    videos = item.get("videos") or []
    source_video = videos[0] if videos else None
    video = None
    if source_video:
        video = {
            "sourceVideoId": str(source_video.get("videoId") or ""),
            "resolutionMethod": "taobao-page-state",
            "downloadUrl": normalize_url(source_video.get("url") or ""),
            "coverUrl": normalize_url(source_video.get("videoThumbnailURL") or ""),
            "durationSeconds": 0,
        }
    fixed_properties = {
        str(prop.get("name") or prop.get("pid")): [str(value.get("name") or "") for value in prop.get("values") or []]
        for prop in properties if len(prop.get("values") or []) == 1
    }
    return {
        "schemaVersion": "taobao-mover/v1",
        "sourcePlatform": "taobao",
        "productKeyPrefix": "TB",
        "collectedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sourceUrl": f"https://item.taobao.com/item.htm?id={item_id}",
        "sourceInputUrl": source_url,
        "sourceSku": item_id,
        "sourceCurrentSku": str(query.get("skuId") or ""),
        "sourceSpu": item_id,
        "sourceVenderId": str(seller.get("sellerId") or ""),
        "sourceShopId": str(seller.get("shopId") or ""),
        "sourceShopName": str(seller.get("shopName") or ""),
        "sourceCategoryIds": [50020808, 50021907],
        "categoryIds": [35338, 35347, 35381],
        "title": str(item.get("title") or ""),
        "fullTitle": str(item.get("title") or ""),
        "sourceBrandId": -1,
        "sourceBrandName": "",
        "saleProperties": sale_properties,
        "sourceFixedSkuProperties": fixed_properties,
        "mainImageUrls": [normalize_url(value) for value in item.get("images") or []],
        "detailImageUrls": detail_image_urls(detail_payload),
        "videoUrl": video["downloadUrl"] if video else "",
        "video": video,
        "variants": variants,
    }


def mp4_duration_seconds(path: Path) -> int:
    data = path.read_bytes()
    offset = data.find(b"mvhd")
    if offset < 0 or offset + 32 > len(data):
        return 0
    version = data[offset + 4]
    if version == 0:
        timescale = struct.unpack(">I", data[offset + 16:offset + 20])[0]
        duration = struct.unpack(">I", data[offset + 20:offset + 24])[0]
    elif version == 1:
        timescale = struct.unpack(">I", data[offset + 24:offset + 28])[0]
        duration = struct.unpack(">Q", data[offset + 28:offset + 36])[0]
    else:
        return 0
    return int(Decimal(duration / timescale).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if timescale else 0


def update_local_video_metadata(source: dict[str, Any], folder: Path) -> dict[str, Any]:
    video = source.get("video") or {}
    if not video.get("localPath"):
        return source
    video_path = folder / video["localPath"]
    video["durationSeconds"] = mp4_duration_seconds(video_path)
    video["sourceSizeBytes"] = video_path.stat().st_size
    source["video"] = video
    mover.write_json(folder / "manifest.json", source)
    return source


def taobao_rules(base_rules: dict[str, Any]) -> dict[str, Any]:
    rules = deepcopy(base_rules)
    rules["categoryAttributes"] = [
        {"componentCode": "prop-product-243058", "attrName": "风格", "inputType": "singleSelect", "required": True, "valueIds": ["1403820"], "valueLabels": ["简约风"]},
        {"componentCode": "prop-product-243094", "attrName": "工艺", "inputType": "singleSelect", "required": True, "valueIds": ["1404130"], "valueLabels": ["喷绘"]},
    ]
    rules["skuAttributes"]["mounting"]["default"] = {"valueId": "1404242", "label": "无框"}
    return rules


def compliant_sale_alias(value: str) -> str:
    normalized = str(value or "").strip()
    if "定制尺寸" in normalized and ("客服" in normalized or "下单" in normalized):
        return "定制尺寸"
    return normalized


def split_source_by_style(source: dict[str, Any], sku_limit: int) -> list[dict[str, Any]]:
    if sku_limit <= 0:
        raise ValueError("SKU 上限必须大于0")
    sale_names = list((source.get("saleProperties") or {}).values())
    if len(sale_names) < 2:
        if len(source.get("variants") or []) > sku_limit:
            raise ValueError("单销售维度商品超过 SKU 上限，无法按完整款式自动拆分")
        return [deepcopy(source)]
    style_name = sale_names[0]
    sanitized = deepcopy(source)
    style_groups: dict[str, list[dict[str, Any]]] = {}
    for variant in sanitized.get("variants") or []:
        attributes = variant.get("sourceAttributes") or {}
        for name, value in list(attributes.items()):
            attributes[name] = compliant_sale_alias(str(value))
        style = str(attributes.get(style_name) or "").strip()
        if not style:
            raise ValueError(f"淘宝真实 SKU 缺少款式属性：{variant.get('sourceSku')}")
        style_groups.setdefault(style, []).append(variant)
    if any(len(rows) > sku_limit for rows in style_groups.values()):
        raise ValueError("单个款式的真实 SKU 数已超过平台单商品上限")
    if len(sanitized.get("variants") or []) <= sku_limit:
        return [sanitized]

    styles = list(style_groups)
    part_count = math.ceil(len(sanitized["variants"]) / sku_limit)
    grouped_parts: list[list[str]] = []
    start = 0
    for part_index in range(part_count):
        remaining_parts = part_count - part_index
        remaining_styles = len(styles) - start
        take = math.ceil(remaining_styles / remaining_parts)
        while take > 0 and sum(len(style_groups[name]) for name in styles[start:start + take]) > sku_limit:
            take -= 1
        while (
            take < remaining_styles
            and sum(len(style_groups[name]) for name in styles[start + take:]) > (remaining_parts - 1) * sku_limit
        ):
            take += 1
        if take <= 0 or sum(len(style_groups[name]) for name in styles[start:start + take]) > sku_limit:
            raise ValueError("无法在保留完整款式组的前提下满足 SKU 上限")
        grouped_parts.append(styles[start:start + take])
        start += take

    parent_sku = str(sanitized["sourceSku"])
    parent_spu = str(sanitized["sourceSpu"])
    parts: list[dict[str, Any]] = []
    for part_index, part_styles in enumerate(grouped_parts, start=1):
        part = deepcopy(sanitized)
        part_code = chr(64 + part_index) if part_index <= 26 else str(part_index)
        part_id = f"{parent_sku}-P{part_index:02d}"
        part["sourceParentSku"] = parent_sku
        part["sourceParentSpu"] = parent_spu
        part["sourceSku"] = part_id
        part["sourceSpu"] = part_id
        part["title"] = f"{part_code}组 {sanitized['title']}"
        part["fullTitle"] = part["title"]
        allowed_styles = set(part_styles)
        part["variants"] = [
            variant for variant in sanitized["variants"]
            if str((variant.get("sourceAttributes") or {}).get(style_name) or "") in allowed_styles
        ]
        part["splitInfo"] = {
            "parentSourceSku": parent_sku,
            "partIndex": part_index,
            "partCount": len(grouped_parts),
            "styleStart": start - len(part_styles) + 1,
            "styleEnd": start,
            "styleCount": len(part_styles),
            "skuCount": len(part["variants"]),
        }
        parts.append(part)
    return parts


def pricing_audit(listing: dict[str, Any]) -> dict[str, Any]:
    pricing = listing["source"]["pricing"]
    national = Decimal("1") - Decimal(str(pricing["nationalSubsidyRate"]))
    activity = Decimal("1") - Decimal(str(pricing["activityDiscountRate"]))
    below = Decimal(str(pricing["belowCompetitor"]))
    coupon = Decimal(str(pricing["coupon"]))
    rows = []
    for sku in listing["skus"]:
        source = Decimal(str(sku["sourcePrice"]))
        jd_price = Decimal(str(sku["jdPrice"]))
        expected_jd = ((source - below + coupon) / (national * activity)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        arrival = (jd_price * national * activity - coupon).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        expected_arrival = (source - below).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rows.append({
            "sourceSku": sku["sourceSku"], "sourcePrice": str(source), "jdPrice": str(jd_price),
            "expectedJdPrice": str(expected_jd), "arrivalPrice": str(arrival),
            "expectedArrivalPrice": str(expected_arrival), "ok": jd_price == expected_jd and arrival == expected_arrival,
        })
    return {"ok": all(row["ok"] for row in rows), "skuCount": len(rows), "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--detail-dom", type=Path, required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--duplicate-review", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "migrations")
    args = parser.parse_args()

    duplicate_review = mover.read_json(args.duplicate_review)
    if duplicate_review.get("decision") != "PROCEED":
        raise ValueError("淘宝 MVP 仅在 AI 判重结论为 PROCEED 时生成")
    store_config = mover.read_json(ROOT / "config" / "store.local.json")
    store = pipeline.verify_store_identity(store_config, args.profile)
    rules = taobao_rules(pipeline.effective_rules(
        mover.read_json(ROOT / "config" / "modern-decorative-painting.rules.json"), store_config,
    ))
    response = load_bootstrap(args.bootstrap.read_text(encoding="utf-8"))
    source = build_source(response, mover.read_json(args.detail_dom), args.source_url)
    prefix = mover.infer_prefix(source["title"], rules)
    style_start, _ = pipeline.next_style_start(prefix, 35381)
    sku_limit = int((mover.read_json(ROOT / "config" / "modern-decorative-painting.reference.json").get("schema") or {}).get("extendMap", {}).get("skuBindThresholdNum") or 299)
    sources = split_source_by_style(source, sku_limit)
    results = []
    current_style_start = style_start
    for part_source in sources:
        folder = mover.materialize(part_source, args.output_root, mover.HttpClient())
        part_source = mover.read_json(folder / "manifest.json")
        part_source = update_local_video_metadata(part_source, folder)
        listing = mover.build_listing(
            part_source, folder, rules,
            mover.read_json(ROOT / "config" / "modern-decorative-painting.reference.json"), current_style_start,
        )
        audit = pricing_audit(listing)
        if not audit["ok"]:
            raise ValueError("淘宝逐 SKU 定价复算失败")
        mover.write_json(folder / "listing.json", listing)
        mover.write_json(folder / "pricing-verification.json", audit)
        mover.write_json(folder / "duplicate-review.json", duplicate_review)
        mover.build_audit_workbook(part_source, listing, folder / f"{folder.name}.xlsx")
        pipeline.save_state(
            folder / "state.json", "LOCAL_DRAFT", sourcePlatform="taobao",
            sourceSku=part_source["sourceSku"], sourceParentSku=part_source.get("sourceParentSku"),
            sourceUrl=part_source["sourceUrl"], store=store, splitInfo=part_source.get("splitInfo"),
            duplicateReview=duplicate_review, skuCount=len(listing["skus"]), productId=None,
        )
        style_count = len({row["styleAlias"] for row in listing["skus"]})
        results.append({
            "folder": str(folder.resolve()), "sourceSku": part_source["sourceSku"],
            "skuCount": len(listing["skus"]), "styleCount": style_count,
            "sizeCount": len({row["sizeAlias"] for row in listing["skus"]}),
            "mainImages": len(part_source["mainImages"]), "detailImages": len(part_source["detailImages"]),
            "videoDurationSeconds": int((part_source.get("video") or {}).get("durationSeconds") or 0),
            "vendorSkuPrefix": listing["product"]["vendorSkuPrefix"], "vendorStyleStart": current_style_start,
        })
        current_style_start += style_count
    aggregate_stage = "SPLIT_LOCAL_DRAFT" if len(results) > 1 else "LOCAL_DRAFT"
    if len(results) > 1:
        pipeline.save_state(
            args.output_root / "state" / f"{source['sourceSku']}.json", aggregate_stage,
            sourcePlatform="taobao", sourceSku=source["sourceSku"], sourceUrl=source["sourceUrl"],
            store=store, duplicateReview=duplicate_review, skuCount=len(source["variants"]),
            splitParts=results, productId=None,
        )
    print(json.dumps({
        "ok": True, "stage": aggregate_stage, "split": len(results) > 1,
        "sourceSku": source["sourceSku"], "skuCount": len(source["variants"]), "parts": results,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
