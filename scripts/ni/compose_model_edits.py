# -*- coding: utf-8 -*-
"""Keep original pixels; take model output only where violations were erased.

Guarantees the product itself is not reshaped/re-rendered by the model:
final = original everywhere, except inside the violation mask, where the
model fill (resized to the original) is pasted with a feathered edge.
"""
import hashlib, io, json, re, sys, time
from pathlib import Path
import cv2
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch as ni_batch  # noqa: E402

ROOT = ni_batch.root_from_argv()
REPORTS = ROOT / "reports"
OUT_ROOT = ROOT / "image-edits"
RAW_ROOT = ROOT / "image-edits-model-raw"
LEDGER = REPORTS / "model-image-remediation.json"
REPORT = REPORTS / "model-composite-report.json"
BOX_NDJSON = REPORTS / "ocr-boxes.ndjson"


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def norm(s):
    return re.sub(r"[\s\u3000]+", "", s or "").lower()


def read_img(p):
    return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)


def write_img(img, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    ext = dst.suffix.lower()
    params = [cv2.IMWRITE_JPEG_QUALITY, 95] if ext in (".jpg", ".jpeg") else [cv2.IMWRITE_PNG_COMPRESSION, 6]
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        raise RuntimeError("encode failed %s" % dst)
    buf.tofile(str(dst))


RAW_ALL_NDJSON = REPORTS / "ocr-boxes-raw-all.ndjson"


def load_boxes():
    """Box index keyed by raw-image md5. widest OCR coverage first."""
    idx = {}
    for sp in (RAW_ALL_NDJSON, BOX_NDJSON):
        if not sp.is_file():
            continue
        with io.open(sp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("md5"):
                    idx[d["md5"]] = d
    return idx


def _load_boxes_legacy():
    idx = {}
    with io.open(BOX_NDJSON, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("md5"):
                idx[d["md5"]] = d
    return idx


def flagged_boxes(boxinfo, terms, px=0.10, py=0.60):
    """OCR line boxes (padded) for lines containing any flagged term."""
    out = []
    if not boxinfo:
        return out
    needles = [norm(t) for t in terms if t and norm(t)]
    if not needles:
        return out
    for ln in boxinfo.get("lines", []):
        t = norm(ln.get("text"))
        if not t:
            continue
        if any(n in t for n in needles):
            x, y, w, h = int(ln["x"]), int(ln["y"]), int(ln["w"]), int(ln["h"])
            ex = int(max(8, w * px))
            ey = int(max(6, h * py))
            out.append((x - ex, y - ey, x + w + ex, y + h + ey))
    return out


def box_mask(shape, boxes, grow=0):
    m = np.zeros(shape, np.uint8)
    h, w = shape[:2]
    for x0, y0, x1, y1 in boxes:
        x0 = max(0, int(x0) - grow); y0 = max(0, int(y0) - grow)
        x1 = min(w - 1, int(x1) + grow); y1 = min(h - 1, int(y1) + grow)
        if x1 <= x0 or y1 <= y0:
            continue
        cv2.rectangle(m, (x0, y0), (x1, y1), 255, -1)
    return m


def ink_mask(gray):
    """High-contrast stroke content (text / logos / fine print), local_fix equivalent."""
    mn = min(gray.shape)
    m = np.zeros(gray.shape, np.uint8)
    for kdiv, thr in ((20, 12), (8, 20)):
        k = max(9, (mn // kdiv) | 1)
        bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        d = (cv2.absdiff(gray, bg) > thr).astype(np.uint8) * 255
        m = cv2.max(m, d)
    return cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2)))


def all_boxes(boxinfo, px=0.04, py=0.25):
    """Every OCR text line box (padded) - the product photo itself is never text."""
    out = []
    if not boxinfo:
        return out
    for ln in boxinfo.get("lines", []):
        if not (ln.get("text") or "").strip():
            continue
        x, y, w, h = int(ln["x"]), int(ln["y"]), int(ln["w"]), int(ln["h"])
        ex = int(max(6, w * px)); ey = int(max(4, h * py))
        out.append((x - ex, y - ey, x + w + ex, y + h + ey))
    return out


def scale_boxes(boxes, sx, sy, w, h):
    out = []
    for x0, y0, x1, y1 in boxes:
        out.append((max(0, x0 * sx), max(0, y0 * sy), min(w - 1, x1 * sx), min(h - 1, y1 * sy)))
    return out
DIFF_THR = 30
NEAR_R = 40
MIN_AREA_FRAC = 0.00006
DM_TEXT_THR = 18
GLOBAL_RERENDER_FRAC = 0.15


def process(src, raw, dst, boxinfo, terms, extra_rects=None):
    a = read_img(src)
    b = read_img(raw)
    if a is None or b is None:
        return {"status": "READ_FAIL"}
    Ha, Wa = a.shape[:2]
    Hb, Wb = b.shape[:2]
    down = (Wb < Wa or Hb < Ha)
    a_small = cv2.resize(a, (Wb, Hb), interpolation=cv2.INTER_AREA if down else cv2.INTER_LANCZOS4)
    d = np.abs(a_small.astype(np.int16) - b.astype(np.int16)).max(axis=2).astype(np.uint8)
    dm = (d > DIFF_THR).astype(np.uint8) * 255
    dm = cv2.morphologyEx(dm, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    b_up = cv2.resize(b, (Wa, Ha), interpolation=cv2.INTER_LANCZOS4 if not down else cv2.INTER_AREA)
    frac30 = float((np.abs(a.astype(np.int16) - b_up.astype(np.int16)).max(axis=2) > 30).mean())
    # 20260923: never paste the whole model render. A model that re-renders the
    # entire photo visibly reshapes the product (forbidden). Keep original pixels
    # everywhere except the violation mask and flag the case for review.
    global_rerender = frac30 > GLOBAL_RERENDER_FRAC

    bx_orig = flagged_boxes(boxinfo, terms)
    all_orig = all_boxes(boxinfo)
    sx, sy = Wb / float(Wa), Hb / float(Ha)
    bx_m = scale_boxes(bx_orig, sx, sy, Wb, Hb)
    tb_m = (box_mask((Hb, Wb), scale_boxes(all_orig, sx, sy, Wb, Hb)) if all_orig
            else np.zeros((Hb, Wb), np.uint8))
    bm = box_mask((Hb, Wb), bx_m) if bx_m else np.zeros((Hb, Wb), np.uint8)
    near = (cv2.dilate(bm, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * NEAR_R + 1, 2 * NEAR_R + 1)))
            if bx_m else np.zeros((Hb, Wb), np.uint8))

    ink_a = ink_mask(cv2.cvtColor(a_small, cv2.COLOR_BGR2GRAY))
    ink_b = ink_mask(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(dm, 8)
    accepted = np.zeros((Hb, Wb), np.uint8)
    acc_count = rej_area = acc_area = 0
    erasure_count = 0
    min_area = max(64, int(MIN_AREA_FRAC * Wb * Hb))
    max_comp = 0.25 * Wb * Hb
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        comp = (labels == i)
        pick = ""
        frac = float((comp & (near > 0)).sum()) / area if len(bx_m) else 0.0
        if frac >= 0.5 and area < max_comp:
            pick = "near_violation"
        if not pick and area < max_comp:
            md = cv2.dilate(comp.astype(np.uint8), np.ones((7, 7), np.uint8)).astype(bool)
            ra = float((ink_a[md] > 0).mean())
            rb = float((ink_b[md] > 0).mean())
            if ra > 0.01 and rb < 0.5 * ra:
                pick = "ink_erased"
        if pick:
            accepted = cv2.bitwise_or(accepted, comp.astype(np.uint8) * 255)
            acc_count += 1
            acc_area += area
            if pick == "ink_erased":
                erasure_count += 1
        else:
            rej_area += area

    dm_text = cv2.dilate((d > DM_TEXT_THR).astype(np.uint8) * 255, np.ones((7, 7), np.uint8))
    mask_b = cv2.bitwise_and(tb_m, dm_text)
    localize_failed = (not len(bx_m)) and acc_count == 0 and not mask_b.any() and not extra_rects
    if localize_failed:
        # 20260923: never paste the whole model render (deforms the product).
        # Keep the original untouched and flag it for manual handling instead.
        write_img(a, dst)
        return {"status": "OK", "mode": "original_kept_localize_failed", "globalRerender": bool(global_rerender),
                "globalChangeFrac": round(frac30, 4), "sourceSize": [Wa, Ha], "modelSize": [Wb, Hb],
                "localizeFailed": True, "output": str(dst), "outputSha256": sha256(dst)}
    else:
        m_small = cv2.bitwise_or(cv2.bitwise_or(bm, accepted), mask_b)

    kk = max(9, (min(Hb, Wb) // 60) | 1)
    m_small = cv2.morphologyEx(m_small, cv2.MORPH_CLOSE,
                               cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kk, kk)))
    m_small = cv2.dilate(m_small, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    mask = cv2.resize(m_small, (Wa, Ha), interpolation=cv2.INTER_NEAREST)
    for r in (extra_rects or []):
        x0 = int(max(0, min(Wa - 1, r[0] * Wa))); y0 = int(max(0, min(Ha - 1, r[1] * Ha)))
        x1 = int(max(0, min(Wa - 1, r[2] * Wa))); y1 = int(max(0, min(Ha - 1, r[3] * Ha)))
        if x1 > x0 and y1 > y0:
            cv2.rectangle(mask, (x0, y0), (x1, y1), 255, -1)
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    alpha = cv2.GaussianBlur(mask, (0, 0), 2.0).astype(np.float32) / 255.0
    alpha = np.clip(alpha * 1.6, 0.0, 1.0)[..., None]
    out = (a.astype(np.float32) * (1.0 - alpha) + b_up.astype(np.float32) * alpha).astype(np.uint8)
    write_img(out, dst)
    ar_a = Wa / float(Ha)
    ar_b = Wb / float(Hb)
    return {"status": "OK", "mode": "composite", "globalRerender": bool(global_rerender),
            "globalChangeFrac": round(frac30, 4),
            "sourceSize": [Wa, Ha], "modelSize": [Wb, Hb],
            "aspectDelta": round(abs(ar_a - ar_b) / ar_a, 4),
            "maskRatio": round(float((mask > 0).mean()), 4),
            "boxCount": len(bx_orig), "allTextBoxes": len(all_orig),
            "textChangedMaskRatio": round(float((mask_b > 0).mean()), 4), "maskRatioClosed": round(float((m_small > 0).mean()), 4),
            "acceptedComponents": acc_count, "erasureComponents": erasure_count,
            "acceptedAreaRatio": round(acc_area / float(Wb * Hb), 4),
            "rejectedAreaRatio": round(rej_area / float(Wb * Hb), 4),
            "globalReRenderDiscarded": bool(rej_area / float(Wb * Hb) > 0.05),
            "localizeFailed": localize_failed,
            "output": str(dst), "outputSha256": sha256(dst)}


def load_manual():
    out = set()
    p = REPORTS / "manual-image-edits.json"
    if p.is_file():
        for r in (json.load(io.open(p, encoding="utf-8")).get("items") or []):
            out.add((r.get("item"), Path(r.get("name") or "").stem))
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-file", default="", help="lines of item/stem to restrict processing")
    args = ap.parse_args()
    only = None
    if args.only_file:
        only = set()
        for line in io.open(args.only_file, encoding="utf-8"):
            line = line.strip()
            if line:
                only.add(line.replace("\\", "/"))
    manual = load_manual()
    extras = {}
    ep = REPORTS / "mask-extra.json"
    if ep.is_file():
        extras = {k.replace("\\", "/"): v for k, v in
                  (json.load(io.open(ep, encoding="utf-8")).get("rects") or {}).items()}
    ledger = json.load(io.open(LEDGER, encoding="utf-8"))
    boxes_idx = load_boxes()
    if only is not None and REPORT.is_file():
        rep = json.load(io.open(REPORT, encoding="utf-8"))
        rep["generatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        rep["note"] = (rep.get("note") or "") + " | incremental --only-file run"
    else:
        rep = {"schema": "model-composite/1", "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "note": "final = original pixels; model fill used only inside the violation mask",
               "items": {}}
    stats = {"ok": 0, "readFail": 0, "localizeFailed": 0, "globalReRenderDiscarded": 0, "pending": 0, "manualSkipped": 0, "localHandled": 0}
    for key, e in ledger["items"].items():
        if e.get("status") != "SUCCESS":
            stats["pending"] += 1
            continue
        if not e.get("output"):
            # handled by the local pipeline (solid-fill); nothing for the model
            # composite to do. Skipping avoids a KeyError on the missing output.
            stats["localHandled"] = stats.get("localHandled", 0) + 1
            continue
        src = Path(e["source"])
        model_out = Path(e["output"])
        item = src.parent.name
        if only is not None and ("%s/%s" % (item, src.stem)) not in only:
            continue
        if (item, src.stem) in manual:
            stats["manualSkipped"] = stats.get("manualSkipped", 0) + 1
            continue
        raw = RAW_ROOT / item / (src.stem + ".png")
        if not raw.is_file():
            if not model_out.is_file():
                stats["readFail"] += 1
                continue
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(model_out.read_bytes())
        terms = [t for v in (e.get("terms") or {}).values() for t in v]
        boxinfo = boxes_idx.get(e.get("md5"))
        dst = OUT_ROOT / item / (src.stem + ".png")
        frac30flag = False
        try:
            r = process(src, raw, dst, boxinfo, terms,
                        extras.get("%s/%s" % (item, src.stem)))
            frac30flag = bool(r.get("globalRerender"))
            print("  composite", item + "/" + src.name, "rerender=", frac30flag, flush=True)
        except Exception as exc:
            r = {"status": "ERROR", "error": "%s: %s" % (type(exc).__name__, exc)}
        r.update({"globalRerender": bool(frac30flag), "role": e.get("role"), "itemIds": e.get("itemIds"), "md5": e.get("md5"),
                  "terms": e.get("terms"), "rawModelOutput": str(raw),
                  "source": str(src), "sourceSha256": sha256(src)})
        rep["items"][str(src)] = r
        if r.get("status") == "OK":
            stats["ok"] += 1
            if r.get("localizeFailed"):
                stats["localizeFailed"] += 1
            if r.get("globalReRenderDiscarded"):
                stats["globalReRenderDiscarded"] += 1
        else:
            stats["readFail"] += 1
        io.open(REPORT, "w", encoding="utf-8").write(json.dumps(rep, ensure_ascii=False, indent=1))
    io.open(REPORT, "w", encoding="utf-8").write(json.dumps(rep, ensure_ascii=False, indent=1))
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
