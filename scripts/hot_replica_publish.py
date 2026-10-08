# -*- coding: utf-8 -*-
"""Hot-replica full publish chain for one PROCEED product (formal, parameterized).

Chain: PUBLISH_TO_JD confirmation -> store identity (name+venderId+profile,
never default) -> vendor style start -> local validation -> upload -> JD
validation -> publish -> verify -> upload style rep images -> full edit
(rep@gallery slot 1 + annotated SkuGgtImage per SKU) -> apply-detail recheck
-> main video automation.

Spec-image (ggt) verification reads the SUBMITTED EDIT BODY, not the
apply-detail response: the platform's product-apply detail skuList carries no
spec-image data at all (material is always null), so counting material.ggtImages
there always reports 0. The edit body is the binding source of truth (one
SkuGgtImage per SKU, imgRefId=platform skuId) and its imgRefIds are matched
against the online skuId set from the rechecked apply-detail.
Formalized from the battle-tested task-local 24_publish.py; store config,
reference and preflight decisions are passed as arguments.
"""
import argparse, json, pathlib, sys, time
from types import SimpleNamespace
sys.stdout.reconfigure(encoding="utf-8")
SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import jd_product_pipeline as pipeline  # noqa: E402
import jd_product_mover as mover  # noqa: E402
import jdo_listing_mvp as cli  # noqa: E402


def prop(ps, k):
    return next((p.get("value") for p in ps or [] if p.get("key") == k), None)


def ggt_binding_report(edit_body: dict, apply_data: dict) -> dict:
    """Verify SkuGgtImage bindings from the submitted edit body.

    apply-detail's skuList has NO spec-image fields (material is always null),
    so the edit body is the source of truth. Each binding must carry an imgUrl
    and its imgRefId must match an online skuId from the rechecked apply data.
    """
    schema = json.loads(str(edit_body.get("schema") or "[]"))
    online = {str(sk.get("skuId") or "") for sk in apply_data.get("skuList") or []}
    online.discard("")
    skus_comp = next((c for c in schema if c.get("componentCode") == "SKUS"), {}).get("value") or []
    total = bound = 0
    missing_url, unknown_ref = [], []
    for sku_comp in skus_comp:
        for c in sku_comp.get("value") or []:
            if c.get("componentCode") != "SkuGgtImage":
                continue
            for img in c.get("value") or []:
                total += 1
                ref = str(prop(img.get("properties"), "imgRefId") or "")
                url = str(prop(img.get("properties"), "imgUrl") or "")
                if not url:
                    missing_url.append(ref)
                    continue
                if online and ref not in online:
                    unknown_ref.append(ref)
                    continue
                bound += 1
    return {"expected": total, "bound": bound, "missingUrl": missing_url,
            "unknownRef": unknown_ref, "onlineSkuCount": len(online),
            "source": "edit-body"}


def run_jdo_validation_with_retry(body_path, attempts=4, wait_seconds=40):
    """JD schema validation with retry for the known transient overload.

    99905030004 服务暂时不可用 is momentary server overload, not a draft
    violation (verified in the 2026-09-28..30 zeya batches): wait 30-45s and
    retry; never treat it as a product problem or resubmit anything.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return cli.run_jdo_large(url="/rest/sp-product/v0/product-schemas/validations",
                                    method="POST", body_path=body_path,
                                    skill_id="jdo-product-publish", yes=True)
        except cli.JdoError as exc:
            if "99905030004" not in str(exc) or attempt == attempts:
                raise
            last_error = exc
            print(f"JD validation transient overload (attempt {attempt}/{attempts}), "
                  f"retry in {wait_seconds}s: {str(exc)[-60:]}")
            time.sleep(wait_seconds)
    raise last_error


def build_edit_body(folder, listing, uploaded, publish_dir, plan, reps, apply_data, product_id):
    body = json.loads((publish_dir / "publish-body.json").read_text(encoding="utf-8"))
    apply_dto = body.get("productApplyDTO") or {}
    apply_dto["productId"] = product_id
    apply_dto["saveActionType"] = 0
    schema = json.loads(str(body.get("schema") or "[]"))
    skus_comp = next(c for c in schema if c["componentCode"] == "SKUS")["value"]
    if len(skus_comp) != len(listing["skus"]):
        raise RuntimeError(f"编辑体SKU数量不一致：{len(skus_comp)} vs {len(listing['skus'])}")

    sku_info = {}
    for sk in apply_data.get("skuList") or []:
        attrs = sk.get("saleAttrs") or []
        main = next((a.get("attrValueAlias") for a in attrs if a.get("attrName") in ("颜色", "款式", "规格")), None)
        if main is None:
            continue
        # Multi-dimension products (e.g. 颜色×尺码) repeat styleAlias across rows,
        # so the online SKU must be keyed by the full alias combination. The
        # "单规格" placeholder carries no dimension and is dropped on both sides.
        others = tuple(str(a.get("attrValueAlias")) for a in attrs
                       if a.get("attrName") not in ("颜色", "款式", "规格")
                       and str(a.get("attrValueAlias")) != "单规格")
        sku_info[(str(main), others)] = (str(sk.get("uuid")), str(next(
            (a.get("mappingId") for a in attrs if a.get("attrName") in ("颜色", "款式", "规格")), "")))
    ggt_by_key = {i["imageKey"]: (i.get("upload") or {}).get("imgUrl")
                  for i in uploaded.get("images") or [] if i.get("role") == "sku_main"}
    sku_main_path_by_key = {i["imageKey"]: i["localPath"]
                            for i in listing.get("images") or [] if i.get("role") == "sku_main"}
    rep_by_path = {item["source"]: (reps.get(item["imageKey"]) or {}).get("imgUrl")
                   for item in plan.get("deannotate") or []}

    wsi = next(c for c in schema if c["componentCode"] == "WareSkuImg")
    pool = [img for img in wsi["value"] if str(prop(img.get("properties"), "colorId")) == "0000000000"]
    pool.sort(key=lambda img: int(prop(img.get("properties"), "orderSort") or 0))
    expected_pool = len([i for i in listing.get("images") or [] if i.get("role") == "spu_main"]) or 9
    if len(pool) != expected_pool:
        raise RuntimeError(f"公共主图池必须是{expected_pool}张，当前{len(pool)}")
    shared = pool[1:]
    entries = list(pool)
    seen_groups = set()

    for idx, (sku_comp, row) in enumerate(zip(skus_comp, listing["skus"])):
        image_key = "SKU-MAIN-%03d" % (idx + 1)
        vcv = next(c for c in sku_comp["value"] if c["componentCode"] == "VenderCategoryValue")
        mid = str(prop(vcv["value"][0]["properties"], "mappingId"))
        row_sz = str(row.get("sizeAlias") or "")
        others = (row_sz,) if row_sz and row_sz != "单规格" else ()
        key = (str(row["styleAlias"]), others)
        if key not in sku_info:
            raise RuntimeError(f"在线SKU匹配失败：style={row['styleAlias']} others={others}")
        sku_id, info_mid = sku_info[key]
        if mid != info_mid:
            raise RuntimeError(f"mappingId不一致：{key} body={mid} online={info_mid}")
        rep_url = rep_by_path.get(sku_main_path_by_key.get(image_key))
        if not rep_url:
            raise RuntimeError(f"缺少代表图：{image_key} {sku_main_path_by_key.get(image_key)}")
        ggt_url = ggt_by_key.get(image_key)
        if not ggt_url:
            raise RuntimeError(f"缺少规格图上传回执：{image_key}")

        smi = next(c for c in sku_comp["value"] if c["componentCode"] == "SkuMainImage")
        swapped = fixed = 0
        for img in smi["value"]:
            if str(prop(img.get("properties"), "orderSort")) == "1":
                for p in img["properties"]:
                    if p["key"] == "imgUrl":
                        p["value"] = rep_url
                    if p["key"] in ("colorId", "imgRefId"):
                        p["value"] = mid
                swapped += 1
            else:
                for p in img["properties"]:
                    if p["key"] in ("colorId", "imgRefId"):
                        p["value"] = "0000000000"
                fixed += 1
        if swapped != 1:
            raise RuntimeError(f"{image_key} 首图槽位异常：swapped={swapped}")
        sku_comp["value"] = [c for c in sku_comp["value"] if c["componentCode"] != "SkuStockNum"]
        sku_comp["value"].insert(0, {"componentCode": "SkuId", "value": sku_id})
        sku_comp["value"].append({"componentCode": "SkuGgtImage", "value": [
            {"componentCode": "img", "properties": [
                {"key": "imgUrl", "value": ggt_url},
                {"key": "imgRefId", "value": sku_id}]}]})
        if mid not in seen_groups:
            seen_groups.add(mid)
            for u, order in ([(rep_url, 1)] + [
                    (str(prop(img.get("properties"), "imgUrl")), i + 2) for i, img in enumerate(shared)]):
                entries.append({"componentCode": "img", "inputType": "multiText", "properties": [
                    {"key": "colorId", "value": mid}, {"key": "imgUrl", "value": u},
                    {"key": "imgRefId", "value": mid}, {"key": "orderSort", "value": str(order)}]})
    wsi["value"] = entries
    body["schema"] = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    out = folder / ".pipeline" / "edit-body.json"
    out.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return out, len(seen_groups)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sku", required=True)
    ap.add_argument("--store-config", required=True,
                    help="store config JSON (name + venderId + profile; identity is re-verified)")
    ap.add_argument("--reference", required=True, help="target category reference JSON")
    ap.add_argument("--decisions", required=True,
                    help="preflight decisions JSON; must contain a PROCEED entry for this sku")
    ap.add_argument("--draft-root", default=str(SKILL / "outputs" / "migrations"))
    ap.add_argument("--work-dir", default=None,
                    help="dir with remake-plan/rep-uploads/apply artifacts (default <draft>/hot-replica)")
    ap.add_argument("--skip-video", action="store_true")
    ap.add_argument("--confirm", required=True, help="must be PUBLISH_TO_JD")
    a = ap.parse_args()
    if a.confirm != "PUBLISH_TO_JD":
        print("FAIL: publish requires explicit confirmation string PUBLISH_TO_JD")
        return 2

    sku = a.sku
    decision = next((d for d in json.loads(pathlib.Path(a.decisions).read_text(encoding="utf-8"))
                     if d.get("decision") == "PROCEED" and str(d.get("sourceSku")) == sku), None)
    if not decision:
        print(f"FAIL: no PROCEED decision for {sku} in {a.decisions}")
        return 2
    cid = int((decision.get("targetCategory") or {}).get("id"))
    root = pathlib.Path(a.draft_root)
    folder = next(root.glob(sku + "_*"), None)
    if not folder:
        print(f"FAIL: draft folder not found for {sku} under {root}")
        return 1
    work = pathlib.Path(a.work_dir) if a.work_dir else folder / "hot-replica"
    work.mkdir(parents=True, exist_ok=True)
    listing_path = folder / "listing.json"
    state_path = root / "state" / f"{sku}.json"
    if state_path.exists():
        existing_state = mover.read_json(state_path)
        if existing_state.get("productId"):
            raise RuntimeError(
                f"{sku} 已有 productId={existing_state['productId']}，禁止重复发布；仅可回查或整版编辑")
    runtime = folder / ".pipeline"
    publish_dir = runtime / "publish"
    publish_dir.mkdir(parents=True, exist_ok=True)

    # Hard identity check before any write operation (never default profile).
    store_cfg = mover.read_json(pathlib.Path(a.store_config))
    profile = str(store_cfg["store"]["profile"])
    if profile == "default":
        raise RuntimeError("禁止使用 default Profile")
    identity = pipeline.verify_store_identity(store_cfg, profile)
    print("STORE_VERIFIED", identity["shopName"], identity["venderId"])
    reference = mover.read_json(pathlib.Path(a.reference))

    listing = mover.read_json(listing_path)
    prefix = str(listing["product"]["vendorSkuPrefix"])
    style_start, used = pipeline.next_style_start(prefix, cid)
    listing["product"]["vendorStyleStart"] = int(style_start)
    mover.write_json(listing_path, listing)
    print("vendorStyleStart", style_start, "used", len(used))

    errors, warnings = cli.validate_manifest(listing_path, listing, reference)
    if errors:
        pipeline.save_state(state_path, "FAILED_LOCAL_VALIDATION", errors=errors, warnings=warnings)
        raise RuntimeError("本地预检失败：" + "; ".join(errors))
    print("LOCAL_VALIDATED warnings:", len(warnings))

    uploaded = runtime / "listing.uploaded.json"
    if uploaded.exists():
        previous_uploaded = mover.read_json(uploaded)
        previous_keys = sorted(str(i.get("imageKey")) for i in previous_uploaded.get("images") or [])
        current_keys = sorted(str(i.get("imageKey")) for i in listing.get("images") or [])
        if previous_keys != current_keys:
            uploaded.unlink()
            print("removed stale upload checkpoint (image list changed)")
    if cli.command_upload(SimpleNamespace(input=listing_path, reference=pathlib.Path(a.reference),
                                          output=uploaded, confirm="UPLOAD_TO_JD")):
        raise RuntimeError("图片上传失败")
    print("MEDIA_UPLOADED", uploaded.name)

    cli.write_publish_payloads(uploaded, publish_dir)
    validation = run_jdo_validation_with_retry(publish_dir / "validation-body.json")
    mover.write_json(publish_dir / "validation-response.json", validation)
    violations = pipeline.validation_violations(validation)
    strong = [v for v in violations if int(v.get("detectResult") or 0) == 1]
    if strong:
        pipeline.save_state(state_path, "FAILED_JD_VALIDATION", violations=violations)
        raise RuntimeError(f"京东合规检测存在{len(strong)}项强违规")
    print("JD_VALIDATED violations:", len(violations))

    publish = cli.run_jdo_large(url="/rest/sp-product/v0/product-applies", method="POST",
                                body_path=publish_dir / "publish-body.json",
                                skill_id="jdo-product-publish", yes=True)
    mover.write_json(publish_dir / "publish-response.json", publish)
    product_id = int((publish.get("data") or {}).get("productId") or 0)
    if not product_id:
        raise RuntimeError("发品返回缺少productId")
    print("PUBLISHED productId", product_id)
    pipeline.save_state(state_path, "PUBLISHED", productId=product_id, sourceSku=sku,
                        folder=str(folder), listing=str(listing_path))

    verification = publish_dir / "verification.json"
    for attempt in range(1, 13):
        try:
            rc = cli.command_verify_product(SimpleNamespace(input=uploaded, product_id=product_id,
                                                            output=verification))
            if rc == 0:
                break
        except cli.JdoError as exc:
            if "未查询到商品信息" not in str(exc):
                raise
            rejection = pipeline.product_apply_rejection(product_id)
            if rejection:
                pipeline.save_state(state_path, "PRODUCT_APPLY_REJECTED", productId=product_id,
                                    productApply=rejection)
                raise RuntimeError(f"商品{product_id}申请被驳回：{rejection}")
            if attempt == 12:
                pipeline.save_state(state_path, "PRODUCT_PROPAGATION_PENDING", productId=product_id)
                raise RuntimeError("商品详情仍在传播，稍后仅回查")
        time.sleep(10)
    print("PRODUCT_VERIFIED")
    state = pipeline.save_state(state_path, "PRODUCT_VERIFIED", productId=product_id,
                                verification=str(verification))

    plan = json.loads((work / f"remake-plan-{sku}.json").read_text(encoding="utf-8"))
    zone, cat = reference["media"]["zoneId"], int(reference["media"]["defaultCategoryId"])
    reps_path = work / f"rep-uploads-{sku}.json"
    reps = json.loads(reps_path.read_text(encoding="utf-8")) if reps_path.exists() else {}
    for item in plan.get("deannotate") or []:
        if not (reps.get(item["imageKey"]) or {}).get("imgUrl"):
            rep_file = folder / item["output"]
            if rep_file.stat().st_size > 3 * 1024 * 1024:
                raise RuntimeError(f"代表图超过3MB：{item['imageKey']}")
            reps[item["imageKey"]] = cli.upload(zone, cat, rep_file)
            reps_path.write_text(json.dumps(reps, ensure_ascii=False, indent=2), encoding="utf-8")
        print("REP", item["imageKey"], "->", reps[item["imageKey"]]["imgUrl"][-20:])

    uploaded_json = mover.read_json(uploaded)
    apply_resp = cli.get("jdo-product-update", "/rest/sp-product/v0/product-apply/{applyId}",
                         {"applyId": str(product_id), "scene": "pop"})
    apply_data = apply_resp.get("data") or {}
    (work / f"apply-full-{sku}.json").write_text(
        json.dumps(apply_data, ensure_ascii=False, indent=2), encoding="utf-8")
    edit_body, groups = build_edit_body(folder, listing, uploaded_json, publish_dir,
                                        plan, reps, apply_data, product_id)
    print("EDIT_BODY groups:", groups, "skus:", len(listing["skus"]))
    for attempt in range(1, 6):
        try:
            edit_resp = cli.run_jdo_large(url="/rest/sp-product/v0/product-applies", method="POST",
                                          body_path=edit_body, skill_id="jdo-product-publish", yes=True)
            break
        except cli.JdoError as exc:
            if "审核中" in str(exc) and attempt < 5:
                print("draft lock, retry in 45s:", str(exc)[-80:])
                time.sleep(45)
            else:
                raise
    mover.write_json(publish_dir / "edit-response.json", edit_resp)
    time.sleep(8)
    recheck = cli.get("jdo-product-update", "/rest/sp-product/v0/product-apply/{applyId}",
                      {"applyId": str(product_id), "scene": "pop"}).get("data") or {}
    (work / f"apply-recheck-{sku}.json").write_text(
        json.dumps(recheck, ensure_ascii=False, indent=2), encoding="utf-8")
    # Spec-image verification from the submitted edit body (the apply-detail
    # response has no spec-image data; material is always null there).
    ggt = ggt_binding_report(mover.read_json(edit_body), recheck)
    (work / f"ggt-verify-{sku}.json").write_text(
        json.dumps(ggt, ensure_ascii=False, indent=2), encoding="utf-8")
    print("EDIT_RECHECK ggtImages:", ggt["bound"], "/", ggt["expected"],
          "| source:", ggt["source"], "| online skus:", ggt["onlineSkuCount"])
    if ggt["missingUrl"] or ggt["unknownRef"] or ggt["bound"] != ggt["expected"]:
        raise RuntimeError(f"规格图绑定校验失败：{json.dumps(ggt, ensure_ascii=False)}")

    source = mover.read_json(folder / "manifest.json")
    video = None
    if not a.skip_video:
        video = pipeline.automate_main_video(
            state_path=state_path, state=state, folder=folder, source=source,
            product_id=product_id, transcode_attempts=18, review_attempts=12, interval=10)
    print("VIDEO_AUTOMATION", json.dumps(video, ensure_ascii=False)[:200] if video else None)
    pipeline.save_state(state_path, "VERIFIED", productId=product_id,
                        videoAutomation=video, editGroups=groups,
                        ggtImages=ggt["bound"], ggtSource=ggt["source"],
                        verification=str(verification))
    print(json.dumps({"ok": True, "sku": sku, "productId": product_id,
                      "groups": groups, "ggtImages": ggt["bound"],
                      "ggtSource": ggt["source"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
