# -*- coding: utf-8 -*-
"""Hot-replica single-link price plan (formal, parameterized).

Competitor hand price per SKU = manifest sourcePrice x --competitor-ratio
(1.0 = "JD source price IS the competitor hand price"; use the ratio derived
from the source link's subsidy tag when it has one, e.g. 0.85 for 15%).
Target hand = competitor hand x --target-multiplier (default 0.97). JD price is
reverse-solved with source_pricing.component_source_plan (Decimal, component
rounding) - the ONLY legal calculation entry; hand-rolled float solving is
forbidden (1-cent drift gets blocked by upload/preflight).
Formalized from the battle-tested task-local 64_price_plan.py.
"""
import argparse, json, pathlib, sys
from decimal import Decimal, ROUND_HALF_UP
sys.stdout.reconfigure(encoding="utf-8")
SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import jd_product_mover as mover  # noqa: E402
from source_pricing import component_source_plan  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sku", required=True)
    ap.add_argument("--draft-root", default=str(SKILL / "outputs" / "migrations"))
    ap.add_argument("--work-dir", default=None,
                    help="output dir for price-plan-<sku>.json (default <draft>/hot-replica)")
    ap.add_argument("--competitor-ratio", default="1.0",
                    help="sourcePrice -> competitor hand ratio; 1.0 when the JD price has no "
                         "active subsidy, else derive from the source tag (e.g. 0.85)")
    ap.add_argument("--target-multiplier", default="0.97",
                    help="target hand = competitor hand x this (user-confirmed 3% under)")
    a = ap.parse_args()
    ratio = Decimal(str(a.competitor_ratio))
    target_mult = Decimal(str(a.target_multiplier))
    if ratio <= 0 or not (0 < target_mult <= 1):
        print("FAIL: invalid ratios")
        return 1
    root = pathlib.Path(a.draft_root)
    folder = next(root.glob(a.sku + "_*"), None)
    if not folder:
        print(f"FAIL: draft folder not found for {a.sku} under {root}")
        return 1
    work = pathlib.Path(a.work_dir) if a.work_dir else folder / "hot-replica"
    work.mkdir(parents=True, exist_ok=True)

    listing = mover.read_json(folder / "listing.json")
    manifest = mover.read_json(folder / "manifest.json")
    source_price_by_sku = {str(v["sourceSku"]): Decimal(str(v["sourcePrice"]))
                           for v in manifest["variants"]}
    pricing = listing["source"]["pricing"]
    pricing["roundingMode"] = "component"
    pricing["targetArrivalMultiplier"] = str(target_mult)
    pricing["belowCompetitor"] = "0"
    pricing["coupon"] = "0"
    rows = []
    for s in listing["skus"]:
        source_price = source_price_by_sku[str(s["sourceSku"])]
        comp_hand = (source_price * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        plan = component_source_plan(comp_hand, pricing)
        s["sourcePrice"] = float(comp_hand)
        s["jdPrice"] = float(plan.jd_price)
        rows.append({
            "skuKey": s["skuKey"], "styleAlias": s.get("styleAlias"),
            "sizeAlias": s.get("sizeAlias"),
            "sourcePrice": str(source_price), "competitorRatio": str(ratio),
            "competitorHand": str(comp_hand), "targetHand": str(plan.target_final_price),
            "newJdPrice": str(plan.jd_price), "direct": str(plan.direct_reduction),
            "subsidy": str(plan.government_subsidy),
        })
    mover.write_json(folder / "listing.json", listing)
    mover.build_audit_workbook(manifest, listing, folder / f"{folder.name}.xlsx")
    (work / f"price-plan-{a.sku}.json").write_text(
        json.dumps({"sku": a.sku, "ratio": str(ratio),
                    "ratioNote": f"sourcePrice x {ratio} = competitor hand; "
                                 f"target = x {target_mult}",
                    "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    prices = sorted({r["newJdPrice"] for r in rows})
    print(f"OK {a.sku} | ratio={ratio} | skus={len(rows)} | jd {prices[0]}..{prices[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
