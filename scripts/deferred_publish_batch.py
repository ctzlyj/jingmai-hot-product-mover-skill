from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess

import requests

import jd_product_mover as mover
import jd_product_pipeline as pipeline
import jdo_listing_mvp as jingmai


class VerificationPending(ValueError):
    pass


class BatchStopError(ValueError):
    pass


def read_state(folder):
    path = folder / "state.json"
    return mover.read_json(path) if path.is_file() else {}


def submitted_result(state):
    return {"ok": False, "pending": True, "stage": state.get("stage", "SUBMITTING"),
            "productId": state.get("productId"), "applyId": state.get("applyId")}


def record_response(folder, response):
    data = response.get("data") or {}
    identifiers = {key: data[key] for key in ("productId", "applyId") if data.get(key)}
    try:
        if identifiers:
            pipeline.save_state(folder / "state.json", "PUBLISHED" if identifiers.get("productId") else "SUBMITTING", **identifiers)
    finally:
        mover.write_json(folder / "publish/response.json", response)
    product_id = int(identifiers.get("productId") or 0)
    if not product_id:
        raise VerificationPending("Submission response missing product ID; reconcile without resubmitting")
    return product_id


def verify_submitted(folder, args, verifier):
    plan = mover.read_json(folder / "plan.json")
    store = pipeline.verify_store_identity(mover.read_json(args.store_config), args.profile)
    if store != plan["store"]:
        raise BatchStopError("Confirmed store changed")
    state = read_state(folder)
    if state.get("stage") == "PRODUCT_APPLY_REJECTED":
        raise ValueError("Previously rejected application retained; do not recreate")
    product_id = int(state.get("productId") or 0)
    if not product_id:
        digest = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if state.get("stage") != "SUBMITTING" or state.get("confirmToken") != digest:
            raise BatchStopError("Unknown submission is not bound to the current plan")
        receipt = folder / "publish/response.json"
        if receipt.is_file():
            data = mover.read_json(receipt).get("data") or {}
            identifiers = {key: data[key] for key in ("productId", "applyId") if data.get(key)}
            if identifiers:
                pipeline.save_state(folder / "state.json", "SUBMITTING", **identifiers)
            product_id = int(identifiers.get("productId") or 0)
        apply_id = read_state(folder).get("applyId")
        if not product_id and apply_id:
            application = jingmai.get("jdo-product-update", "/rest/sp-product/v0/product-apply/{applyId}",
                                      {"applyId": str(apply_id), "scene": "pop"}).get("data") or {}
            apply_info = application.get("applyInfo") or {}
            if int(apply_info.get("draftComputeStatus") or 0) == 3:
                pipeline.save_state(folder / "state.json", "PRODUCT_APPLY_REJECTED", productApply=apply_info)
                raise ValueError("Application rejected; retained application ID without recreation")
        if not product_id:
            existing = pipeline.find_existing_product(plan["itemNumber"])
            product_id = int((existing or {}).get("productId") or 0)
        if not product_id:
            raise VerificationPending("Submission still unresolved; list absence does not permit recreation")
        detail = jingmai.get("jdo-product-status", "/rest/sp-product/v0/products/{productId}",
                             {"productId": product_id, "scene": "pop"}).get("data") or {}
        if str((detail.get("productInfo") or {}).get("itemNum")) != plan["itemNumber"]:
            raise BatchStopError("Recovered product does not match the transaction item number")
        pipeline.save_state(folder / "state.json", "PUBLISHED", productId=product_id)
    if product_id == plan.get("originalProductId"):
        raise BatchStopError("Refusing to verify original A as new B")
    result = verifier(folder, product_id)
    if not result.get("ok"):
        raise VerificationPending("Product verification has not completed")
    pipeline.save_state(folder / "state.json", "VERIFIED", productId=product_id)
    return {**result, "pending": False, "stage": "VERIFIED"}


def finalize_progress(progress):
    results = progress["results"]
    progress["succeeded"] = sum(bool(row.get("ok")) for row in results)
    progress["awaitingVerification"] = sum(bool(row.get("pending")) for row in results)
    progress["failed"] = sum(not row.get("ok") and not row.get("pending") for row in results)
    progress["unexecuted"] = progress["requested"] - len(results)
    progress["pending"] = progress["awaitingVerification"] + progress["unexecuted"]
    progress["completed"] = progress["pending"] == 0 and progress.get("stage") != "STOPPED"
    progress["ok"] = progress["completed"] and progress["failed"] == 0
    if progress.get("stage") != "STOPPED":
        progress["stage"] = "PARTIAL" if progress["pending"] else ("COMPLETED_WITH_FAILURES" if progress["failed"] else "VERIFIED")


def transport_error(error):
    return isinstance(error, (TimeoutError, ConnectionError, requests.RequestException,
                              subprocess.TimeoutExpired, jingmai.JdoError))


def fatal_error(error):
    if isinstance(error, (BatchStopError, pipeline.PipelineError, PermissionError)):
        return True
    if re.search(r"\b(?:401|403|unauthorized|forbidden)\b|鉴权|权限|登录|白名单|profile", str(error), re.I):
        return True
    return not isinstance(error, (ValueError, FileNotFoundError, jingmai.JdoError,
                                  TimeoutError, ConnectionError, requests.RequestException, subprocess.TimeoutExpired))


def error_result(folder, error, *, verifying=False):
    state = read_state(folder)
    rejected = state.get("stage") == "PRODUCT_APPLY_REJECTED"
    uncertain = isinstance(error, VerificationPending) or transport_error(error) or fatal_error(error)
    pending = not rejected and (uncertain if verifying else bool(state.get("productId") or state.get("applyId") or state.get("stage") == "SUBMITTING"))
    return {"ok": False, "pending": pending, "skipped": not pending,
            "productId": state.get("productId"), "applyId": state.get("applyId"),
            "productApply": state.get("productApply"),
            "stage": state.get("stage", "FAILED_BEFORE_SUBMISSION"), "error": str(error)}


def run_batch(args, rows, folder_name, lock_name, submit, verifier, *, skip_failures=True, audit_only=False):
    if not audit_only and args.confirm != "PUBLISH_TO_JD":
        raise ValueError("Explicit publication confirmation required")
    limit = args.limit or 0
    if limit < 0:
        raise ValueError("Batch limit must not be negative")
    if len({row["sourceSku"] for row in rows}) != len(rows):
        raise ValueError("Duplicate source items in prepared batch")
    valid = [row for row in rows if row.get("ok", True)]
    selected = valid[:limit] if limit else valid
    progress = {"startedAt": mover.now_iso(), "requested": len(rows), "stage": "RUNNING",
                "phase": "VERIFYING" if audit_only else "SUBMITTING",
                "results": [{**row, "pending": False} for row in rows if not row.get("ok", True)]}
    lock = args.output / lock_name
    handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(handle)

    def save():
        finalize_progress(progress)
        mover.write_json(args.output / "progress.json", progress)

    def save_result(row, result):
        result = {"sourceSku": row["sourceSku"], **result, "checkedAt": mover.now_iso()}
        if row.get("originalProductId"):
            result["originalProductId"] = row["originalProductId"]
        prior = next((index for index, item in enumerate(progress["results"]) if item["sourceSku"] == row["sourceSku"]), None)
        if prior is None:
            progress["results"].append(result)
        else:
            previous = progress["results"][prior]
            if previous.get("error"):
                result["submissionError"] = previous["error"]
            progress["results"][prior] = result
        mover.write_json(args.output / folder_name / row["sourceSku"] / "batch-result.json", result)
        if not result.get("ok") and not result.get("pending"):
            mover.write_json(args.output / folder_name / row["sourceSku"] / "failure.json", result)
        save()

    consecutive_errors = 0
    try:
        store = pipeline.verify_store_identity(mover.read_json(args.store_config), args.profile)
        save()
        for row in selected:
            folder = args.output / folder_name / row["sourceSku"]
            state = read_state(folder)
            if state.get("stage") == "PRODUCT_APPLY_REJECTED":
                save_result(row, error_result(folder, ValueError("Previously rejected application retained")))
                if not audit_only and not skip_failures:
                    raise ValueError("Previously rejected application retained")
                continue
            if audit_only:
                if state.get("productId") or state.get("applyId") or state.get("stage") == "SUBMITTING":
                    save_result(row, submitted_result(state))
                continue
            try:
                if state.get("productId") or state.get("applyId") or state.get("stage") == "SUBMITTING":
                    result = submitted_result(state)
                else:
                    result = submit(folder)
                save_result(row, result)
                consecutive_errors = 0
            except Exception as error:
                save_result(row, error_result(folder, error))
                consecutive_errors = consecutive_errors + 1 if transport_error(error) else 0
                if fatal_error(error) or not skip_failures or consecutive_errors >= 3:
                    raise
                if pipeline.verify_store_identity(mover.read_json(args.store_config), args.profile) != store:
                    raise BatchStopError("Store identity changed after submission error")
        progress["phase"] = "VERIFYING"
        save()
        consecutive_errors = 0
        for row in list(progress["results"]):
            if not row.get("pending"):
                continue
            folder = args.output / folder_name / row["sourceSku"]
            try:
                save_result(row, verify_submitted(folder, args, verifier))
                consecutive_errors = 0
            except Exception as error:
                save_result(row, error_result(folder, error, verifying=True))
                consecutive_errors = consecutive_errors + 1 if transport_error(error) else 0
                if fatal_error(error) or consecutive_errors >= 3:
                    raise
        progress["phase"] = "FINISHED"
        progress["finishedAt"] = mover.now_iso()
    except Exception as error:
        progress.update(stage="STOPPED", error=str(error))
        raise
    finally:
        try:
            save()
            print(json.dumps({key: progress[key] for key in ("phase", "stage", "succeeded", "failed", "awaitingVerification", "unexecuted", "completed")}), flush=True)
        finally:
            lock.unlink(missing_ok=True)
    return progress
