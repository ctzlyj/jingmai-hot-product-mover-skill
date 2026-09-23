# -*- coding: utf-8 -*-
"""Refresh the WareEditor (detail image) component of an already-live product.

Only the WareEditor component is patched (opType=3). Everything else on the
live listing is left untouched. The patch is refused unless the live detail
image count equals the local compliant detail image count, so no image can be
silently added or dropped.
"""
from __future__ import annotations
import argparse, io, json, re, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jdo_listing_mvp as cli  # noqa: E402
import batch as ni_batch  # noqa: E402
import listing_batch as A  # noqa: E402


def online_desc_images(info: dict) -> list[str]:
    desc = ((info.get("productDetailDesc") or {}).get("desc") or "")
    return re.findall(r"<img[^>]*src=[\"']([^\"']+)[\"']", desc)


def build_desc(urls: list[str]) -> str:
    return "".join("<p><img src='%s'/></p>" % cli.detail_image_url(u) for u in urls)


def expected_urls(item: str) -> list[str]:
    candidate = A.load(A.ROOT / "candidates" / item / "candidate.json")
    cache_path = A.ROOT / "image-upload-cache.json"
    cache = A.load(cache_path) if cache_path.exists() else {}
    reference = A.load(A.ROOT / A.CATEGORY_RULES[candidate["categoryId"]]["reference"])
    out = []
    for path_text in candidate["detailImages"]:
        path = Path(path_text)
        if not path.is_file():
            raise RuntimeError("missing compliant detail image: %s" % path)
        import hashlib
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest not in cache:
            resp = cli.upload(reference["media"]["zoneId"], reference["media"]["defaultCategoryId"], path)
            if not resp.get("imgUrl"):
                raise RuntimeError("upload failed: %s" % path)
            cache[digest] = {**resp, "localPath": str(path), "sha256": digest}
            A.save(cache_path, cache)
        out.append(str(cache[digest]["imgUrl"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ni_batch.add_root_argument(ap)
    ap.add_argument("--items", required=True, help="comma list itemId:productId")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    A.set_root(args.root)
    if args.apply and args.confirm != "UPDATE_PRODUCT_IMAGES":
        raise RuntimeError("真实修改商品图片必须传 --confirm UPDATE_PRODUCT_IMAGES")
    A.identity()
    report = {"updatedAt": A.now(), "results": []}
    for pair in [x.strip() for x in args.items.split(",") if x.strip()]:
        item, _, pid_text = pair.partition(":")
        product_id = int(pid_text)
        row = {"sourceItemId": item, "productId": product_id}
        try:
            ap_detail = cli.get("jdo-product-update", "/rest/sp-product/v0/product-apply/{applyId}",
                                {"applyId": str(product_id), "scene": "pop"}).get("data") or {}
            info = ap_detail.get("productInfo") or {}
            online = [str(u) for u in online_desc_images(info)]
            want_codes = expected_urls(item)
            want = [cli.detail_image_url(u) for u in want_codes]
            row.update({"onlineCount": len(online), "expectedCount": len(want),
                        "onlineUrls": online, "expectedUrls": want})
            if online == want:
                row["status"] = "ALREADY_CURRENT"
                report["results"].append(row)
                print(json.dumps({k: v for k, v in row.items() if k not in ("onlineUrls", "expectedUrls")},
                                 ensure_ascii=False), flush=True)
                continue
            if len(online) != len(want):
                row["status"] = "REFUSED_COUNT_MISMATCH"
                report["results"].append(row)
                print(json.dumps({"sourceItemId": item, "status": "REFUSED_COUNT_MISMATCH",
                                  "online": len(online), "expected": len(want)}, ensure_ascii=False), flush=True)
                continue
            diff_positions = [i + 1 for i, (a, b) in enumerate(zip(online, want)) if a != b]
            row["diffPositions"] = diff_positions
            if not args.apply:
                row["status"] = "DRY_RUN_WOULD_PATCH"
                report["results"].append(row)
                print(json.dumps({"sourceItemId": item, "status": "DRY_RUN_WOULD_PATCH",
                                  "diffPositions": diff_positions}, ensure_ascii=False), flush=True)
                continue
            component = {
                "componentCode": "WareEditor",
                "value": [{"componentCode": "detail", "properties": [
                    {"key": "desc", "value": build_desc(want_codes)},
                    {"key": "zhuangBaUseFlag", "value": "0"},
                    {"key": "userAgent", "value": "pc"}]}],
            }
            body = {
                "applyId": str(product_id), "opType": 3, "scene": "pop",
                "productInfo": {"productId": product_id,
                                "categoryDetail": {"lastCategoryId": int((info.get("categoryDetail") or {}).get("lastCategoryId") or 0)}},
                "productApplyDTO": {"id": str(product_id)},
                "patchOperationList": [
                    {"path": "product",
                     "value": json.dumps(component, ensure_ascii=False, separators=(",", ":"))}
                ],
            }
            body_path = A.ROOT / "candidates" / item / "image-update-body.json"
            A.save(body_path, body)
            response = cli.run_jdo_large(url="/rest/sp-product/v0/product-applies/{applyId}",
                                         method="PATCH", body_path=body_path,
                                         params={"applyId": str(product_id)},
                                         skill_id="jdo-product-update", yes=True)
            A.save(A.ROOT / "candidates" / item / "image-update-response.json", response)
            row["status"] = "PATCH_SUBMITTED"
            row["response"] = response
            report["results"].append(row)
            print(json.dumps({"sourceItemId": item, "status": "PATCH_SUBMITTED",
                              "data": response.get("data")}, ensure_ascii=False), flush=True)
        except Exception as error:  # noqa: BLE001
            row["status"] = "ERROR"
            row["error"] = str(error)
            report["results"].append(row)
            print(json.dumps({"sourceItemId": item, "status": "ERROR", "error": str(error)},
                             ensure_ascii=False), flush=True)
    tag = "image-update-results.json" if args.apply else "image-update-dryrun.json"
    A.save(A.ROOT / "reports" / tag, report)


if __name__ == "__main__":
    main()
