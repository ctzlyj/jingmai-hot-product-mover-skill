# -*- coding: utf-8 -*-
"""Publish the ready subset of a no-inventory batch.

Excludes:
  - categories flagged brandRequired in batch-config.json -> SKIPPED_BRAND_REQUIRED
  - items whose compliant images are still pending remediation
"""
from __future__ import annotations

import argparse, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch as ni_batch  # noqa: E402
import listing_batch as A  # noqa: E402

DEFAULT_SKIP_BRAND_REASON = "类目强制品牌且当前店铺类型无无品牌经营权限，舍弃该商品（不编造品牌、不改挂表外类目）"


def main() -> None:
    ap = argparse.ArgumentParser()
    ni_batch.add_root_argument(ap)
    ap.add_argument("--confirm", default="")
    ap.add_argument("--exclude", default="", help="comma separated sourceItemIds to defer")
    ap.add_argument("--skip-brand-category", default="", help="comma list of categoryIds to mark SKIPPED_BRAND_REQUIRED; default: config categories with brandRequired=true")
    ap.add_argument("--phase", choices=["submit", "verify", "both"], default="both")
    args = ap.parse_args()
    config = A.set_root(args.root)
    if args.confirm != "PUBLISH_TO_JD":
        raise RuntimeError("缺少发布确认串 PUBLISH_TO_JD")
    deferred = {x.strip() for x in args.exclude.split(",") if x.strip()}

    skip_brand_categories = {int(x) for x in args.skip_brand_category.split(",") if x.strip()}
    if not skip_brand_categories:
        skip_brand_categories = {
            int(cid) for cid, rule in (config.get("categories") or {}).items()
            if rule.get("brandRequired")
        }
    reasons = {
        int(cid): str((config.get("categories") or {}).get(cid, {}).get("brandRequiredReason")
                      or DEFAULT_SKIP_BRAND_REASON)
        for cid in skip_brand_categories
    }

    preparation = A.load(A.ROOT / "reports" / "candidate-preparation.json")
    rows = [r for r in preparation["results"] if r["status"] == "LOCAL_VALIDATED"]
    A.identity()

    brand_skipped = []
    for row in rows:
        if row["categoryId"] in skip_brand_categories:
            sid = row["sourceItemId"]
            state_path = A.ROOT / "candidates" / sid / "state.json"
            state = A.load(state_path) if state_path.exists() else {"sourceItemId": sid}
            state.update(stage="SKIPPED_BRAND_REQUIRED", reason=reasons[row["categoryId"]],
                         categoryId=row["categoryId"], title=row["title"], updatedAt=A.now())
            A.save(state_path, state)
            brand_skipped.append({"sourceItemId": sid, "categoryId": row["categoryId"],
                                  "title": row["title"], "skuCount": row["skuCount"],
                                  "status": "SKIPPED_BRAND_REQUIRED", "reason": reasons[row["categoryId"]]})
    (A.ROOT / "reports" / "skipped-brand-required.json").write_text(
        json.dumps({"generatedAt": A.now(), "items": brand_skipped}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    targets = [r["sourceItemId"] for r in rows
               if r["categoryId"] not in skip_brand_categories and r["sourceItemId"] not in deferred]
    print(json.dumps({"phase": "plan", "targets": len(targets),
                      "brandSkipped": len(brand_skipped), "deferred": sorted(deferred)},
                     ensure_ascii=False), flush=True)

    submissions = []
    if args.phase in ("submit", "both"):
        for sid in targets:
            try:
                state_path = A.ROOT / "candidates" / sid / "state.json"
                state = A.load(state_path) if state_path.exists() else {}
                if state.get("stage") == "VERIFIED":
                    submissions.append({"sourceItemId": sid, "stage": "VERIFIED", "resumed": True})
                    continue
                if state.get("stage") in A.AWAITING_VERIFICATION:
                    submissions.append({"sourceItemId": sid, "stage": state["stage"], "resumed": True})
                    continue
                if not state.get("stage") or state.get("stage") in {"JD_VALIDATION_BLOCKED", "JD_VALIDATED"}:
                    state = A.validate_one(sid, args.confirm)
                if state.get("stage") == "JD_VALIDATED":
                    state = A.publish_one(sid, args.confirm)
                row = {"sourceItemId": sid, "stage": state.get("stage"),
                       "productId": state.get("productId"), "error": state.get("error")}
                submissions.append(row)
                print(json.dumps({"phase": "submit", **row}, ensure_ascii=False), flush=True)
            except Exception as error:  # noqa: BLE001
                row = {"sourceItemId": sid, "stage": "FAILED_ITEM", "error": str(error)}
                submissions.append(row)
                print(json.dumps({"phase": "submit", **row}, ensure_ascii=False), flush=True)
        A.save(A.ROOT / "reports" / "publish-batch-submissions.json",
               {"updatedAt": A.now(), "targets": targets, "results": submissions})

    verifications = []
    if args.phase in ("verify", "both"):
        for sid in targets:
            try:
                state_path = A.ROOT / "candidates" / sid / "state.json"
                if not state_path.exists():
                    raise RuntimeError("没有可核验的提交状态")
                state = A.load(state_path)
                if state.get("stage") == "VERIFIED":
                    verifications.append({"sourceItemId": sid, "stage": "VERIFIED", "resumed": True})
                    continue
                if state.get("stage") not in A.AWAITING_VERIFICATION:
                    raise RuntimeError(f"当前阶段不可核验：{state.get('stage')}")
                state = A.verify_one(sid)
                row = {"sourceItemId": sid, "stage": state.get("stage"), "productId": state.get("productId"),
                       "ok": (state.get("verification") or {}).get("ok"),
                       "errors": (state.get("verification") or {}).get("errors")}
                verifications.append(row)
                print(json.dumps({"phase": "verify", **row}, ensure_ascii=False), flush=True)
            except Exception as error:  # noqa: BLE001
                row = {"sourceItemId": sid, "stage": "FAILED_ITEM", "error": str(error)}
                verifications.append(row)
                print(json.dumps({"phase": "verify", **row}, ensure_ascii=False), flush=True)
        A.save(A.ROOT / "reports" / "publish-batch-results.json",
               {"updatedAt": A.now(), "submissions": submissions, "verifications": verifications})

    print(json.dumps({
        "targets": len(targets),
        "brandSkipped": len(brand_skipped),
        "submitted": len(submissions),
        "verifiedOk": sum(1 for r in verifications if r.get("stage") == "VERIFIED"),
        "verificationCount": len(verifications),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
