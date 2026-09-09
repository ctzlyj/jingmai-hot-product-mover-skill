#!/usr/bin/env python3
"""Publish or resume an existing local Jingmai listing folder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jd_product_mover as mover
import jd_product_pipeline as pipeline
import jdo_listing_mvp as jingmai


ROOT = Path(__file__).resolve().parents[1]


def existing_product_id(state: dict[str, Any], item_number: str) -> tuple[int, dict[str, Any] | None]:
    product_id = int(state.get("productId") or 0)
    if product_id:
        return product_id, None
    existing = pipeline.find_existing_product(item_number)
    return (int(existing.get("productId") or 0), existing) if existing else (0, None)


def reuse_split_uploaded_images(
    folder: Path, source: dict[str, Any], listing: dict[str, Any],
) -> int:
    parent_sku = str(source.get("sourceParentSku") or "").strip()
    if not parent_sku:
        return 0

    uploaded_by_hash: dict[str, dict[str, Any]] = {}
    for sibling in folder.parent.glob(f"{parent_sku}-P*_*"):
        if sibling.resolve() == folder.resolve():
            continue
        uploaded_path = sibling / ".pipeline" / "listing.uploaded.json"
        if not uploaded_path.is_file():
            continue
        uploaded_listing = mover.read_json(uploaded_path)
        for item in uploaded_listing.get("images") or []:
            upload = item.get("upload") or {}
            image_path = sibling / str(item.get("localPath") or "")
            if upload.get("status") != "uploaded" or not image_path.is_file():
                continue
            uploaded_by_hash[hashlib.sha256(image_path.read_bytes()).hexdigest()] = dict(upload)

    reused = 0
    for item in listing.get("images") or []:
        if (item.get("upload") or {}).get("status") == "uploaded":
            continue
        image_path = folder / str(item.get("localPath") or "")
        if not image_path.is_file():
            continue
        upload = uploaded_by_hash.get(hashlib.sha256(image_path.read_bytes()).hexdigest())
        if upload:
            item["upload"] = dict(upload)
            reused += 1
    return reused


def publish_local_draft(args: argparse.Namespace) -> dict[str, Any]:
    folder = args.folder.resolve()
    listing_path = folder / "listing.json"
    source_path = folder / "manifest.json"
    state_path = folder / "state.json"
    lock_path = folder / ".publish.lock"
    if not listing_path.is_file() or not source_path.is_file() or not state_path.is_file():
        raise pipeline.PipelineError(f"本地草稿不完整：{folder}")
    try:
        lock_handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_handle)
    except FileExistsError as exc:
        raise pipeline.PipelineError(f"该草稿已有发布任务运行：{folder}") from exc

    try:
        state = mover.read_json(state_path)
        source = mover.read_json(source_path)
        listing = mover.read_json(listing_path)
        store_config = mover.read_json(args.store_config)
        rules = pipeline.effective_rules(mover.read_json(args.rules), store_config)
        store = pipeline.verify_store_identity(store_config)
        state = pipeline.save_state(state_path, "STORE_VERIFIED", store=store)

        errors, warnings = jingmai.validate_manifest(
            listing_path, listing, jingmai.load_json(args.reference),
        )
        if errors:
            pipeline.save_state(state_path, "FAILED_LOCAL_VALIDATION", errors=errors, warnings=warnings)
            raise pipeline.PipelineError("本地预检失败：" + "; ".join(errors))
        state = pipeline.save_state(state_path, "LOCAL_VALIDATED", warnings=warnings)

        product_id, existing = existing_product_id(
            state, str((listing.get("product") or {}).get("itemNumber") or ""),
        )
        if existing:
            state = pipeline.save_state(
                state_path, "EXISTING_PRODUCT_FOUND", productId=product_id, existingProduct=existing,
            )

        runtime_dir = folder / ".pipeline"
        uploaded_path = runtime_dir / "listing.uploaded.json"
        publish_dir = runtime_dir / "publish"
        validation_path = publish_dir / "validation-response.json"
        publish_response_path = publish_dir / "publish-response.json"
        verification_path = publish_dir / "verification.json"

        if not product_id:
            reused_image_count = reuse_split_uploaded_images(folder, source, listing)
            if reused_image_count:
                mover.write_json(listing_path, listing)
                state = pipeline.save_state(
                    state_path, "SPLIT_MEDIA_REUSED", reusedUploadedImageCount=reused_image_count,
                )
            prefix = str((listing.get("product") or {}).get("vendorSkuPrefix") or "")
            current_start, used_codes = pipeline.next_style_start(
                prefix, int((listing.get("category") or {}).get("id") or 35381),
            )
            if int(listing["product"].get("vendorStyleStart") or 0) != current_start:
                listing["product"]["vendorStyleStart"] = current_start
                mover.write_json(listing_path, listing)
                mover.build_audit_workbook(source, listing, folder / f"{folder.name}.xlsx")
            state = pipeline.save_state(
                state_path, "LOCAL_DRAFT_READY", folder=str(folder), listing=str(listing_path),
                vendorSkuPrefix=prefix, vendorStyleStart=current_start,
                usedVendorSkuCount=len(used_codes),
            )

            if not uploaded_path.is_file():
                upload_result = jingmai.command_upload(SimpleNamespace(
                    input=listing_path, reference=args.reference,
                    output=uploaded_path, confirm="UPLOAD_TO_JD",
                ))
                if upload_result:
                    pipeline.save_state(state_path, "FAILED_UPLOAD")
                    raise pipeline.PipelineError("图片上传失败")
            state = pipeline.save_state(state_path, "MEDIA_UPLOADED", uploadedListing=str(uploaded_path))

            payload_info = jingmai.write_publish_payloads(uploaded_path, publish_dir)
            state = pipeline.save_state(state_path, "LOCAL_DRAFT_BUILT", payloads=payload_info)
            validation_response = jingmai.run_jdo_large(
                url="/rest/sp-product/v0/product-schemas/validations", method="POST",
                body_path=publish_dir / "validation-body.json",
                skill_id="jdo-product-publish", yes=True,
            )
            mover.write_json(validation_path, validation_response)
            violations = pipeline.validation_violations(validation_response)
            strong = [item for item in violations if int(item.get("detectResult") or 0) == 1]
            if strong:
                pipeline.save_state(state_path, "FAILED_JD_VALIDATION", violations=violations)
                raise pipeline.PipelineError(f"京东合规检测存在{len(strong)}项强违规")
            state = pipeline.save_state(
                state_path, "JD_VALIDATED", validationResponse=str(validation_path),
                violationCount=len(violations),
            )
            if args.confirm != "PUBLISH_TO_JD":
                raise pipeline.PipelineError("正式上架必须传 --confirm PUBLISH_TO_JD")
            publish_response = jingmai.run_jdo_large(
                url="/rest/sp-product/v0/product-applies", method="POST",
                body_path=publish_dir / "publish-body.json",
                skill_id="jdo-product-publish", yes=True,
            )
            mover.write_json(publish_response_path, publish_response)
            product_id = int((publish_response.get("data") or {}).get("productId") or 0)
            if not product_id:
                pipeline.save_state(state_path, "FAILED_PUBLISH", publishResponse=str(publish_response_path))
                raise pipeline.PipelineError("京东发品返回中缺少productId")
            state = pipeline.save_state(
                state_path, "PUBLISHED", productId=product_id,
                applyId=(publish_response.get("data") or {}).get("applyId"),
                publishResponse=str(publish_response_path),
            )

        last_result: dict[str, Any] = {}
        for attempt in range(1, args.verify_attempts + 1):
            try:
                return_code = jingmai.command_verify_product(SimpleNamespace(
                    input=uploaded_path if uploaded_path.exists() else listing_path,
                    product_id=product_id, output=verification_path,
                ))
            except jingmai.JdoError as exc:
                last_result = {"ok": False, "errors": [str(exc)], "attempt": attempt}
                if "未查询到商品信息" not in str(exc):
                    raise
                rejection = pipeline.product_apply_rejection(product_id)
                if rejection:
                    pipeline.save_state(
                        state_path, "PRODUCT_APPLY_REJECTED", productId=product_id,
                        productApply=rejection, verification=last_result,
                    )
                    raise pipeline.PipelineError(
                        f"商品{product_id}申请已驳回，禁止重新创建；请按驳回原因修正原申请"
                    ) from exc
                if attempt >= args.verify_attempts:
                    pipeline.save_state(
                        state_path, "PRODUCT_PROPAGATION_PENDING", productId=product_id,
                        verification=last_result,
                    )
                    raise pipeline.PipelineError(
                        f"商品{product_id}已创建，详情仍在传播；后续仅回查，禁止重新创建"
                    ) from exc
                time.sleep(args.verify_interval)
                continue
            last_result = mover.read_json(verification_path)
            if return_code == 0 and last_result.get("ok"):
                break
            if attempt < args.verify_attempts:
                time.sleep(args.verify_interval)
        else:
            pipeline.save_state(state_path, "FAILED_VERIFICATION", verification=last_result)
            raise pipeline.PipelineError("发布后商品审计失败")

        state = pipeline.save_state(
            state_path, "PRODUCT_VERIFIED", productId=product_id,
            verification=str(verification_path),
        )
        video_automation = pipeline.automate_main_video(
            state_path=state_path, state=state, folder=folder, source=source,
            product_id=product_id, transcode_attempts=args.video_transcode_attempts,
            review_attempts=args.video_review_attempts, interval=args.video_interval,
        )
        final_stage = (
            "VIDEO_REVIEW_PENDING"
            if video_automation and video_automation.get("status") == "review_pending"
            else "VERIFIED"
        )
        pipeline.save_state(
            state_path, final_stage, productId=product_id,
            verification=str(verification_path), videoAutomation=video_automation,
        )
        return {
            "ok": True, "stage": final_stage, "productId": product_id,
            "folder": str(folder), "state": str(state_path),
            "verification": str(verification_path), "videoAutomation": video_automation,
        }
    except Exception as exc:
        pipeline.record_pipeline_exception(state_path, exc)
        raise
    finally:
        lock_path.unlink(missing_ok=True)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--store-config", type=Path, default=ROOT / "config" / "store.local.json")
    parser.add_argument("--rules", type=Path, default=ROOT / "config" / "modern-decorative-painting.rules.json")
    parser.add_argument("--reference", type=Path, default=ROOT / "config" / "modern-decorative-painting.reference.json")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--verify-attempts", type=int, default=12)
    parser.add_argument("--verify-interval", type=int, default=10)
    parser.add_argument("--video-transcode-attempts", type=int, default=18)
    parser.add_argument("--video-review-attempts", type=int, default=12)
    parser.add_argument("--video-interval", type=int, default=10)
    args = parser.parse_args()
    try:
        print(json.dumps(publish_local_draft(args), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
