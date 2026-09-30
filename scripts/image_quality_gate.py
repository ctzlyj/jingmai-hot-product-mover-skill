# -*- coding: utf-8 -*-
"""Technical-only image file helpers for listing drafts.

2026-09-30: visual detection (VLM gate, brand scan) was removed from this
Skill at the user's request. This module keeps only byte/hash/coverage
helpers used by publish_local_draft for technical report validation;
it never calls a vision model and never edits or uploads images.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROMPT_VERSION = 4
REMAKE_PROMPT_VERSION = 5
PROMPT_VERSIONS = {"fidelity": PROMPT_VERSION, "remake": REMAKE_PROMPT_VERSION}


def file_evidence(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


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

    A human should review ambiguous near matches; this function returns
    blockers, never silently removes SKU/detail images.
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


def validate_candidate_report(candidate, report, source_first, gate_mode=None):
    """Check report coverage and actual bytes before any upload/validation/publish."""
    errors = []
    expected_mode = gate_mode or (candidate.get("gateMode") if isinstance(candidate, dict) else None) or "fidelity"
    if expected_mode not in PROMPT_VERSIONS:
        return ["image-quality-mode-unknown:" + str(expected_mode)]
    if isinstance(report, dict) and report.get("promptVersion") != PROMPT_VERSIONS[expected_mode]:
        errors.append("image-quality-report-version-mismatch")
    if isinstance(report, dict) and (report.get("gateMode") or "fidelity") != expected_mode:
        errors.append("image-quality-mode-mismatch")
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
    main_hashes = {(by_path.get((role, str(Path(path).resolve()))) or {}).get("final", {}).get("sha256")
                   for role, path in selected if role in ("main1", "main")}
    for role, path in selected:
        if role != "detail":
            continue
        detail_sha = (by_path.get((role, str(Path(path).resolve()))) or {}).get("final", {}).get("sha256")
        if detail_sha and detail_sha in main_hashes:
            errors.append("detail-duplicates-main:" + str(path))
    try:
        for pair in duplicate_details([p for role, p in selected if role == "detail"]):
            errors.append("detail-near-duplicate:" + pair["path"])
    except (OSError, ValueError):
        errors.append("detail-unreadable")
    return errors


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
