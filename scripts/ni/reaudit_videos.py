# -*- coding: utf-8 -*-
"""Re-audit review_pending video bindings (no re-upload, single query each)."""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import jdo_listing_mvp as cli  # noqa: E402
import jd_product_pipeline as pipeline  # noqa: E402
import batch as ni_batch  # noqa: E402
import listing_batch as A  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    ni_batch.add_root_argument(parser)
    args = parser.parse_args()
    A.set_root(args.root)
    root = A.ROOT
    A.identity()
    rows = [json.loads(line) for line in
            io.open(root / "reports" / "video-bind-results.jsonl", encoding="utf-8-sig") if line.strip()]
    latest = {}
    for row in rows:
        latest[row["item"]] = row  # last wins
    pending = [key for key, value in latest.items()
               if value.get("ok") and value.get("status") == "review_pending"]
    print("pending:", pending)
    out = []
    for item in pending:
        try:
            state = A.load(root / "candidates" / item / "state.json")
            automation = state.get("videoAutomation") or {}
            video_id = int(automation.get("videoId") or 0)
            zone = str(automation.get("zoneId") or pipeline.video_zone_id())
            if not video_id:
                raise RuntimeError("no videoId in state")
            detail = cli.get("jdo-product-status", "/rest/sp-product/v0/products/{productId}",
                             {"productId": str(latest[item]["productId"]), "scene": "pop"}).get("data") or {}
            sku_ids = sorted(int(s["skuId"]) for s in detail.get("skuList") or [])
            relations = pipeline.query_video_relations(zone, video_id)
            audit = pipeline.video_relation_audit(relations, sku_ids, video_id)
            if audit.get("allApproved"):
                automation["status"] = "verified"
                automation["audit"] = audit
                state["videoAutomation"] = automation
                state["stage"] = "VIDEO_VERIFIED"
                (root / "candidates" / item / "state.json").write_text(
                    json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            latest[item].update(status=automation["status"], audit=audit)
            out.append({"item": item, "status": automation["status"],
                        "allApproved": audit.get("allApproved"),
                        "rejectedSkuIds": audit.get("rejectedSkuIds"),
                        "approvedSkuIds": audit.get("approvedSkuIds")})
            print(item, automation["status"], "allApproved:", audit.get("allApproved"),
                  "rejected:", audit.get("rejectedSkuIds"))
        except Exception as error:  # noqa: BLE001
            out.append({"item": item, "error": str(error)[:200]})
            print(item, "ERR", str(error)[:150])
    (root / "reports" / "video-bind-reaudit.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if out and all(row.get("allApproved") for row in out) else 1


if __name__ == "__main__":
    raise SystemExit(main())
