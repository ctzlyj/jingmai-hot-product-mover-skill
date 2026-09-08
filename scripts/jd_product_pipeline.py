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


class PipelineError(RuntimeError):
    pass


def video_is_ready(status: int) -> bool:
    return status in {2, 4}


def validate_store_config(config: dict[str, Any]) -> None:
    store = config.get("store") or {}
    business = config.get("business") or {}
    if int(store.get("venderId") or 0) <= 0:
        raise ValueError("店铺配置缺少有效的 store.venderId")
    if not str(store.get("shopName") or "").strip() or str(store.get("shopName")) == "YOUR_SHOP_NAME":
        raise ValueError("店铺配置缺少有效的 store.shopName")
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
    state["stage"] = stage
    state["updatedAt"] = mover.now_iso()
    state.setdefault("history", []).append({"stage": stage, "at": state["updatedAt"]})
    mover.write_json(path, state)
    return state


def existing_folder(source_sku: str, output_root: Path) -> Path | None:
    matches = sorted(path for path in output_root.glob(f"{source_sku}_*") if path.is_dir())
    if len(matches) > 1:
        raise PipelineError(f"同一源SKU存在多个运行目录：{matches}")
    return matches[0] if matches else None


def verify_store_identity(store_config: dict[str, Any]) -> dict[str, Any]:
    validate_store_config(store_config)
    expected = store_config["store"]
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
    return {"venderId": vender_id, "shopName": shop_name, "shopId": int(shop.get("shopId") or 0)}


def list_products(category_id: int | None = None, item_num: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        params: dict[str, Any] = {
            "scopeSet": "productId,productName,productStatus,categoryId,itemNum,created,modified",
            "page": page,
            "pageSize": 50,
        }
        if category_id is not None:
            params["categoryIdList"] = [category_id]
        if item_num:
            params["itemNum"] = item_num
        response = jingmai.get("jdo-product-status", "/rest/sp-product/v0/products", params)
        batch = response.get("data") or []
        rows.extend(batch)
        pagination = response.get("paginationData") or {}
        total = int(pagination.get("totalItems") or len(rows))
        if not batch or len(rows) >= total:
            return rows
        page += 1


def find_existing_product(source_spu: str) -> dict[str, Any] | None:
    matches = list_products(item_num=source_spu)
    if len(matches) > 1:
        raise PipelineError(f"货号{source_spu}匹配到多个线上商品，停止自动处理")
    return matches[0] if matches else None


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
        listing = mover.read_json(folder / "listing.json")
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
    response = jingmai.get(
        "jdo-product-material",
        "/rest/sp-product/v0/media-zones/{zoneId}/video-sku-rels",
        {
            "zoneId": zone_id,
            "videoType": VIDEO_MATERIAL_TYPE,
            "videoIds": [video_id],
            "page": 1,
            "pageSize": 50,
            "scopeSet": [
                "videoId", "productId", "skuId", "status", "createdDate",
                "modifiedDate", "videoType",
            ],
        },
    )
    return response.get("data") or []


def existing_video_materials(sku_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    response = jingmai.get(
        "jdo-product-material",
        "/rest/sp-product/v0/sku-materials",
        {"skuIds": sku_ids, "materialTypes": [VIDEO_MATERIAL_TYPE], "scopeSet": [3]},
    )
    return {
        int(row["skuId"]): [
            item for item in row.get("materials") or []
            if int(item.get("materialType") or 0) == VIDEO_MATERIAL_TYPE
        ]
        for row in response.get("data") or []
    }


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
        store = verify_store_identity(store_config)
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
                if "未查询到商品信息" not in str(exc) or attempt >= args.verify_attempts:
                    raise
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
            save_state(state_path, "FAILED", error=str(exc))
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
            results = []
            for url in urls:
                item_args = argparse.Namespace(**vars(args), url=url)
                try:
                    results.append(run_pipeline(item_args))
                except Exception as exc:
                    results.append({"ok": False, "url": url, "error": str(exc)})
                    break
            result = {
                "ok": all(item.get("ok") for item in results) and len(results) == len(urls),
                "requested": len(urls),
                "completed": sum(bool(item.get("ok")) for item in results),
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
