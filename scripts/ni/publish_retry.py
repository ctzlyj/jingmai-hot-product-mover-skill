# -*- coding: utf-8 -*-
"""Retry/re-publish driver for remaining no-inventory batch items.

Safety model:
  * Every write is preceded by an itemNumber existence check (rule: reconcile
    before replay). If the remote already holds the technical item number the
    item is reconciled/verified instead of resubmitted.
  * PRODUCT_APPLY_REJECTED states are archived (confirmed absent remotely) and
    the item is rebuilt with the SAME persisted itemNumber / vendorSku codes.
"""
from __future__ import annotations
import argparse, io, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch as ni_batch  # noqa: E402
import listing_batch as A  # noqa: E402

TRANSIENT = ("99905030004", "临时", "稍后", "超时", "timeout", "Timeout", "502", "503", "504")


def archive_state(path: Path, sid: str) -> None:
    if not path.is_file():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = path.parent / ("state.superseded-%s.json" % stamp)
    dst.write_bytes(path.read_bytes())
    path.unlink()


def main() -> None:
    ap = argparse.ArgumentParser()
    ni_batch.add_root_argument(ap)
    ap.add_argument("--confirm", default="")
    ap.add_argument("--items", required=True)
    ap.add_argument("--phase", choices=["submit", "verify", "both"], default="both")
    ap.add_argument("--attempts", type=int, default=4)
    args = ap.parse_args()
    A.set_root(args.root)
    if args.confirm != "PUBLISH_TO_JD":
        raise RuntimeError("缺少发布确认串 PUBLISH_TO_JD")
    items = [x.strip() for x in args.items.split(",") if x.strip()]

    A.identity()
    out = {"updatedAt": A.now(), "items": items, "results": []}

    def record(row):
        out["results"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        A.save(A.ROOT / "reports" / "publish-retry-results.json", out)

    if args.phase in ("submit", "both"):
        for sid in items:
            sp = A.ROOT / "candidates" / sid / "state.json"
            row = {"sourceItemId": sid, "submitStage": None}
            for attempt in range(1, args.attempts + 1):
                try:
                    state = A.load(sp) if sp.is_file() else {}
                    stage = state.get("stage")
                    if stage == "VERIFIED":
                        row["submitStage"] = "VERIFIED"
                        break
                    if stage in A.AWAITING_VERIFICATION and stage != "PRODUCT_APPLY_REJECTED":
                        row["submitStage"] = stage
                        break
                    if stage == "PRODUCT_APPLY_REJECTED":
                        archive_state(sp, sid)
                        state, stage = {}, None
                    if stage in ("SUBMITTING", "SUBMISSION_RESULT_REQUIRES_RECONCILIATION"):
                        original_item = str(state.get("itemNumber") or "")
                        if not original_item:
                            raise RuntimeError("write result unknown and original itemNumber missing; manual reconciliation required")
                        matches = A.exact_remote_matches({"itemNumber": original_item})
                        if len(matches) == 1:
                            state.update(stage="REMOTE_RECONCILED_AWAITING_VERIFICATION",
                                         productId=int(matches[0]["productId"]),
                                         applyId=str(matches[0]["productId"]), updatedAt=A.now())
                            A.save(sp, state)
                            row["submitStage"] = state["stage"]
                            row["productId"] = state["productId"]
                        else:
                            row["submitStage"] = "SUBMISSION_RESULT_REQUIRES_RECONCILIATION"
                            row["error"] = "write result unknown; no replay until remote state is reconciled"
                        break
                    if not stage or stage in ("JD_VALIDATION_BLOCKED", "JD_VALIDATED", "VALIDATING"):
                        state = A.validate_one(sid, args.confirm)
                        stage = state.get("stage")
                    if stage == "JD_VALIDATION_BLOCKED":
                        row["submitStage"] = "JD_VALIDATION_BLOCKED"
                        row["blockingViolations"] = state.get("blockingViolations")
                        break
                    state = A.publish_one(sid, args.confirm)
                    stage = state.get("stage")
                    row["submitStage"] = stage
                    row["productId"] = state.get("productId")
                    if stage in ("SUBMITTED_AWAITING_VERIFICATION", "REMOTE_EXISTING_REQUIRES_VERIFICATION"):
                        break
                    if stage == "REMOTE_EXISTING_REQUIRES_VERIFICATION":
                        break
                    row["error"] = state.get("error")
                except Exception as error:  # noqa: BLE001
                    row["submitStage"] = "ERROR"
                    row["error"] = str(error)
                if attempt < args.attempts:
                    time.sleep(25)
            record(row)

    if args.phase in ("verify", "both"):
        for sid in items:
            sp = A.ROOT / "candidates" / sid / "state.json"
            if not sp.is_file():
                record({"sourceItemId": sid, "verifyStage": "NO_STATE"})
                continue
            state = A.load(sp)
            if state.get("stage") == "VERIFIED":
                record({"sourceItemId": sid, "verifyStage": "VERIFIED", "resumed": True,
                        "productId": state.get("productId")})
                continue
            if state.get("stage") not in A.AWAITING_VERIFICATION:
                record({"sourceItemId": sid, "verifyStage": "NOT_VERIFIABLE",
                        "stage": state.get("stage")})
                continue
            ok = False
            for attempt in range(1, args.attempts + 1):
                try:
                    state = A.verify_one(sid)
                    stage = state.get("stage")
                    row = {"sourceItemId": sid, "verifyStage": stage,
                           "productId": state.get("productId"),
                           "ok": (state.get("verification") or {}).get("ok"),
                           "errors": (state.get("verification") or {}).get("errors")}
                    if stage in ("VERIFIED", "PRODUCT_APPLY_REJECTED", "PRODUCT_PROPAGATION_PENDING"):
                        record(row)
                        ok = True
                        break
                    record(row)
                    ok = True
                    break
                except Exception as error:  # noqa: BLE001
                    last = str(error)
                    if attempt < args.attempts:
                        time.sleep(25)
            if not ok:
                record({"sourceItemId": sid, "verifyStage": "ERROR", "error": last})

    A.save(A.ROOT / "reports" / "publish-retry-results.json", out)
    print(json.dumps({"items": len(items),
                      "verified": sum(1 for r in out["results"] if r.get("verifyStage") == "VERIFIED")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
