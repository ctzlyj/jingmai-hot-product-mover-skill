# -*- coding: utf-8 -*-
"""Assemble final compliant image set: originals for clean images, remediated for violating ones."""
import hashlib, io, json, sys, time
from pathlib import Path
from PIL import Image
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch as ni_batch  # noqa: E402

ROOT = ni_batch.root_from_argv()
RAW = ROOT/"images"/"raw"
CAND = ROOT/"candidates"
EDIT = ROOT/"image-edits"
OUT = ROOT/"images"/"compliant"
REP = ROOT/"reports"

_video_exclude_path = ROOT / "reports" / "video-exclusions.json"
VIDEO_EXCLUDE = set(json.loads(_video_exclude_path.read_text(encoding="utf-8-sig")).get("items") or []) if _video_exclude_path.is_file() else set()

def sha256(p):
    h=hashlib.sha256()
    with open(p,"rb") as f:
        for c in iter(lambda: f.read(1<<20), b""): h.update(c)
    return h.hexdigest()

def md5f(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def build_md5_edit_index():
    """content-hash -> remediated file, so identical images on other items reuse the fix."""
    idx = {}
    for name in ("local-image-remediation.json", "model-image-remediation.json"):
        p = REP / name
        if not p.is_file():
            continue
        doc = json.load(io.open(p, encoding="utf-8"))
        for rec in (doc.get("items") or {}).values():
            m = rec.get("md5")
            out = rec.get("output")
            if not m or not out or rec.get("status") != "SUCCESS":
                continue
            if Path(out).is_file() and m not in idx:
                idx[m] = Path(out)
    return idx


def find_edit(itemId, src: Path, method):
    stem, ext = src.stem, src.suffix.lower()
    local = EDIT/itemId/(stem+ext)
    model = EDIT/itemId/(stem+".png")
    lv = local if local.is_file() else None
    mv = model if model.is_file() else None
    if method == "LOCAL_SOLID_FILL":
        return lv or mv, ("local" if lv else ("model" if mv else None))
    return mv or lv, ("model" if mv else ("local" if lv else None))

def write_as(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == dst.suffix.lower():
        dst.write_bytes(src.read_bytes())
        return
    with Image.open(src) as im:
        im = im.convert("RGB")
        if dst.suffix.lower() in (".jpg", ".jpeg"):
            im.save(dst, "JPEG", quality=95, optimize=True)
        else:
            im.save(dst, "PNG", optimize=True)

def main():
    plan_doc = json.load(io.open(REP/"image-remediation-plan.json", encoding="utf-8"))
    plan_items = plan_doc["items"] if isinstance(plan_doc, dict) else plan_doc
    plan = {i["path"]: i for i in plan_items}
    md5_edit = build_md5_edit_index()
    out = {"schema": "compliant-image-map/1", "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "items": {}}
    stats = {"copied": 0, "remediated": 0, "missingEdit": 0, "videos": 0, "videoExcluded": 0,
             "sharedMd5Reuse": 0}
    for d in sorted(CAND.iterdir()):
        f = d/"source-normalized.json"
        if not f.is_file():
            continue
        c = json.load(io.open(f, encoding="utf-8"))
        item = str(c["sourceItemId"])
        groups = {"spu_main": [], "sku_main": [], "detail": []}
        refs = [("spu_main", m) for m in c.get("mainImages") or []]
        for s in c.get("skus") or []:
            if s.get("skuImage"):
                refs.append(("sku_main", s["skuImage"]))
        refs += [("detail", m) for m in c.get("detailImages") or []]
        for role, ref in refs:
            src = Path(ref["file"])
            if not src.is_file():
                stats["missingEdit"] += 1
                groups[role].append({"name": src.name, "source": str(src),
                                     "sourceIndex": ref.get("sourceIndex"), "error": "source_missing"})
                continue
            meth = (plan.get(str(src)) or {}).get("method")
            edit, origin = find_edit(item, src, meth)
            if edit is None:
                shared = md5_edit.get(md5f(src))
                if shared is not None:
                    edit, origin = shared, "shared-md5"
                    stats["sharedMd5Reuse"] += 1
            dst = OUT/item/src.name
            if edit is not None:
                write_as(edit, dst)
                stats["remediated"] += 1
            else:
                write_as(src, dst)
                stats["copied"] += 1
            groups[role].append({"name": src.name, "source": str(src),
                                 "sourceIndex": ref.get("sourceIndex"),
                                 "compliant": str(dst), "remediated": edit is not None,
                                 "remediationOrigin": origin,
                                 "method": meth or ("SHARED_MD5_REMEDIATED" if origin == "shared-md5" else "ORIGINAL"),
                                 "sha256": sha256(dst)})
        vinfo = None
        vid = (c.get("video") or {}).get("file")
        if vid and Path(vid).is_file():
            dstv = OUT/item/"video.mp4"
            dstv.parent.mkdir(parents=True, exist_ok=True)
            dstv.write_bytes(Path(vid).read_bytes())
            up = item not in VIDEO_EXCLUDE
            vinfo = {"compliant": str(dstv), "source": vid, "uploadVideo": up,
                     "note": "" if up else "视频画面含德国/官方/旗舰等违规文字，无法逐帧修复，本批不上传该视频"}
            stats["videos" if up else "videoExcluded"] += 1
        out["items"][item] = {"categoryId": c["categoryId"], "categoryName": c["categoryName"],
                              "title": c["title"], "itemNumber": c["itemNumber"],
                              "groups": groups, "video": vinfo}
    (REP/"compliant-image-map.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False))
    print("items:", len(out["items"]))

if __name__ == "__main__":
    main()
