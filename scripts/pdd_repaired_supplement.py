from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import jd_product_mover as mover
import jd_product_pipeline as pipeline
import jdo_listing_mvp as jingmai
import deferred_publish_batch as batch
from pdd_ab_image_publish import digest_json, file_hash, require_confirmation
from pdd_local_export_mvp import harmonize_sku_main_images, parse_export_folder
from publish_local_draft import reuse_shared_uploaded_images, update_shared_uploaded_images
from component_rounded_pricing import create_price_plan

ROOT = Path(__file__).resolve().parents[1]


def require_codes(variants):
    codes = [str(row.get("vendorSku") or "").strip() for row in variants]
    if not codes or not all(codes):
        raise ValueError("表格商家SKU为空；不自动生成编码")
    if len(codes) != len(set(codes)):
        raise ValueError("表格商家SKU重复；不自动改码")


@lru_cache(maxsize=2048)
def solve_price(source_price):
    return create_price_plan(Decimal(source_price) * 2, Decimal("0.15"), Decimal("0.15"), target_gap=Decimal("0"))


def set_exact_prices(listing, source):
    originals = {row["sourceSku"]: row for row in source["variants"]}
    require_codes(source["variants"])
    audit = []
    for sku in listing["skus"]:
        original = originals[sku["sourceSku"]]
        if sku["vendorSku"] != original["vendorSku"]:
            raise ValueError("商家SKU发生变化")
        price = solve_price(original["sourcePrice"])
        sku["jdPrice"] = float(price.jd_price)
        audit.append({"sourceSku": sku["sourceSku"], "merchantSku": sku["vendorSku"],
                      "pddPrice": original["sourcePrice"], "jdPrice": str(price.jd_price),
                      "directReduction": str(price.direct_reduction), "subsidy": str(price.government_subsidy),
                      "targetArrival": str(price.target_final_price), "formulaArrival": str(price.final_price)})
    listing["source"] = {}
    return audit


def require_resumable_state(state):
    if state.get("stage") == "PRODUCT_APPLY_REJECTED":
        raise ValueError(f"已驳回申请保留，不重新创建：{state.get('productId')}")
    if state.get("stage") == "SUBMITTING" and not state.get("productId"):
        raise ValueError("上次提交结果不明，不重复提交")


def require_new_payload(payload):
    schema = json.loads(payload["schema"])
    text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    if payload["productApplyDTO"].get("productId") or '"componentCode":"SkuId"' in text:
        raise ValueError("新增请求不得携带旧商品ID或SKU ID")


def verified_replacements(paths, source_sku):
    replacements = {}
    for path in paths:
        review = mover.read_json(path)
        if review.get("decision") not in {"PASS", "PASSED"} or str(review.get("sourceSku")) != source_sku:
            continue
        for row in review.get("replacements", []):
            if row.get("role") == "SKU":
                continue
            replacement = Path(row["replacementPath"])
            if not replacement.is_absolute():
                replacement = path.parent / replacement
            if replacement.is_file() and file_hash(replacement) == row.get("replacementSha256"):
                replacements[row["sourceSha256"]] = {**row, "replacementPath": str(replacement.resolve())}
    return replacements


def prepare(args):
    store = pipeline.verify_store_identity(mover.read_json(args.store_config), args.profile)
    records = mover.read_json(args.preflight)["records"]
    inventory = {row["productId"]: row for row in mover.read_json(args.inventory)["products"]}
    reference = mover.read_json(args.reference)
    rules = pipeline.effective_rules(mover.read_json(args.rules), mover.read_json(args.store_config))
    rules["pricing"].update(targetArrivalMultiplier="2", belowCompetitor="0")
    results = []
    for record in records:
        if record["action"] != "UNPAIRED_REQUIRES_BASELINE":
            continue
        source_id = record["sourceSku"]
        folder = args.output / "products" / source_id
        try:
            if (folder / "state.json").is_file():
                raise ValueError("已有发布状态，禁止重新准备覆盖")
            repaired = Path(record["repairedFolder"])
            replacements = verified_replacements(args.compliance_reviews, source_id)
            source_folder = repaired.parents[1]
            source = parse_export_folder(source_folder)
            require_codes(source["variants"])
            audit = mover.read_json(repaired.parent / "audit.json")
            if (audit.get("schemaVersion") != "pdd-sku-image-merchant-feedback-repair/v2"
                    or audit.get("status") != "passed" or audit.get("sourceUnchanged") is not True
                    or audit.get("failures")):
                raise ValueError("修图审计未通过")
            hashes = {row["name"]: row["sha256"] for row in audit["outputs"]}
            original_rows = inventory[source_id]["rows"]
            if len(original_rows) != len(source["variants"]) or len(hashes) != len(original_rows):
                raise ValueError("表格与修图SKU数量不同")
            files = {}
            repair_rows = []
            for variant, original in zip(source["variants"], original_rows):
                name = variant["sourceImageFile"]
                if name != original["name"]:
                    raise ValueError("修图SKU行序与表格不一致")
                image = repaired / name
                if file_hash(image) != hashes[name]:
                    raise ValueError(f"修图哈希不一致：{name}")
                variant["skuImage"] = str(image.resolve())
                files[str(image.resolve())] = hashes[name]
            for role, key, filenames in [("主图", "mainImages", "sourceMainImageFiles"),
                                         ("详情图", "detailImages", "sourceDetailImageFiles")]:
                source[key] = []
                for name in source[filenames]:
                    original_path = source_folder / role / name
                    digest = file_hash(original_path)
                    replacement = replacements.get(digest)
                    image = Path(replacement["replacementPath"]) if replacement else original_path
                    if replacement:
                        repair_rows.append({"role": role, "sourceFilename": name, **replacement})
                    source[key].append(str(image.resolve()))
                    files[str(original_path.resolve())] = digest
                    files[str(image.resolve())] = file_hash(image)
            listing = mover.build_listing(source, folder, rules, reference, 10000)
            style_hashes = {}
            for sku, variant in zip(listing["skus"], source["variants"]):
                digest = files[variant["skuImage"]]
                if style_hashes.setdefault(sku["styleAlias"], digest) != digest:
                    raise ValueError("同款式修正版图片内容不同，不能合并")
            harmonize_sku_main_images(listing)
            listing["product"]["itemNumber"] = f"ABFIX-{source_id}"
            prices = set_exact_prices(listing, source)
            for sku, variant in zip(listing["skus"], source["variants"]):
                values = list(variant["sourceAttributes"].values())
                if listing["product"]["includeSizeSaleAttribute"]:
                    sku["sizeAlias"] = mover.safe_sale_alias(values[1])
                    sku["sizeMappingId"] = mover.stable_mapping_id(listing["product"]["productKey"], "size", sku["sizeAlias"])
            for key in {sku["sizeAlias"] for sku in listing["skus"]}:
                order = sorted({sku["sizeAlias"] for sku in listing["skus"]}).index(key) + 1
                for sku in listing["skus"]:
                    if sku["sizeAlias"] == key:
                        sku["sizeOrder"] = order
            mover.write_json(folder / "manifest.json", source)
            mover.write_json(folder / "listing.json", listing)
            errors, warnings = jingmai.validate_manifest(folder / "listing.json", listing, reference)
            if errors:
                raise ValueError("本地预检：" + "; ".join(errors))
            workbook = source_folder / source["sourceWorkbook"]
            files[str(workbook.resolve())] = file_hash(workbook)
            plan = {"sourceSku": source_id, "title": listing["product"]["title"], "store": store,
                    "itemNumber": listing["product"]["itemNumber"], "skuCount": len(listing["skus"]),
                    "listingSha256": file_hash(folder / "listing.json"), "files": files,
                    "pricing": prices, "reusedComplianceRepairs": repair_rows, "warnings": warnings,
                    "pricingContract": "PDD E column x2; direct 15%, subsidy 15%; half-up components; coupon 0; gap 0",
                    "rollback": "Do not delete originals or recreate accepted/rejected applications; retain IDs",
                    "createdAt": mover.now_iso()}
            mover.write_json(folder / "plan.json", plan)
            results.append({"sourceSku": source_id, "ok": True, "skuCount": plan["skuCount"]})
        except Exception as error:
            failure = {"sourceSku": source_id, "ok": False, "phase": "PREPARE", "error": str(error)}
            mover.write_json(folder / "failure.json", failure)
            results.append(failure)
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    mover.write_json(args.output / "prepared.json", {"store": store, "results": results})


def seal(args):
    reviews = mover.read_json(args.output / "content-review.json")
    for row in mover.read_json(args.output / "prepared.json")["results"]:
        if not row["ok"]:
            continue
        folder = args.output / "products" / row["sourceSku"]
        plan = mover.read_json(folder / "plan.json")
        review = reviews.get(row["sourceSku"])
        if not review:
            raise ValueError(f"缺少内容检查：{row['sourceSku']}")
        plan["contentReview"] = review
        mover.write_json(folder / "plan.json", plan)
        mover.write_json(folder / "dry-run.json", {"ok": review["decision"] == "PASS", "confirmToken": digest_json(plan)})


def verify(folder, product_id):
    listing_path = folder / "listing.uploaded.json"
    for attempt in range(24):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                status = jingmai.command_verify_product(SimpleNamespace(input=listing_path, product_id=product_id,
                                                                        output=folder / "verification.json"))
            if status == 0:
                break
        except jingmai.JdoError as error:
            if "未查询到商品信息" not in str(error):
                raise
        rejection = pipeline.product_apply_rejection(product_id)
        if rejection:
            pipeline.save_state(folder / "state.json", "PRODUCT_APPLY_REJECTED", productId=product_id, productApply=rejection)
            raise ValueError(f"京东申请驳回：{rejection}")
        time.sleep(5)
    else:
        raise batch.VerificationPending(f"商品{product_id}待回查完成，不重复创建")
    detail = jingmai.get("jdo-product-status", "/rest/sp-product/v0/products/{productId}",
                         {"productId": product_id, "scene": "pop"}).get("data") or {}
    plan = mover.read_json(folder / "plan.json")
    if detail["productInfo"].get("itemNum") != plan["itemNumber"]:
        raise ValueError("线上技术货号不同")
    online = {str(sku["outerId"]): sku for sku in detail["skuList"]}
    if set(online) != {row["merchantSku"] for row in plan["pricing"]}:
        raise ValueError("线上商家SKU与表格不一致")
    for row in plan["pricing"]:
        actual = Decimal(str(online[row["merchantSku"]]["priceInfo"]["jdPrice"]))
        cents = int(actual * 100)
        arrival = cents - (cents * 15 + 50) // 100 - (cents * 85 * 15 + 5000) // 10000
        if actual != Decimal(row["jdPrice"]) or arrival != int(Decimal(row["pddPrice"]) * 200):
            raise ValueError("线上逐项取整到手价不等于PDD的2倍")
    listing = mover.read_json(listing_path)
    skus = {sku["skuKey"]: sku for sku in listing["skus"]}
    colors = jingmai.online_style_color_ids(detail)
    groups = {group["uuid"]: group["imageInfoList"] for group in detail["material"]["mainImages"]}
    for image in listing["images"]:
        if image["role"] != "sku_main":
            continue
        alias = jingmai.normalized_sale_alias(skus[image["skuKey"]]["styleAlias"])
        first = next(row for row in groups[colors[alias]] if int(row["orderSort"]) == 1)
        if first["imgUrl"] != image["upload"]["imgUrl"]:
            raise ValueError("线上款式首图不是修正版")
    mover.write_json(folder / "detail.json", detail)
    result = {"ok": True, "sourceSku": plan["sourceSku"], "productId": product_id, "skuCount": len(online),
              "url": f"https://item.jd.com/{detail['skuList'][0]['skuId']}.html", "verifiedAt": mover.now_iso()}
    mover.write_json(folder / "final-verification.json", result)
    pipeline.save_state(folder / "state.json", "VERIFIED", productId=product_id)
    return result


def publish_one(folder, args, *, defer_verification=False):
    plan = mover.read_json(folder / "plan.json")
    require_confirmation(args.confirm, mover.read_json(folder / "dry-run.json")["confirmToken"], plan)
    if plan["contentReview"]["decision"] != "PASS":
        raise ValueError("内容预检跳过：" + plan["contentReview"]["reason"])
    if file_hash(folder / "listing.json") != plan["listingSha256"]:
        raise ValueError("草稿在确认后变化")
    for path, digest in plan["files"].items():
        if file_hash(path) != digest:
            raise ValueError(f"源文件在确认后变化：{path}")
    store = pipeline.verify_store_identity(mover.read_json(args.store_config), args.profile)
    if store != plan["store"]:
        raise batch.BatchStopError("确认店铺不一致")
    state_path = folder / "state.json"
    state = mover.read_json(state_path) if state_path.is_file() else {}
    if defer_verification and state.get("stage") != "PRODUCT_APPLY_REJECTED" and (state.get("productId") or state.get("applyId") or state.get("stage") == "SUBMITTING"):
        return batch.submitted_result(state)
    require_resumable_state(state)
    product_id = int(state.get("productId") or 0)
    if not product_id:
        listing = mover.read_json(folder / "listing.json")
        cache = args.output / "media-cache.json"
        store_key = f"{store['profile']}:{store['venderId']}"
        uploaded = folder / "listing.uploaded.json"
        if not uploaded.is_file():
            reuse_shared_uploaded_images(folder, listing, cache, store_key)
            upload_input = folder / "listing.upload-input.json"
            mover.write_json(upload_input, listing)
            with contextlib.redirect_stdout(io.StringIO()):
                status = jingmai.command_upload(SimpleNamespace(input=upload_input, reference=args.reference,
                                                                output=uploaded, confirm="UPLOAD_TO_JD"))
            if status:
                raise ValueError("图片上传失败")
        update_shared_uploaded_images(folder, mover.read_json(uploaded), cache, store_key)
        jingmai.write_publish_payloads(uploaded, folder / "publish")
        body = folder / "publish/publish-body.json"
        require_new_payload(mover.read_json(body))
        response = jingmai.run_jdo_large(url="/rest/sp-product/v0/product-schemas/validations", method="POST",
                                        body_path=folder / "publish/validation-body.json", skill_id="jdo-product-publish", yes=True)
        mover.write_json(folder / "publish/validation-response.json", response)
        if any(int(row.get("detectResult") or 0) == 1 for row in pipeline.validation_violations(response)):
            raise ValueError("京东合规预检未通过")
        pipeline.save_state(state_path, "SUBMITTING", confirmToken=digest_json(plan), payloadSha256=file_hash(body))
        response = jingmai.run_jdo_large(url="/rest/sp-product/v0/product-applies", method="POST", body_path=body,
                                        skill_id="jdo-product-publish", yes=True)
        product_id = batch.record_response(folder, response)
        print(json.dumps({"stage": "PUBLISHED", "sourceSku": plan["sourceSku"], "productId": product_id}), flush=True)
    if defer_verification:
        return batch.submitted_result(batch.read_state(folder))
    return verify(folder, product_id)


def finalize_progress(progress):
    batch.finalize_progress(progress)


def publish(args, *, audit_only=False):
    if not audit_only and args.confirm != "PUBLISH_TO_JD":
        raise ValueError("缺少明确发布确认")
    prepared = mover.read_json(args.output / "prepared.json")
    rows = sorted(prepared["results"], key=lambda row: (not row["ok"], row.get("skuCount", 0), row["sourceSku"]))
    return batch.run_batch(args, rows, "products", ".publish.lock",
                           lambda folder: publish_one(folder, args, defer_verification=True),
                           verify, audit_only=audit_only)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "seal", "publish", "verify"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--store-config", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--rules", type=Path, default=ROOT / "config/modern-decorative-painting.rules.json")
    parser.add_argument("--compliance-reviews", type=Path, nargs="*", default=[])
    parser.add_argument("--confirm", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.command in {"publish", "verify"}:
        publish(args, audit_only=args.command == "verify")
    else:
        {"prepare": prepare, "seal": seal}[args.command](args)


if __name__ == "__main__":
    main()
