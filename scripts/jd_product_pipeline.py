#!/usr/bin/env python3
"""End-to-end JD product migration and Jingmai publication pipeline."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import requests

import jd_product_mover as mover
import jdo_listing_mvp as jingmai


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "outputs" / "migrations"
REFERENCE = ROOT / "config" / "modern-decorative-painting.reference.json"
RULES = ROOT / "config" / "modern-decorative-painting.rules.json"
STORE_CONFIG = ROOT / "config" / "store.local.json"
VIDEO_MATERIAL_TYPE = 42
VIDEO_TYPE = 1
BATCH_PREFLIGHT_DECISIONS = {
    "PROCEED",
    "SKIPPED_OUT_OF_SCOPE_CATEGORY",
    "SKIPPED_AI_DUPLICATE",
}


class PipelineError(RuntimeError):
    pass


def video_is_ready(status: int) -> bool:
    return status in {2, 4}


def validate_store_config(config: dict[str, Any], profile: str | None = None) -> None:
    store = config.get("store") or {}
    business = config.get("business") or {}
    if int(store.get("venderId") or 0) <= 0:
        raise ValueError("店铺配置缺少有效的 store.venderId")
    if not str(store.get("shopName") or "").strip() or str(store.get("shopName")) == "YOUR_SHOP_NAME":
        raise ValueError("店铺配置缺少有效的 store.shopName")
    configured_profile = str(store.get("profile") or "").strip().lower()
    if not configured_profile or configured_profile == "default":
        raise ValueError("店铺配置缺少明确的 store.profile，且不得使用 default")
    if profile and configured_profile != str(profile).strip().lower():
        raise ValueError(
            f"命令 Profile 与店铺配置不一致：{profile} != {configured_profile}"
        )
    if int(business.get("freightTemplateId") or 0) <= 0:
        raise ValueError("店铺配置缺少有效的 business.freightTemplateId")
    if not str(business.get("freightTemplateLabel") or "").strip():
        raise ValueError("店铺配置缺少有效的 business.freightTemplateLabel")


def effective_rules(rules: dict[str, Any], store_config: dict[str, Any]) -> dict[str, Any]:
    validate_store_config(store_config)
    merged = deepcopy(rules)
    defaults = merged.setdefault("productDefaults", {})
    business = store_config["business"]
    defaults["freightTemplateId"] = int(business["freightTemplateId"])
    defaults["freightTemplateLabel"] = str(business["freightTemplateLabel"])
    return merged


def video_upload_url_params(
    video_path: Path, file_size: int, jd_video_id: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "fileName": video_path.name,
        "fileSize": file_size,
        "videoType": VIDEO_TYPE,
    }
    if jd_video_id:
        params["jdVideoId"] = jd_video_id
        params["breakPointFlag"] = True
    return params


def build_video_binding_body(
    *, product_id: int, sku_id: int, video: dict[str, Any], tag: dict[str, Any],
) -> dict[str, Any]:
    return {
        "skuMaterials": [{
            "productId": product_id,
            "skuId": sku_id,
            "materials": [{
                "materialType": VIDEO_MATERIAL_TYPE,
                "order": 1,
                "videoMaterial": {
                    "indexImage": str(video["indexImage"]),
                    "videoFrom": 1,
                    "videoId": int(video["videoId"]),
                    "jdVideoId": int(video["jdVideoId"]),
                    "title": str(video["videoName"]),
                    "duration": int(video["duration"]),
                    "tagOrder": int(tag["sort"]),
                    "tagId": int(tag["tagId"]),
                    "tagName": str(tag["tagName"]),
                },
            }],
        }],
    }


def video_relation_audit(
    relations: list[dict[str, Any]], sku_ids: list[int], video_id: int,
) -> dict[str, Any]:
    expected = set(sku_ids)
    statuses = {
        int(item["skuId"]): int(item.get("relationStatus") or 0)
        for item in relations
        if int(item.get("videoId") or 0) == video_id and int(item.get("skuId") or 0) in expected
    }
    missing = sorted(expected - set(statuses))
    pending = sorted(sku_id for sku_id, status in statuses.items() if status == 3)
    rejected = sorted(sku_id for sku_id, status in statuses.items() if status == 5)
    unexpected = sorted(sku_id for sku_id, status in statuses.items() if status not in {3, 4, 5})
    return {
        "allApproved": not missing and not pending and not rejected and not unexpected,
        "relationCount": len(statuses),
        "statusCounts": {
            str(status): list(statuses.values()).count(status)
            for status in sorted(set(statuses.values()))
        },
        "missingSkuIds": missing,
        "pendingSkuIds": pending,
        "rejectedSkuIds": rejected,
        "unexpectedStatusSkuIds": unexpected,
    }


def save_state(path: Path, stage: str, **values: Any) -> dict[str, Any]:
    state = mover.read_json(path) if path.exists() else {}
    state.update(values)
    if not stage.startswith("SKIPPED_"):
        state.pop("categoryDecision", None)
        state.pop("preflightDecision", None)
        state.pop("duplicateReview", None)
    if not stage.startswith("FAILED"):
        state.pop("error", None)
        state.pop("errors", None)
    state["stage"] = stage
    state["updatedAt"] = mover.now_iso()
    state.setdefault("history", []).append({"stage": stage, "at": state["updatedAt"]})
    mover.write_json(path, state)
    return state


def record_pipeline_exception(path: Path, exc: Exception) -> dict[str, Any]:
    state = mover.read_json(path) if path.exists() else {}
    if state.get("stage") in {"PRODUCT_PROPAGATION_PENDING", "PRODUCT_APPLY_REJECTED"} and state.get("productId"):
        return state
    return save_state(path, "FAILED", error=str(exc))


def existing_folder(source_sku: str, output_root: Path) -> Path | None:
    matches = sorted(path for path in output_root.glob(f"{source_sku}_*") if path.is_dir())
    if len(matches) > 1:
        raise PipelineError(f"同一源SKU存在多个运行目录：{matches}")
    return matches[0] if matches else None


def verify_store_identity(store_config: dict[str, Any], profile: str) -> dict[str, Any]:
    validate_store_config(store_config, profile)
    expected = store_config["store"]
    active_profile = jingmai.configure_jdo_profile(profile)
    data = jingmai.get(
        "jdo-seller-info",
        "/rest/sp-seller/v0/venderinfo",
        {"scopeSet": "baseInfo"},
    ).get("data") or {}
    shop = data.get("shopBaseInfo") or {}
    vender_id = int(data.get("venderId") or 0)
    shop_name = str(shop.get("shopName") or "")
    if vender_id != int(expected["venderId"]) or shop_name != str(expected["shopName"]):
        raise PipelineError(
            f"当前京麦登录态不是白名单店铺：venderId={vender_id}, shopName={shop_name}"
        )
    if int(shop.get("type", -1)) != 0 or int(shop.get("status") or 0) != 1:
        raise PipelineError(f"店铺不是正常POP状态：{shop}")
    jingmai.mark_jdo_store_verified(active_profile, vender_id, shop_name)
    return {
        "profile": active_profile,
        "venderId": vender_id,
        "shopName": shop_name,
        "shopId": int(shop.get("shopId") or 0),
    }


def product_apply_rejection(product_id: int) -> dict[str, Any] | None:
    try:
        detail = jingmai.get(
            "jdo-product-update",
            "/rest/sp-product/v0/product-apply/{applyId}",
            {"applyId": str(product_id), "scene": "pop"},
        ).get("data") or {}
    except jingmai.JdoError:
        return None
    apply_info = detail.get("applyInfo") or {}
    draft_status = int(apply_info.get("draftComputeStatus") or 0)
    if draft_status != 3:
        return None
    rejection = {
        "applyId": str(apply_info.get("applyId") or product_id),
        "draftComputeStatus": draft_status,
        "bossAuditStatus": int(apply_info.get("bossAuditStatus") or 0),
    }
    try:
        response = jingmai.get(
            "jdo-product-update",
            "/rest/sp-product/v0/product-applies",
            {
                "page": 1,
                "pageSize": 10,
                "sortBy": "created",
                "sortType": "desc",
                "productIdList": [product_id],
                "scopeSet": [
                    "productId", "productName", "auditReason", "pushReason",
                    "pushStatus", "status", "keeperStatus",
                ],
            },
        )
    except jingmai.JdoError:
        return rejection
    row = next(
        (
            item for item in response.get("data") or []
            if int(item.get("productId") or 0) == product_id
        ),
        None,
    )
    if row:
        rejection.update({
            key: row.get(key)
            for key in (
                "productName", "status", "keeperStatus", "pushStatus",
                "auditReason", "pushReason",
            )
            if row.get(key) not in (None, "")
        })
    return rejection


def list_products(category_id: int | None = None, item_num: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    expected_total: int | None = None
    page = 1
    while True:
        params: dict[str, Any] = {
            "scopeSet": "productId,productName,productStatus,categoryId,itemNum,created,modified",
            "page": page,
            "pageSize": 50,
            "sortBy": "created",
            "sortType": "asc",
        }
        if category_id is not None:
            params["categoryIdList"] = [category_id]
        if item_num:
            params["itemNum"] = item_num
        response = jingmai.get("jdo-product-status", "/rest/sp-product/v0/products", params)
        batch = response.get("data") or []
        pagination = response.get("paginationData") or {}
        if "totalItems" not in pagination:
            raise PipelineError("商品列表缺少分页元数据，不能确认全量")
        total = int(pagination["totalItems"])
        if total == 0 and not batch:
            return []
        if "currentPage" not in pagination:
            raise PipelineError("商品列表缺少页码元数据，不能确认全量")
        if expected_total is None:
            expected_total = total
        if total < 0 or total != expected_total or int(pagination["currentPage"]) != page:
            raise PipelineError("商品列表总量或页码发生变化，请重新只读扫描")
        for product in batch:
            product_id = str(product.get("productId") or "")
            if not product_id or product_id in seen_ids:
                raise PipelineError("商品列表存在缺失或重复ID，不能确认全量")
            seen_ids.add(product_id)
        rows.extend(batch)
        if len(rows) > total or (not batch and len(rows) < total):
            raise PipelineError("商品列表实际数量与分页总量不一致")
        if len(rows) == total:
            return rows
        page += 1


def find_existing_product(source_spu: str) -> dict[str, Any] | None:
    matches = list_products(item_num=source_spu)
    if len(matches) > 1:
        raise PipelineError(f"货号{source_spu}匹配到多个线上商品，停止自动处理")
    return matches[0] if matches else None


def load_batch_preflight_decisions(path: Path | None, urls: list[str]) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        raise PipelineError("批量任务缺少完整的 AI/类目预检决策文件")
    payload = mover.read_json(path)
    if isinstance(payload, list):
        records = payload
    else:
        records = payload.get("decisions")
        if records is None:
            records = [*(payload.get("categorySkips") or []), *(payload.get("aiReviews") or [])]
    if not isinstance(records, list):
        raise PipelineError("批量预检决策文件格式错误：缺少 decisions 数组")
    by_sku: dict[str, dict[str, Any]] = {}
    for raw in records:
        if not isinstance(raw, dict):
            raise PipelineError("批量预检决策必须为对象")
        source_sku = str(raw.get("sourceSku") or "")
        decision = str(raw.get("decision") or raw.get("stage") or "")
        reason = str(raw.get("reason") or "").strip()
        if not source_sku or decision not in BATCH_PREFLIGHT_DECISIONS or not reason:
            raise PipelineError(f"批量预检决策字段无效：{raw}")
        if source_sku in by_sku:
            raise PipelineError(f"批量预检存在重复源SKU决策：{source_sku}")
        if decision == "SKIPPED_AI_DUPLICATE" and not (
            raw.get("matchedProductId") or raw.get("matchedBatchSourceSku")
        ):
            raise PipelineError(f"AI重复决策缺少匹配对象：{source_sku}")
        by_sku[source_sku] = {**raw, "decision": decision, "reason": reason}
    requested = [mover.source_sku_from_url(url) for url in urls]
    missing = [source_sku for source_sku in requested if source_sku not in by_sku]
    if missing:
        raise PipelineError(f"以下批量链接缺少 AI/类目预检决策：{missing}")
    return {source_sku: by_sku[source_sku] for source_sku in requested}


def record_batch_preflight_skip(
    *, url: str, output_root: Path, decision: dict[str, Any],
) -> dict[str, Any]:
    source_sku = mover.source_sku_from_url(url)
    state_path = output_root / "state" / f"{source_sku}.json"
    existing_state = mover.read_json(state_path) if state_path.exists() else {}
    if existing_state.get("productId"):
        raise PipelineError(
            f"源SKU {source_sku} 已存在 productId={existing_state['productId']}，"
            "不得改写为跳过状态；请按原商品恢复"
        )
    stage = str(decision["decision"])
    save_state(
        state_path,
        stage,
        sourceSku=source_sku,
        sourceUrl=url,
        preflightDecision=decision,
    )
    return {
        "ok": True,
        "skipped": True,
        "stage": stage,
        "sourceSku": source_sku,
        "reason": str(decision["reason"]),
        "state": str(state_path),
    }


def next_style_start(prefix: str, category_id: int = 35381) -> tuple[int, list[str]]:
    product_rows = list_products(category_id=category_id)
    used: list[str] = []
    pattern = re.compile(rf"^{re.escape(prefix)}(\d{{5}})[A-Z]+$")
    for row in product_rows:
        product_id = int(row.get("productId") or 0)
        if not product_id:
            continue
        detail = jingmai.get(
            "jdo-product-status",
            "/rest/sp-product/v0/products/{productId}",
            {"productId": product_id, "scene": "pop"},
        ).get("data") or {}
        for sku in detail.get("skuList") or []:
            outer_id = str(sku.get("outerId") or "").strip()
            if pattern.fullmatch(outer_id):
                used.append(outer_id)
    numbers = [int(pattern.fullmatch(value).group(1)) for value in used]
    return (max(numbers) + 1 if numbers else 10000), used


def local_draft(
    url: str, output_root: Path, rules: dict[str, Any], reference_path: Path,
) -> tuple[Path, dict[str, Any]]:
    source_sku = mover.source_sku_from_url(url)
    folder = existing_folder(source_sku, output_root)
    if folder and (folder / "listing.json").exists():
        previous = mover.read_json(folder / "listing.json")
        source = mover.read_json(folder / "manifest.json")
        style_start = int((previous.get("product") or {}).get("vendorStyleStart") or 10000)
        listing = mover.build_listing(
            source, folder, rules, mover.read_json(reference_path), style_start,
        )
        mover.write_json(folder / "listing.json", listing)
        mover.build_audit_workbook(source, listing, folder / f"{folder.name}.xlsx")
        return folder, listing
    source = mover.collect_source(url, mover.HttpClient())
    prefix = mover.infer_prefix(source["title"], rules)
    style_start, _ = next_style_start(prefix, int(rules["category"]["id"]))
    folder = mover.materialize(source, output_root, mover.HttpClient())
    source = mover.read_json(folder / "manifest.json")
    listing = mover.build_listing(source, folder, rules, mover.read_json(reference_path), style_start)
    mover.write_json(folder / "listing.json", listing)
    mover.build_audit_workbook(source, listing, folder / f"{folder.name}.xlsx")
    return folder, listing


def validation_violations(response: dict[str, Any]) -> list[dict[str, Any]]:
    return ((response.get("data") or {}).get("wareKeeperDetectResultList") or [])


def response_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data") or []
    if isinstance(data, list):
        return data
    return data.get("data") or data.get("list") or data.get("videoList") or []


def video_zone_id() -> str:
    zones = jingmai.get("jdo-product-material", "/rest/sp-product/v0/media-zones").get("data") or []
    video_zone = next((item for item in zones if int(item.get("zoneType") or 0) == 2), None)
    if not video_zone:
        raise PipelineError("当前店铺没有视频空间")
    return str(video_zone["zoneId"])


def query_video(zone_id: str, video_id: int) -> dict[str, Any] | None:
    response = jingmai.get(
        "jdo-product-material",
        "/rest/sp-product/v0/media-zones/{zoneId}/videos",
        {
            "zoneId": zone_id,
            "videoType": VIDEO_TYPE,
            "videoIdSet": [video_id],
            "page": 1,
            "pageSize": 10,
            "scopeSet": [
                "videoId", "jdVideoId", "videoName", "videoType", "status",
                "indexImage", "playUrl", "duration", "videoSize", "createDate",
            ],
        },
    )
    return next(
        (item for item in response_rows(response) if int(item.get("videoId") or 0) == video_id),
        None,
    )


def query_video_relations(zone_id: str, video_id: int) -> list[dict[str, Any]]:
    relations: list[dict[str, Any]] = []
    page = 1
    page_size = 50
    while True:
        response = jingmai.get(
            "jdo-product-material",
            "/rest/sp-product/v0/media-zones/{zoneId}/video-sku-rels",
            {
                "zoneId": zone_id,
                "videoType": VIDEO_MATERIAL_TYPE,
                "videoIds": [video_id],
                "page": page,
                "pageSize": page_size,
                "scopeSet": [
                    "videoId", "productId", "skuId", "status", "createdDate",
                    "modifiedDate", "videoType",
                ],
            },
        )
        rows = response.get("data") or []
        relations.extend(rows)
        total_items = int((response.get("paginationData") or {}).get("totalItems") or 0)
        if (total_items and len(relations) >= total_items) or len(rows) < page_size:
            break
        page += 1
    return relations


def existing_video_materials(sku_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    materials_by_sku: dict[int, list[dict[str, Any]]] = {}
    for start in range(0, len(sku_ids), 50):
        response = jingmai.get(
            "jdo-product-material",
            "/rest/sp-product/v0/sku-materials",
            {
                "skuIds": sku_ids[start:start + 50],
                "materialTypes": [VIDEO_MATERIAL_TYPE],
                "scopeSet": [3],
            },
        )
        for row in response.get("data") or []:
            materials_by_sku[int(row["skuId"])] = [
                item for item in row.get("materials") or []
                if int(item.get("materialType") or 0) == VIDEO_MATERIAL_TYPE
            ]
    return materials_by_sku


def automate_main_video(
    *, state_path: Path, state: dict[str, Any], folder: Path, source: dict[str, Any],
    product_id: int, transcode_attempts: int, review_attempts: int, interval: int,
) -> dict[str, Any] | None:
    video = source.get("video") or {}
    local_path = str(video.get("localPath") or "").strip()
    if not local_path:
        return None
    video_path = (folder / local_path).resolve()
    if not video_path.is_file():
        raise PipelineError(f"主图视频文件不存在：{video_path}")
    size_bytes = video_path.stat().st_size
    duration_seconds = int(video.get("durationSeconds") or 0)
    if not 5 <= duration_seconds <= 90:
        raise PipelineError(f"主图视频时长必须为5至90秒：当前{duration_seconds}秒")
    if size_bytes > 200 * 1024 * 1024:
        raise PipelineError(f"主图视频必须小于等于200MB：当前{size_bytes}字节")

    runtime_dir = folder / ".pipeline" / "video"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    automation = dict(state.get("videoAutomation") or {})
    zone_id = str(automation.get("zoneId") or video_zone_id())
    saved_video: dict[str, Any] | None = None
    video_id = int(automation.get("videoId") or 0)
    if video_id:
        saved_video = query_video(zone_id, video_id)
    if not saved_video:
        upload_response = jingmai.get(
            "jdo-product-material",
            "/rest/sp-product/v0/media-zones/{zoneId}/upload-urls",
            {"zoneId": zone_id, **video_upload_url_params(video_path, size_bytes)},
        )
        upload_info = upload_response.get("data") or {}
        upload_url = str(upload_info.get("httpsUploadUrl") or upload_info.get("uploadUrl") or "")
        jd_video_id = int(upload_info.get("jdVideoId") or 0)
        if not upload_url or not jd_video_id:
            raise PipelineError("获取视频上传地址返回不完整")
        jingmai.recheck_verified_jdo_store()
        with video_path.open("rb") as handle:
            transfer = requests.post(
                upload_url,
                files={"video_file": (video_path.name, handle, "video/mp4")},
                timeout=300,
            )
        if not 200 <= transfer.status_code < 300:
            raise PipelineError(f"视频文件传输失败：HTTP {transfer.status_code}")
        mover.write_json(runtime_dir / "transfer-result.json", {
            "statusCode": transfer.status_code,
            "jdVideoId": jd_video_id,
            "fileSize": size_bytes,
        })
        save_body = {
            "zoneVideoDTO": {
                "videoName": str(source.get("title") or "商品主图视频")[:18],
                "videoType": VIDEO_TYPE,
                "videoDesc": "自动搬运商品主图展示视频",
                "videoSize": size_bytes,
                "jdVideoId": jd_video_id,
            },
        }
        save_path = runtime_dir / "save-video-body.json"
        mover.write_json(save_path, save_body)
        save_response = jingmai.run_jdo_large(
            url="/rest/sp-product/v0/media-zones/{zoneId}/videos",
            method="POST",
            body_path=save_path,
            params={"zoneId": zone_id},
            skill_id="jdo-product-material",
            yes=True,
        )
        mover.write_json(runtime_dir / "save-video-response.json", save_response)
        saved = save_response.get("data") or {}
        video_id = int(saved.get("videoId") or 0)
        if not video_id:
            raise PipelineError("保存视频信息后未返回videoId")
        automation.update({
            "zoneId": zone_id,
            "videoId": video_id,
            "jdVideoId": int(saved.get("jdVideoId") or jd_video_id),
            "sizeBytes": size_bytes,
            "sourceVideoPath": str(video_path),
        })
        state = save_state(state_path, "VIDEO_SAVED", videoAutomation=automation)

    for attempt in range(1, transcode_attempts + 1):
        saved_video = query_video(zone_id, video_id)
        if saved_video and video_is_ready(int(saved_video.get("status") or 0)):
            break
        if saved_video and int(saved_video.get("status") or 0) in {5, 6}:
            raise PipelineError(f"主图视频转码或审核失败：{saved_video}")
        if attempt < transcode_attempts:
            time.sleep(interval)
    if not saved_video or not video_is_ready(int(saved_video.get("status") or 0)):
        raise PipelineError(f"主图视频转码未在限定时间内完成：videoId={video_id}")
    automation.update({
        "status": "transcoded",
        "durationSeconds": int(saved_video.get("duration") or duration_seconds),
        "indexImage": str(saved_video.get("indexImage") or ""),
    })
    state = save_state(state_path, "VIDEO_TRANSCODED", videoAutomation=automation)

    category_ids = [int(value) for value in source.get("categoryIds") or []]
    second_category_id = category_ids[-2] if len(category_ids) >= 2 else category_ids[-1]
    tags = jingmai.get(
        "jdo-product-material",
        "/rest/sp-product/v0/sku-material-tags",
        {"materialType": VIDEO_MATERIAL_TYPE, "secondCategoryId": second_category_id},
    ).get("data") or []
    tag = next(
        (item for item in tags if item.get("tagName") == "产品展示"),
        next((item for item in tags if not (item.get("tagRule") or {}).get("salerVideoFlag")), None),
    )
    if not tag:
        raise PipelineError("当前类目没有可用的商家多主图视频标签")
    detail = jingmai.get(
        "jdo-product-status",
        "/rest/sp-product/v0/products/{productId}",
        {"productId": product_id, "scene": "pop"},
    ).get("data") or {}
    sku_ids = sorted(int(item["skuId"]) for item in detail.get("skuList") or [])
    if not sku_ids:
        raise PipelineError("商品详情未返回SKU，无法绑定主图视频")
    materials = existing_video_materials(sku_ids)
    bound_sku_ids: list[int] = []
    for sku_id in sku_ids:
        existing = materials.get(sku_id) or []
        existing_video_ids = {
            int((item.get("videoMaterial") or {}).get("videoId") or 0)
            for item in existing
        } - {0}
        if existing_video_ids == {video_id}:
            bound_sku_ids.append(sku_id)
            continue
        if existing_video_ids:
            raise PipelineError(f"SKU {sku_id} 已绑定其他主图视频，禁止覆盖：{sorted(existing_video_ids)}")
        body_path = runtime_dir / f"bind-{sku_id}.json"
        mover.write_json(body_path, build_video_binding_body(
            product_id=product_id, sku_id=sku_id, video=saved_video, tag=tag,
        ))
        response = jingmai.run_jdo_large(
            url="/rest/sp-product/v0/sku-materials/batch",
            method="POST",
            body_path=body_path,
            skill_id="jdo-product-material",
            yes=True,
        )
        mover.write_json(runtime_dir / f"bind-{sku_id}-response.json", response)
        bound_sku_ids.append(sku_id)
        automation["boundSkuIds"] = bound_sku_ids
        state = save_state(state_path, "VIDEO_BINDING", videoAutomation=automation)

    audit: dict[str, Any] = {}
    relations: list[dict[str, Any]] = []
    for attempt in range(1, review_attempts + 1):
        relations = query_video_relations(zone_id, video_id)
        audit = video_relation_audit(relations, sku_ids, video_id)
        mover.write_json(runtime_dir / "binding-audit.json", {**audit, "relations": relations})
        if audit["allApproved"] or audit["rejectedSkuIds"]:
            break
        if attempt < review_attempts:
            time.sleep(interval)
    if audit.get("rejectedSkuIds"):
        raise PipelineError(f"主图视频绑定审核被驳回：{audit['rejectedSkuIds']}")
    automation.update({
        "status": "verified" if audit.get("allApproved") else "review_pending",
        "tagId": int(tag["tagId"]),
        "tagName": str(tag["tagName"]),
        "skuCount": len(sku_ids),
        "audit": audit,
        "manualUploadRequired": False,
    })
    save_state(
        state_path,
        "VIDEO_VERIFIED" if audit.get("allApproved") else "VIDEO_REVIEW_PENDING",
        videoAutomation=automation,
    )
    return automation


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    source_sku = mover.source_sku_from_url(args.url)
    state_path = args.output / "state" / f"{source_sku}.json"
    lock_path = args.output / "state" / f"{source_sku}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_handle)
    except FileExistsError as exc:
        raise PipelineError(f"同一源SKU已有任务运行：{source_sku}") from exc

    try:
        state = save_state(state_path, "RECEIVED", sourceUrl=args.url, sourceSku=source_sku)
        store_config = mover.read_json(args.store_config)
        rules = effective_rules(mover.read_json(args.rules), store_config)
        if not args.reference.is_file():
            raise PipelineError(
                f"缺少店铺类目参考文件：{args.reference}；请先运行 jdo_listing_mvp.py refresh-reference"
            )
        store = verify_store_identity(store_config, args.profile)
        state = save_state(state_path, "STORE_VERIFIED", store=store)

        folder, listing = local_draft(args.url, args.output, rules, args.reference)
        listing_path = folder / "listing.json"
        source = mover.read_json(folder / "manifest.json")
        listing.setdefault("source", {})["pricing"] = rules["pricing"]
        listing["product"]["title"] = mover.listing_title(
            str(source["title"]), str(source.get("sourceBrandName") or ""),
        )
        listing["product"]["includeSizeSaleAttribute"] = len(source.get("saleProperties") or {}) >= 2
        listing["product"]["itemNumber"] = source["sourceSpu"]
        listing["product"]["mainImageMode"] = rules["productDefaults"]["mainImageMode"]
        listing["product"]["freightTemplateId"] = rules["productDefaults"]["freightTemplateId"]
        listing["product"]["freightTemplateLabel"] = rules["productDefaults"]["freightTemplateLabel"]
        video_handoff = mover.video_handoff(folder, listing)
        prefix = str(listing["product"]["vendorSkuPrefix"])
        if state.get("productId"):
            current_start = int(listing["product"]["vendorStyleStart"])
            used_codes: list[str] = []
        else:
            current_start, used_codes = next_style_start(prefix, int(rules["category"]["id"]))
        if not state.get("productId") and int(listing["product"]["vendorStyleStart"]) != current_start:
            listing["product"]["vendorStyleStart"] = current_start
        mover.write_json(listing_path, listing)
        mover.build_audit_workbook(source, listing, folder / f"{folder.name}.xlsx")
        state = save_state(
            state_path,
            "LOCAL_DRAFT_READY",
            folder=str(folder),
            listing=str(listing_path),
            sourceSpu=source["sourceSpu"],
            vendorSkuPrefix=prefix,
            vendorStyleStart=int(listing["product"]["vendorStyleStart"]),
            usedVendorSkuCount=len(used_codes),
            videoHandoff=video_handoff,
        )

        errors, warnings = jingmai.validate_manifest(
            listing_path, listing, jingmai.load_json(args.reference),
        )
        if errors:
            save_state(state_path, "FAILED_LOCAL_VALIDATION", errors=errors, warnings=warnings)
            raise PipelineError("本地预检失败：" + "; ".join(errors))
        state = save_state(state_path, "LOCAL_VALIDATED", warnings=warnings)

        product_id = int(state.get("productId") or 0)
        if not product_id:
            existing = find_existing_product(str(listing["product"]["itemNumber"]))
            if existing:
                product_id = int(existing["productId"])
                state = save_state(
                    state_path,
                    "EXISTING_PRODUCT_FOUND",
                    productId=product_id,
                    existingProduct=existing,
                )

        runtime_dir = folder / ".pipeline"
        uploaded_path = runtime_dir / "listing.uploaded.json"
        publish_dir = runtime_dir / "publish"
        validation_path = publish_dir / "validation-response.json"
        publish_response_path = publish_dir / "publish-response.json"
        verification_path = publish_dir / "verification.json"

        if not product_id:
            upload_result = jingmai.command_upload(SimpleNamespace(
                input=listing_path,
                reference=args.reference,
                output=uploaded_path,
                confirm="UPLOAD_TO_JD",
            ))
            if upload_result:
                save_state(state_path, "FAILED_UPLOAD")
                raise PipelineError("图片上传失败")
            state = save_state(state_path, "MEDIA_UPLOADED", uploadedListing=str(uploaded_path))

            payload_info = jingmai.write_publish_payloads(uploaded_path, publish_dir)
            state = save_state(state_path, "LOCAL_DRAFT_BUILT", payloads=payload_info)
            validation_response = jingmai.run_jdo_large(
                url="/rest/sp-product/v0/product-schemas/validations",
                method="POST",
                body_path=publish_dir / "validation-body.json",
                skill_id="jdo-product-publish",
                yes=True,
            )
            mover.write_json(validation_path, validation_response)
            violations = validation_violations(validation_response)
            strong = [item for item in violations if int(item.get("detectResult") or 0) == 1]
            if strong:
                save_state(state_path, "FAILED_JD_VALIDATION", violations=violations)
                raise PipelineError(f"京东合规检测存在{len(strong)}项强违规")
            state = save_state(
                state_path,
                "JD_VALIDATED",
                validationResponse=str(validation_path),
                violationCount=len(violations),
            )
            if not args.publish:
                return {
                    "ok": True,
                    "stage": "JD_VALIDATED",
                    "state": str(state_path),
                    "folder": str(folder),
                    "videoHandoff": video_handoff,
                }
            if args.confirm != "PUBLISH_TO_JD":
                raise PipelineError("正式上架必须同时传 --publish --confirm PUBLISH_TO_JD")

            publish_response = jingmai.run_jdo_large(
                url="/rest/sp-product/v0/product-applies",
                method="POST",
                body_path=publish_dir / "publish-body.json",
                skill_id="jdo-product-publish",
                yes=True,
            )
            mover.write_json(publish_response_path, publish_response)
            product_id = int((publish_response.get("data") or {}).get("productId") or 0)
            if not product_id:
                save_state(state_path, "FAILED_PUBLISH", publishResponse=str(publish_response_path))
                raise PipelineError("京东发品返回中缺少productId")
            state = save_state(
                state_path,
                "PUBLISHED",
                productId=product_id,
                applyId=(publish_response.get("data") or {}).get("applyId"),
                publishResponse=str(publish_response_path),
            )

        last_result: dict[str, Any] = {}
        product_verified = False
        for attempt in range(1, args.verify_attempts + 1):
            try:
                return_code = jingmai.command_verify_product(SimpleNamespace(
                    input=uploaded_path if uploaded_path.exists() else listing_path,
                    product_id=product_id,
                    output=verification_path,
                ))
            except jingmai.JdoError as exc:
                last_result = {"ok": False, "errors": [str(exc)], "attempt": attempt}
                if "未查询到商品信息" not in str(exc):
                    raise
                rejection = product_apply_rejection(product_id)
                if rejection:
                    save_state(
                        state_path, "PRODUCT_APPLY_REJECTED", productId=product_id,
                        productApply=rejection, verification=last_result,
                    )
                    raise PipelineError(
                        f"商品{product_id}申请已驳回，禁止重新创建；请按驳回原因修正原申请"
                    ) from exc
                if attempt >= args.verify_attempts:
                    save_state(
                        state_path,
                        "PRODUCT_PROPAGATION_PENDING",
                        productId=product_id,
                        verification=last_result,
                    )
                    raise PipelineError(
                        f"商品{product_id}已创建，详情仍在传播；后续仅回查，禁止重新创建"
                    ) from exc
                time.sleep(args.verify_interval)
                continue
            last_result = mover.read_json(verification_path)
            if return_code == 0:
                state = save_state(
                    state_path,
                    "PRODUCT_VERIFIED",
                    verification=str(verification_path),
                    videoHandoff=video_handoff,
                )
                product_verified = True
                break
            if attempt < args.verify_attempts:
                time.sleep(args.verify_interval)
        if not product_verified:
            save_state(state_path, "FAILED_VERIFICATION", verification=last_result)
            raise PipelineError(f"发布后验证未通过：{last_result.get('errors')}")
        video_automation = automate_main_video(
            state_path=state_path,
            state=state,
            folder=folder,
            source=source,
            product_id=product_id,
            transcode_attempts=args.video_transcode_attempts,
            review_attempts=args.video_review_attempts,
            interval=args.video_interval,
        )
        final_stage = (
            "VIDEO_REVIEW_PENDING"
            if video_automation and video_automation.get("status") == "review_pending"
            else "VERIFIED"
        )
        final_handoff = video_handoff
        if final_handoff and video_automation:
            final_handoff = {
                **final_handoff,
                "manualUploadRequired": False,
                "message": "主图视频已自动上传并提交至全部SKU，无需人工上传。",
            }
        save_state(
            state_path,
            final_stage,
            verification=str(verification_path),
            videoAutomation=video_automation,
            videoHandoff=final_handoff,
        )
        return {
            "ok": True,
            "stage": final_stage,
            "productId": product_id,
            "sourceSku": source_sku,
            "folder": str(folder),
            "state": str(state_path),
            "verification": str(verification_path),
            "videoAutomation": video_automation,
            "videoHandoff": final_handoff,
        }
    except Exception as exc:
        if lock_path.parent.exists():
            record_pipeline_exception(state_path, exc)
        raise
    finally:
        lock_path.unlink(missing_ok=True)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--url", required=True)
    run.add_argument("--profile", required=True)
    run.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    run.add_argument("--store-config", type=Path, default=STORE_CONFIG)
    run.add_argument("--rules", type=Path, default=RULES)
    run.add_argument("--reference", type=Path, default=REFERENCE)
    run.add_argument("--publish", action="store_true")
    run.add_argument("--confirm", default="")
    run.add_argument("--verify-attempts", type=int, default=12)
    run.add_argument("--verify-interval", type=int, default=10)
    run.add_argument("--video-transcode-attempts", type=int, default=18)
    run.add_argument("--video-review-attempts", type=int, default=12)
    run.add_argument("--video-interval", type=int, default=10)
    batch = commands.add_parser("batch")
    batch.add_argument("--input", type=Path, required=True)
    batch.add_argument("--profile", required=True)
    batch.add_argument("--preflight-decisions", type=Path, required=True)
    batch.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    batch.add_argument("--store-config", type=Path, default=STORE_CONFIG)
    batch.add_argument("--rules", type=Path, default=RULES)
    batch.add_argument("--reference", type=Path, default=REFERENCE)
    batch.add_argument("--publish", action="store_true")
    batch.add_argument("--confirm", default="")
    batch.add_argument("--verify-attempts", type=int, default=12)
    batch.add_argument("--verify-interval", type=int, default=10)
    batch.add_argument("--video-transcode-attempts", type=int, default=18)
    batch.add_argument("--video-review-attempts", type=int, default=12)
    batch.add_argument("--video-interval", type=int, default=10)
    args = parser.parse_args()
    try:
        if args.command == "run":
            result = run_pipeline(args)
        else:
            urls = [
                line.strip()
                for line in args.input.read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if not urls:
                raise PipelineError("批量链接文件为空")
            decisions = load_batch_preflight_decisions(args.preflight_decisions, urls)
            results = []
            for url in urls:
                item_args = argparse.Namespace(**vars(args), url=url)
                try:
                    source_sku = mover.source_sku_from_url(url)
                    decision = decisions[source_sku]
                    if decision["decision"] == "PROCEED":
                        results.append(run_pipeline(item_args))
                    else:
                        results.append(record_batch_preflight_skip(
                            url=url,
                            output_root=args.output,
                            decision=decision,
                        ))
                except Exception as exc:
                    results.append({"ok": False, "url": url, "error": str(exc)})
                    break
            result = {
                "ok": all(item.get("ok") for item in results) and len(results) == len(urls),
                "requested": len(urls),
                "completed": sum(bool(item.get("ok")) and not item.get("skipped") for item in results),
                "skipped": sum(bool(item.get("skipped")) for item in results),
                "results": results,
            }
            if not result["ok"]:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PipelineError, mover.MoverError, jingmai.JdoError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
