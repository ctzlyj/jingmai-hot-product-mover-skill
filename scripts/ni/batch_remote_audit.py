# -*- coding: utf-8 -*-
"""Batch remote audit of all VERIFIED items: title/category/codes/price/stock/shelf + detail current."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import jdo_listing_mvp as cli  # noqa: E402
import batch as ni_batch  # noqa: E402
import listing_batch as A  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    ni_batch.add_root_argument(parser)
    parser.add_argument("--offline-items", default="",
                        help="comma list of sourceItemIds taken offline by the user; default: reports/user-offline-items.json")
    args = parser.parse_args()
    A.set_root(args.root)
    root = A.ROOT
    A.identity()
    if args.offline_items:
        offline_user = {x.strip() for x in args.offline_items.split(",") if x.strip()}
    else:
        path = root / "reports" / "user-offline-items.json"
        offline_user = set(json.loads(path.read_text(encoding="utf-8-sig")).get("items") or []) if path.is_file() else set()

    verified = []
    ledger = json.loads((root / "ledger.json").read_text(encoding="utf-8-sig"))
    for item, value in sorted(ledger["items"].items()):
        if value.get("status") == "VERIFIED" and value.get("productId"):
            verified.append((item, int(value["productId"])))
    out = {"schema": "batch-remote-audit/1", "generatedAt": A.now(), "items": {}}
    ok_count = 0
    for item, pid in verified:
        cd = A.load(root / "candidates" / item / "candidate.json")
        row = {"productId": pid, "errors": [], "skus": []}
        try:
            r = cli.get("jdo-product-status", "/rest/sp-product/v0/products/{productId}",
                        {"productId": pid, "scene": "pop"})
            d = r.get("data") or {}
            info = d.get("productInfo") or {}
            skus = d.get("skuList") or []
            if info.get("itemNum") != cd["itemNumber"] or int((info.get("brandInfo") or {}).get("brandId") or 0) != -1:
                row["errors"].append("货号或品牌不一致")
            if (int((info.get("categoryDetail") or {}).get("lastCategoryId") or 0) != cd["categoryId"]
                    or info.get("productName") != cd["title"]):
                row["errors"].append("类目或标题不一致")
            if int((info.get("productStatus") or {}).get("productStatusNew") or 0) != 103 and item not in offline_user:
                row["errors"].append("商品未上架: %s" % (info.get("productStatus") or {}))
            actual_by = {str(s.get("outerId") or ""): s for s in skus}
            if set(actual_by) != {r2["vendorSku"] for r2 in cd["skus"]}:
                row["errors"].append("SKU数量或商家编码不一致")
            stocks = cli.query_sku_stocks([int(s["skuId"]) for s in skus]) if skus else []
            stock_by = {int(x["skuId"]): int(x["stockNum"]) for x in stocks}
            for exp in cd["skus"]:
                act = actual_by.get(exp["vendorSku"])
                if not act:
                    row["errors"].append("缺少SKU %s" % exp["vendorSku"])
                    continue
                audit = A.component_source_audit(exp["sourcePrice"], act["priceInfo"]["jdPrice"], cd["pricing"])
                sku_row = {"vendorSku": exp["vendorSku"], "skuId": act["skuId"],
                           "jdPrice": act["priceInfo"]["jdPrice"], "priceOk": audit["ok"],
                           "stock": stock_by.get(int(act["skuId"])),
                           "enabled": int(act.get("skuEnableStatus") or 0),
                           "shelf": int((act.get("skuStatus") or {}).get("onOffShelfStatus") or 0)}
                row["skus"].append(sku_row)
                if not audit["ok"]:
                    row["errors"].append("价格不一致:%s" % act["skuId"])
                if sku_row["stock"] != A.STOCK:
                    row["errors"].append("库存不一致:%s" % act["skuId"])
                if sku_row["enabled"] != 1 or sku_row["shelf"] != 1:
                    row["errors"].append("SKU未启用或未上架:%s" % act["skuId"])
        except Exception as error:  # noqa: BLE001
            row["errors"].append("查询异常: %s" % error)
        row["ok"] = not row["errors"]
        if row["ok"]:
            ok_count += 1
        out["items"][item] = row
        print(item, "OK" if row["ok"] else "ERR " + "; ".join(row["errors"][:4]), flush=True)
    out["summary"] = {"verified": len(verified), "remoteOk": ok_count,
                      "withErrors": len(verified) - ok_count}
    A.save(root / "reports" / "batch-remote-audit.json", out)
    print(json.dumps(out["summary"], ensure_ascii=False))
    return 0 if ok_count == len(verified) and verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
