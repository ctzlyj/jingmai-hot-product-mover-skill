# -*- coding: utf-8 -*-
"""Bind original source videos to live listings: direct copy, no scanning.

Reuses jd_product_pipeline.automate_main_video (idempotent: skips uploaded
videos and already-bound SKUs) and re-audits every SKU video relation.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import jdo_listing_mvp as cli  # noqa: E402
import jd_product_pipeline as pipeline  # noqa: E402
import batch as ni_batch  # noqa: E402
import listing_batch as A  # noqa: E402


def probe_duration(path: Path) -> int:
    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        return max(1, int(round(frames / fps))) if fps else 0
    finally:
        cap.release()


def main() -> int:
    parser = argparse.ArgumentParser()
    ni_batch.add_root_argument(parser)
    parser.add_argument("--plan", default="reports/video-bind-plan.json")
    args = parser.parse_args()
    A.set_root(args.root)
    root = A.ROOT
    A.identity()
    plan = json.loads((root / args.plan).read_text(encoding="utf-8-sig"))
    targets = [x["item"] for x in plan.get("bindOnsale", [])]
    targets += [x["item"] for x in plan.get("bindOfflineForUserRelist", [])]

    results = []
    outf = root / "reports" / "video-bind-results.jsonl"
    log = io.open(outf, "a", encoding="utf-8")
    for item in targets:
        ledger = A.load(root / "ledger.json")
        product_id = int(ledger["items"][item]["productId"])
        video = root / "images" / "raw" / item / "video.mp4"
        record = {"item": item, "productId": product_id}
        try:
            if not video.is_file():
                raise RuntimeError("video file missing")
            candidate = A.load(root / "candidates" / item / "candidate.json")
            detail = cli.get("jdo-product-update", "/rest/sp-product/v0/products/{productId}",
                             {"productId": str(product_id), "scene": "pop"}).get("data") or {}
            cd = (detail.get("productInfo") or detail).get("categoryDetail") or {}
            third = int(cd.get("thirdCategoryId") or 0) or int(cd.get("lastCategoryId") or 0)
            last = int(cd.get("lastCategoryId") or candidate["categoryId"])
            duration = probe_duration(video)
            if not 5 <= duration <= 90:
                raise RuntimeError(f"duration {duration}s out of 5-90 range")
            state_path = root / "candidates" / item / "state.json"
            state = A.load(state_path)
            source = {
                "title": str(candidate.get("title") or item)[:18],
                "categoryIds": [third, last],
                "video": {"localPath": str(video.relative_to(root)), "durationSeconds": duration},
            }
            automation = pipeline.automate_main_video(
                state_path=state_path, state=state, folder=root, source=source,
                product_id=product_id, transcode_attempts=30, review_attempts=20, interval=10,
            )
            record.update(ok=True, videoId=automation.get("videoId"),
                          status=automation.get("status"), skuCount=automation.get("skuCount"),
                          audit=automation.get("audit"))
            print("OK", item, automation.get("status"), "sku:", automation.get("skuCount"), flush=True)
        except Exception as error:  # noqa: BLE001
            record.update(ok=False, error=str(error)[:300])
            print("FAIL", item, str(error)[:200], flush=True)
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.flush()
        results.append(record)
    log.close()
    ok = sum(1 for r in results if r.get("ok"))
    print("done", len(results), "ok", ok, "fail", len(results) - ok, flush=True)
    return 0 if ok == len(results) and results else 1


if __name__ == "__main__":
    raise SystemExit(main())
