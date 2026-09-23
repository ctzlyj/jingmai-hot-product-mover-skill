# -*- coding: utf-8 -*-
"""Local residual-violation check: OCR remediated images and look for residual terms."""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch as ni_batch  # noqa: E402


def norm(value: str) -> str:
    return re.sub(r"[\s\u3000]+", "", value or "").lower()


def main() -> int:
    parser = argparse.ArgumentParser()
    ni_batch.add_root_argument(parser)
    args = parser.parse_args()
    root = ni_batch.resolve_root(args.root)
    reports = root / "reports"
    remediation = json.loads((reports / "local-image-remediation.json").read_text(encoding="utf-8"))
    routing = {
        row["path"]: row
        for row in json.loads((reports / "image-routing.json").read_text(encoding="utf-8"))
    }
    boxes = {}
    for line in (reports / "ocr-boxes-fixed.ndjson").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            row = json.loads(line)
            boxes[row["path"]] = row
    clean = residual = ocrfail = 0
    bad = []
    for source, item in remediation["items"].items():
        if item.get("status") != "SUCCESS":
            continue
        output = item["output"]
        route = routing.get(source, {})
        terms = [t for values in (route.get("terms") or {}).values() for t in values]
        box = boxes.get(output)
        if not box or not box.get("ok"):
            ocrfail += 1
            continue
        text = "".join(norm(line["text"]) for line in box.get("lines", []))
        hits = [t for t in set(terms) if norm(t) in text]
        if hits:
            residual += 1
            bad.append((item["role"], Path(source).parent.name, Path(source).name, hits,
                        "".join(line["text"] for line in box.get("lines", []))[:120]))
        else:
            clean += 1
    print("clean=%d residual=%d ocr_fail=%d" % (clean, residual, ocrfail))
    for row in bad[:40]:
        print(" RESID", row[0], row[1], row[2], row[3], "|", row[4])
    return 0 if residual == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
