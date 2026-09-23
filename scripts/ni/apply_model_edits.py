# -*- coding: utf-8 -*-
"""Apply verified regenerated images to compliant slots.
PASS priority: Oxygen-Product-Pro > Oxygen-Imagen > sufy-image2 > old model-raw reuse.
Also: delete delete-plan detail pages, md5-dedup details, refresh candidate.json.
Main-role delete-plan entries are NOT deleted here (main count must be preserved);
item 989409622246 main-03 is handled separately by update_main_images.
"""
import argparse, hashlib, io, json, sys
from pathlib import Path
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch as ni_batch  # noqa: E402

ROOT = ni_batch.root_from_argv()

VERIFY_PRIORITY = [
    ("image-edits-pro",    "pro-verify.jsonl"),
    ("image-edits-imagen", "imagen-verify.jsonl"),
    ("image-edits-sufy",   "sufy-verify.jsonl"),
    ("image-edits-model-raw", "reuse-verify.jsonl"),
]

def load_jsonl(p):
    rows=[]
    if not p.is_file(): return rows
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line=line.strip()
        if line:
            try: rows.append(json.loads(line))
            except Exception: pass
    return rows

def find_slot(item_dir, stem):
    for ext in (".jpg",".png",".jpeg",".webp"):
        c=item_dir/(stem+ext)
        if c.is_file(): return c
    return None

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--items", default="", help="comma list of source item ids; empty = all ledger items")
    a=ap.parse_args()

    led=json.loads((ROOT/"ledger.json").read_text(encoding="utf-8-sig"))
    all_items=[sid for sid,v in led["items"].items() if v.get("status")=="VERIFIED"]
    if a.items:
        want={x.strip() for x in a.items.split(",") if x.strip()}
        all_items=[i for i in all_items if i in want]

    # 1) build (item, srcName) -> PASS output
    passed={}
    for dirname, vfile in VERIFY_PRIORITY:
        for r in load_jsonl(ROOT/"reports"/vfile):
            if not r.get("ok"): continue
            key=(r.get("item"), r.get("name"))
            if key not in passed:  # first hit wins = priority order
                out=Path(r["output"])
                if out.is_file() and out.stat().st_size>8000:
                    passed[key]=out

    delplan=json.loads((ROOT/"reports"/"delete-plan.json").read_text(encoding="utf-8-sig"))
    detail_deletes=[(Path(p).parent.name, Path(p).name) for p,v in delplan.items()
                    if Path(p).name.split("-")[0]!="main"]
    main_deletes=[(Path(p).parent.name, Path(p).name) for p,v in delplan.items()
                  if Path(p).name.split("-")[0]=="main"]

    report={"applied":0,"appliedByModel":{"pro":0,"imagen":0,"sufy":0,"reuse":0},
            "missingPass":[],"deletedDetails":0,"dedupDropped":0,"candidatesUpdated":0,
            "items":{}}
    dd_set=set(detail_deletes)

    for item in all_items:
        comp=ROOT/"images"/"compliant"/item
        cand_path=ROOT/"candidates"/item/"candidate.json"
        if not comp.is_dir() or not cand_path.is_file(): continue
        cd=json.loads(cand_path.read_text(encoding="utf-8"))
        changed=False
        stats={"applied":0,"deleted":0,"dedup":0}

        # 2) replace slots with PASS outputs (resize back to slot size)
        for f in sorted(comp.iterdir()):
            if not f.is_file(): continue
            src_name=f.name
            if (item, src_name) in dd_set: continue  # will be deleted, skip replace
            stem=src_name.rsplit(".",1)[0]
            out=passed.get((item, src_name))
            if not out: continue
            with Image.open(f) as orig: ow,oh=orig.size
            with Image.open(out) as im:
                rgb=im.convert("RGB")
                if rgb.size!=(ow,oh):
                    rgb=rgb.resize((ow,oh), Image.LANCZOS)
                if not a.dry_run:
                    if src_name.lower().endswith((".jpg",".jpeg")):
                        rgb.save(f,"JPEG",quality=92)
                    else:
                        rgb.save(f,"PNG")
            stats["applied"]+=1; report["applied"]+=1; changed=True
            for tag,d in (("pro",("image-edits-pro",)),("imagen",("image-edits-imagen",)),
                          ("sufy",("image-edits-sufy",)),("reuse",("image-edits-model-raw",))):
                if str(d[0]) in str(out): report["appliedByModel"][tag]+=1

        # 3) delete delete-plan detail pages
        for (it,nm) in detail_deletes:
            if it!=item: continue
            slot=find_slot(comp, nm.rsplit(".",1)[0])
            if slot and slot.is_file():
                if not a.dry_run: slot.unlink()
                stats["deleted"]+=1; report["deletedDetails"]+=1; changed=True

        # 4) rebuild detailImages: sorted, md5-dedup
        det_files=sorted([f for f in comp.glob("detail-*.*") if f.is_file()])
        seen={}; keep=[]
        for f in det_files:
            h=hashlib.md5(f.read_bytes()).hexdigest()
            if h in seen:
                stats["dedup"]+=1; report["dedupDropped"]+=1; changed=True
                continue
            seen[h]=f; keep.append(str(f))
        if keep!=(cd.get("detailImages") or []):
            cd["detailImages"]=keep; changed=True

        # 5) mainImages refresh (no deletion here)
        mains=sorted([str(f) for f in comp.glob("main-*.*") if f.is_file()])
        if mains!=(cd.get("mainImages") or []):
            cd["mainImages"]=mains; changed=True

        if changed:
            if not a.dry_run:
                cand_path.write_text(json.dumps(cd,ensure_ascii=False,indent=1),encoding="utf-8")
            report["candidatesUpdated"]+=1
        report["items"][item]=stats

    # tasks expected but no PASS output yet (for coverage accounting)
    plan=json.loads((ROOT/"reports"/"regen-plan.json").read_text(encoding="utf-8-sig"))
    for p in plan:
        pp=Path(p); key=(pp.parent.name, pp.name)
        if key in dd_set: continue
        if key not in passed:
            report["missingPass"].append(str(pp))

    report["mainDeletesDeferred"]=[f"{i}/{n}" for i,n in main_deletes]
    out=ROOT/"reports"/"apply-oxygen2-report.json"
    out.write_text(json.dumps(report,ensure_ascii=False,indent=1),encoding="utf-8")
    print(("DRY-RUN " if a.dry_run else "")+"applied:",report["applied"],
          "byModel:",report["appliedByModel"],
          "deletedDetails:",report["deletedDetails"],
          "dedupDropped:",report["dedupDropped"],
          "candidatesUpdated:",report["candidatesUpdated"],
          "missingPass:",len(report["missingPass"]),flush=True)

if __name__=="__main__":
    main()
