# -*- coding: utf-8 -*-
"""Fail-closed, source-paired image selection for no-inventory listings.

CLI consumes a source/selected-image manifest and writes a hash-bound approval
report. It never edits images, uploads, or calls a Jingmai write API. Missing
model evidence cannot be interpreted as CLEAN.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

DELETE_FLAGS = frozenset({
    "PATENT", "TAOJINBI", "PRICE_EXPLANATION", "PLATFORM_DELIVERY",
    "PLATFORM_PROMOTION", "BRAND_INFO_ONLY", "COMPANY_PROFILE",
    "SOURCE_FACTORY", "DENSE_VIOLATIONS", "LOW_VALUE",
})
EDIT_FLAGS = frozenset({"SMALL_VIOLATION", "BRAND", "PLATFORM", "PROMO", "CLAIM", "PERSON"})
PAIR_KEYS = ("productSame", "violationsCleared", "noNewArtifacts", "qualityOk",
             "layoutPreserved", "safeTextPreserved", "textLegible", "noHoles")


def file_evidence(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _flags(parsed):
    if not isinstance(parsed, dict):
        return set()
    flags = parsed.get("flags") or []
    return {str(flag).upper() for flag in flags} if isinstance(flags, list) else set()


def decide(role, primary, review=None):
    """Never let a clean flash verdict erase a severe flag from the lite pass."""
    if not isinstance(primary, dict) or (review is not None and not isinstance(review, dict)):
        return "BLOCK"
    flags = _flags(primary) | _flags(review)
    if flags & DELETE_FLAGS:
        return "BLOCK" if role == "main1" else "DELETE"
    for parsed in (primary, review):
        if parsed is None:
            continue
        if not isinstance(parsed.get("flags"), list) or not all(isinstance(f, str) for f in parsed["flags"]):
            return "BLOCK"
        if parsed.get("uncertain") or parsed.get("legible") is not True or parsed.get("layoutOk") is not True:
            return "BLOCK"
        if parsed.get("valuable") is not True or not isinstance(parsed.get("violations"), list):
            return "BLOCK"
    if flags & EDIT_FLAGS or primary["violations"] or (review and review["violations"]):
        return "EDIT"
    if flags:
        return "BLOCK"  # unknown labels need a human classification
    return "KEEP"


def pair_accepted(role, verdict):
    if not isinstance(verdict, dict) or not all(verdict.get(k) is True for k in PAIR_KEYS):
        return False
    return role != "main1" or verdict.get("sourceFirstMatched") is True


def _dhash(path):
    from PIL import Image
    with Image.open(path) as im:
        gray = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(gray.get_flattened_data() if hasattr(gray, "get_flattened_data") else gray.getdata())
    bits = sum((pixels[y*9+x] > pixels[y*9+x+1]) << (y*8+x)
               for y in range(8) for x in range(8))
    return bits


def duplicate_details(paths, threshold=4):
    """Conservative dHash shortlist, including exact duplicates across encodings.

    A visual model/human should review ambiguous near matches; this function
    returns blockers, never silently removes SKU/detail images.
    """
    seen = []
    duplicates = []
    for path in paths:
        digest = _dhash(path)
        match = next((prior for prior, value in seen if (digest ^ value).bit_count() <= threshold), None)
        if match:
            duplicates.append({"path": str(path), "nearDuplicateOf": match})
        else:
            seen.append((str(path), digest))
    return duplicates


def validate_candidate_report(candidate, report, source_first):
    """Check report coverage and actual bytes before any upload/validation/publish."""
    errors = []
    item = str(candidate.get("sourceItemId") or "")
    entry = (report.get("items") or {}).get(item) if isinstance(report, dict) else None
    if not isinstance(entry, dict):
        return ["image-quality-report-missing:" + item]
    if entry.get("sourceFirst") != source_first:
        errors.append("source-first-mismatch")
    rows = entry.get("images") or []
    selected = [("main1" if i == 0 else "main", p)
                for i, p in enumerate(candidate.get("mainImages") or [])]
    selected += [("detail", p) for p in candidate.get("detailImages") or []]
    selected += [("sku", p) for p in {str(s.get("mainImage") or "") for s in candidate.get("skus") or []}
                 if p and p not in (candidate.get("mainImages") or [])]
    active = [r for r in rows if r.get("decision") not in ("DELETE",)]
    by_path = {}
    for row in active:
        final = row.get("final") or {}
        key = (row.get("role"), final.get("path"))
        if key in by_path:
            errors.append("image-quality-duplicate-report:" + str(key))
        by_path[key] = row
    expected_paths = {(role, str(Path(path).resolve())) for role, path in selected}
    if set(by_path) != expected_paths:
        errors.append("image-quality-coverage-mismatch")
    expected_order = [(role, str(Path(path).resolve())) for role, path in selected if role != "sku"]
    reviewed_order = [(row.get("role"), (row.get("final") or {}).get("path"))
                      for row in active if row.get("role") != "sku"]
    if reviewed_order != expected_order:
        errors.append("image-quality-order-mismatch")
    for role, path in selected:
        row = by_path.get((role, str(Path(path).resolve())))
        if row is None:
            continue
        if row.get("decision") != "KEEP" or row.get("pairAccepted") is not True:
            errors.append("image-quality-not-approved:" + role)
        try:
            if file_evidence(path) != row["final"] or file_evidence(row["source"]["path"]) != row["source"]:
                errors.append("image-quality-stale:" + str(path))
        except (OSError, KeyError, TypeError):
            errors.append("image-quality-stale:" + str(path))
    if selected and selected[0][0] == "main1":
        first = by_path.get(("main1", str(Path(selected[0][1]).resolve())))
        if first and first.get("source") != source_first:
            errors.append("source-first-not-paired")
    else:
        errors.append("first-main-missing")
    try:
        for pair in duplicate_details([p for role, p in selected if role == "detail"]):
            errors.append("detail-near-duplicate:" + pair["path"])
    except (OSError, ValueError):
        errors.append("detail-unreadable")
    return errors


SINGLE_PROMPT = (
    "审核最终候选商品图。逐字核对文字、字形、商品、画面。专利/专利证书、淘金币、价格或活动说明、"
    "天猫明日达等平台服务和活动图、品牌信息页、公司介绍、源头工厂以及低价值图一律判删除；"
    "违规点过密的非首图判删除。小面积违规标 SMALL_VIOLATION 待局部修复；未经违规的原有文字不应被删除。"
    "检查缺字乱码、排版错误、大面积留白空洞及商品变形。仅输出 JSON："
    "flags 可以从 PATENT、TAOJINBI、PRICE_EXPLANATION、PLATFORM_DELIVERY、PLATFORM_PROMOTION、"
    "BRAND_INFO_ONLY、COMPANY_PROFILE、SOURCE_FACTORY、DENSE_VIOLATIONS、LOW_VALUE、"
    "SMALL_VIOLATION、BRAND、PLATFORM、PROMO、CLAIM、PERSON 中选多个。"
    '{"flags":[], '
    '"violations":[{"term":"违规词","text":"整句"}],'
    '"legible":true,"layoutOk":true,"valuable":true,"uncertain":false}。'
    "flags 数组只填实际命中的代号；看不清就 uncertain=true、legible=false，不得猜测干净。"
)

PAIR_PROMPT = ("第1张是商家来源原图，第2张是拟上架图。仅对违规点允许小面积改动，未违规的文字、字体、字号、"
               "文字位置与排版、商品外形数量及场景必须与第1张一致。人物按移除并自然补全评估。"
               "第1张若是来源首主图，第2张必须仍是对应首主图，不能换成新构图或删光正常文案。"
               "仔细检查乱码错字、排版错误、商品变形、空白空洞、专利或平台促销残留。只输出 JSON："
               '{"productSame":true,"violationsCleared":true,"noNewArtifacts":true,"qualityOk":true,'
               '"layoutPreserved":true,"safeTextPreserved":true,"textLegible":true,"noHoles":true,'
               '"sourceFirstMatched":true,"issues":[]}。无法看清的字段必须为 false。')


def create_manifest_from_listing(folder, source, listing):
    """Build exact source pairs for a new Taobao link with untouched snapshots.

    Edited images with a new localPath need an explicit sourceImageSnapshot
    mapping: positional guessing after deletions is not safe.
    """
    folder = Path(folder).resolve()
    snapshot = source.get("sourceImageSnapshot") or {}
    mains = sorted((row for row in listing.get("images") or [] if row.get("role") == "spu_main"),
                   key=lambda row: int(row.get("order") or 0))
    if not mains or not isinstance(snapshot, dict) or not source.get("mainImages"):
        raise ValueError("image quality gate: source first or snapshot missing")
    first_path = str(source["mainImages"][0])
    first = snapshot.get(first_path)
    if not first or snapshot.get(str(mains[0].get("localPath") or "")) != first:
        raise ValueError("image quality gate: source first image changed or missing")

    images = []
    seen = set()
    for row in sorted(listing.get("images") or [],
                      key=lambda r: ({"spu_main": 0, "detail": 1, "sku_main": 2}.get(r.get("role"), 3),
                                     int(r.get("order") or 0), str(r.get("localPath") or ""))):
        path = str(row.get("localPath") or "")
        if not path:
            raise ValueError("image quality gate: empty candidate image")
        role = {"spu_main": "main1" if row is mains[0] else "main",
                "detail": "detail", "sku_main": "sku"}.get(row.get("role"))
        if role is None:
            continue
        if role == "sku" and path in [str(m.get("localPath")) for m in mains]:
            continue  # SKU fallback to the already-reviewed first SPU image
        if (role, path) in seen:
            continue
        seen.add((role, path))
        original = snapshot.get(path)
        if not original:
            raise ValueError("image quality gate: missing source mapping: " + path)
        for relative in (path, original):
            if not (folder / relative).resolve().is_relative_to(folder):
                raise ValueError("image quality gate: path outside draft")
        images.append({"role": role, "source": original, "final": path})
    return {"items": {str(source["sourceSku"]): {"sourceFirst": first, "images": images}}}


def audit(manifest, root):
    """Lite on all selected images; flash only flagged/unclear; conflicts block."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import oxygen_verify as pair_model
    import vision_audit as single_model
    key = pair_model.load_key()  # DPAPI only; never persist/print the key
    output = {"schema": "image-quality-acceptance/1", "items": {}}
    for item, data in manifest["items"].items():
        source_first = file_evidence(root / data["sourceFirst"])
        rows = []
        for image in data["images"]:
            role = str(image["role"])
            source = file_evidence(root / image["source"])
            final = file_evidence(root / image["final"])
            row = {"role": role, "source": source, "final": final, "decision": "BLOCK", "pairAccepted": False}
            try:
                primary = single_model.call(key, single_model.PRIMARY_MODEL, final["path"])
                p = primary.get("parsed") if primary.get("ok") else None
                flagged = single_model.needs_review(primary)
                flagged = flagged or bool(_flags(p)) or (isinstance(p, dict) and (
                    p.get("legible") is not True or p.get("layoutOk") is not True or p.get("valuable") is not True))
                r = single_model.call(key, single_model.REVIEW_MODEL, final["path"]) if flagged else None
                rp = (r or {}).get("parsed") if r and r.get("ok") else None
                decision = decide(role, p, rp) if (not flagged or rp is not None) else "BLOCK"
                source_flags = set()
                if source["sha256"] != final["sha256"]:
                    source_primary = single_model.call(key, single_model.PRIMARY_MODEL, source["path"])
                    source_parsed = source_primary.get("parsed") if source_primary.get("ok") else None
                    if not isinstance(source_parsed, dict):
                        decision = "BLOCK"
                    else:
                        source_flags |= _flags(source_parsed)
                        if single_model.needs_review(source_primary):
                            source_review = single_model.call(key, single_model.REVIEW_MODEL, source["path"])
                            if source_review.get("ok") and isinstance(source_review.get("parsed"), dict):
                                source_flags |= _flags(source_review["parsed"])
                            elif not (source_flags & DELETE_FLAGS):
                                decision = "BLOCK"
                        if source_flags & DELETE_FLAGS:
                            decision = "BLOCK" if role == "main1" else "DELETE"
                row.update(decision=decision, flags=sorted(_flags(p) | _flags(rp) | source_flags))
                if decision == "KEEP":
                    first = pair_model.call(key, source["path"], final["path"], PAIR_PROMPT,
                                            pair_model.PRIMARY_MODEL)
                    if pair_accepted(role, first):
                        row["pairAccepted"] = True
                    else:
                        second = pair_model.call(key, source["path"], final["path"], PAIR_PROMPT,
                                                 pair_model.REVIEW_MODEL)
                        row["pairAccepted"] = pair_accepted(role, first) and pair_accepted(role, second)
                        row["decision"] = "BLOCK"  # conflicting judgments are not safe to auto-upload
                        row["reviewIssues"] = (second or {}).get("issues", [])[:8]
                elif decision == "EDIT":
                    row["issues"] = ["局部违规需修复并重新验收"]
            except Exception as error:
                row["issues"] = [type(error).__name__]  # no request body or credentials in report
            rows.append(row)
        detail_rows = [r for r in rows if r["role"] == "detail" and r["decision"] == "KEEP"]
        try:
            for duplicate in duplicate_details([r["final"]["path"] for r in detail_rows]):
                for row in detail_rows:
                    if row["final"]["path"] == duplicate["path"]:
                        row["decision"] = "BLOCK"
                        row["pairAccepted"] = False
                        row["issues"] = ["近重复详情图，需人工决定保留页"]
        except (OSError, ValueError):
            for row in detail_rows:
                row["decision"] = "BLOCK"; row["pairAccepted"] = False
        output["items"][str(item)] = {"sourceFirst": source_first, "images": rows}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--manifest", help="JSON: items -> sourceFirst, images[{role,source,final}]")
    group.add_argument("--from-listing", action="store_true", help="new Taobao draft: manifest.json + listing.json + source snapshots")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.from_listing:
        source = json.loads((root / "manifest.json").read_text(encoding="utf-8-sig"))
        listing = json.loads((root / "listing.json").read_text(encoding="utf-8-sig"))
        manifest = create_manifest_from_listing(root, source, listing)
    else:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8-sig"))
    report = audit(manifest, root)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    blocked = sum(r["decision"] != "KEEP" for item in report["items"].values() for r in item["images"])
    print(json.dumps({"items": len(report["items"]), "pendingImages": blocked}, ensure_ascii=False))
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
