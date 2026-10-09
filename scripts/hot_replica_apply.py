# -*- coding: utf-8 -*-
"""Hot-replica apply for one source SKU (formal, parameterized).

Repoints the draft listing at the remade (重制-固定) outputs from the remake
plan produced by hot_replica_preflight.py: all plan mains + kept details +
SKU spec images, flexible main count; series products also repoint their
per-style scene images. Upload checkpoints are invalidated when the image list
changes. Formalized from the battle-tested task-local 63_apply_fast.py; no
store-specific fields.
"""
import argparse, json, pathlib, sys
sys.stdout.reconfigure(encoding="utf-8")
SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import jd_product_mover as mover  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sku", required=True)
    ap.add_argument("--draft-root", default=str(SKILL / "outputs" / "migrations"))
    ap.add_argument("--work-dir", default=None,
                    help="dir holding remake-plan-<sku>.json (default <draft>/hot-replica)")
    a = ap.parse_args()
    root = pathlib.Path(a.draft_root)
    folder = next(root.glob(a.sku + "_*"), None)
    if not folder:
        print(f"FAIL: draft folder not found for {a.sku} under {root}")
        return 1
    work = pathlib.Path(a.work_dir) if a.work_dir else folder / "hot-replica"
    plan_path = work / f"remake-plan-{a.sku}.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    listing_path = folder / "listing.json"
    listing = mover.read_json(listing_path)
    product_key = listing["product"]["productKey"]
    media_cat = listing["product"]["defaultMediaCategoryId"]

    def check(out_rel):
        out = folder / out_rel
        if not out.is_file() or out.stat().st_size < 10000:
            raise RuntimeError(f"重制图缺失或过小：{out_rel}")
        return out_rel

    def entry(item, role):
        return {"imageKey": item["imageKey"], "productKey": product_key,
                "skuKey": "", "role": role, "localPath": check(item["output"]),
                "order": int(item.get("order") or 0),
                "mediaCategoryId": media_cat,
                "upload": {"status": "pending", "imgId": "", "imgUrl": ""}}

    mains = [entry(i, "spu_main") for i in sorted(
        [x for x in plan["images"] if x.get("role") == "spu_main"],
        key=lambda x: int(x.get("order") or 0))]
    details = [entry(i, "detail") for i in sorted(
        [x for x in plan["images"] if x.get("role") == "detail"],
        key=lambda x: int(x.get("order") or 0))]
    if len(mains) < 2:
        raise RuntimeError(f"主图必须至少2张（公共池+卖点首图），当前{len(mains)}")

    sku_out = {i["source"]: check(i["output"]) for i in plan["images"]
               if str(i.get("source", "")).startswith("SKU图/")}
    sku_mains, repurposed = [], set()
    for im in listing["images"]:
        if im.get("role") != "sku_main":
            continue
        src = im.get("localPath")
        if src not in sku_out:
            raise RuntimeError(f"SKU 规格图缺少重制映射：{im.get('imageKey')} -> {src}")
        repurposed.add(src)
        im["localPath"] = sku_out[src]
        im["upload"] = {"status": "pending", "imgId": "", "imgUrl": ""}
        sku_mains.append(im)
    if len(repurposed) != len(sku_out):
        raise RuntimeError(f"计划中存在未被 listing 引用的 SKU 源图：{sorted(set(sku_out) - repurposed)}")

    style_plan_by_key = {i["imageKey"]: i for i in plan["images"] if i.get("role") == "style_scene"}
    style_scenes, seen_style_keys = [], set()
    for mapping in plan.get("styleSceneMapping") or []:
        target = str(mapping.get("imageKey") or "")
        if target.startswith("SPU-MAIN-") or target in seen_style_keys or target not in style_plan_by_key:
            continue
        seen_style_keys.add(target)
        style_scenes.append(entry(style_plan_by_key[target], "style_scene"))

    listing["images"] = mains + details + style_scenes + sku_mains
    up = folder / ".pipeline" / "listing.uploaded.json"
    if up.exists():
        up.unlink()
        print("removed stale upload checkpoint")
    mover.write_json(listing_path, listing)
    print(f"{a.sku} images now: styleImageMode={plan.get('styleImageMode') or 'shared'} "
          f"styleScenes={len(style_scenes)} mains={len(mains)} "
          f"details={len(details)} sku={len(sku_mains)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
