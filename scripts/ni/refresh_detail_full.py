# -*- coding: utf-8 -*-
"""Full detail-image refresh: replace the whole WareEditor desc list in one patch.
Unlike update_detail_images.py this intentionally allows count changes (page
deletions and dedup). Uploads new images via the shared cache. Only for VERIFIED items."""
import argparse, hashlib, io, json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jdo_listing_mvp as cli
import batch as ni_batch
import listing_batch as A

def online_desc_images(info):
    desc = ((info.get("productDetailDesc") or {}).get("desc") or "")
    return re.findall(r"<img[^>]*src=[\"']([^\"']+)[\"']", desc)

def build_desc(urls):
    return "".join("<p><img src='%s'/></p>" % cli.detail_image_url(u) for u in urls)

def expected_urls(item):
    candidate = A.load(A.ROOT/"candidates"/item/"candidate.json")
    cache_path = A.ROOT/"image-upload-cache.json"
    cache = A.load(cache_path) if cache_path.exists() else {}
    reference = A.load(A.ROOT/A.CATEGORY_RULES[candidate["categoryId"]]["reference"])
    out = []
    for path_text in candidate["detailImages"]:
        path = Path(path_text)
        if not path.is_file():
            raise RuntimeError("missing compliant detail image: %s" % path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest not in cache:
            resp = cli.upload(reference["media"]["zoneId"], reference["media"]["defaultCategoryId"], path)
            if not resp.get("imgUrl"):
                raise RuntimeError("upload failed: %s" % path)
            cache[digest] = {**resp, "localPath": str(path), "sha256": digest}
            A.save(cache_path, cache)
        out.append(str(cache[digest]["imgUrl"]))
    return out

def main():
    ap = argparse.ArgumentParser()
    ni_batch.add_root_argument(ap)
    ap.add_argument("--items", required=True)
    ap.add_argument("--confirm", default="")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    A.set_root(args.root)
    if args.apply and args.confirm != "UPDATE_PRODUCT_IMAGES":
        raise RuntimeError("need --confirm UPDATE_PRODUCT_IMAGES")
    A.identity()
    report = {"updatedAt": A.now(), "results": []}
    for pair in [x.strip() for x in args.items.split(",") if x.strip()]:
        item, _, pid_text = pair.partition(":")
        pid = int(pid_text)
        row = {"sourceItemId": item, "productId": pid}
        try:
            ap_detail = cli.get("jdo-product-update", "/rest/sp-product/v0/product-apply/{applyId}",
                                {"applyId": str(pid), "scene": "pop"}).get("data") or {}
            info = ap_detail.get("productInfo") or {}
            online = [str(u) for u in online_desc_images(info)]
            want_codes = expected_urls(item)
            want = [cli.detail_image_url(u) for u in want_codes]
            row.update({"onlineCount": len(online), "expectedCount": len(want)})
            if online == want:
                row["status"] = "ALREADY_CURRENT"
            elif not args.apply:
                row["status"] = "DRY_RUN_WOULD_PATCH"
            else:
                component = {"componentCode": "WareEditor", "value": [{"componentCode": "detail", "properties": [
                    {"key": "desc", "value": build_desc(want_codes)},
                    {"key": "zhuangBaUseFlag", "value": "0"},
                    {"key": "userAgent", "value": "pc"}]}]}
                body = {"applyId": str(pid), "opType": 3, "scene": "pop",
                        "productInfo": {"productId": pid,
                          "categoryDetail": {"lastCategoryId": int((info.get("categoryDetail") or {}).get("lastCategoryId") or 0)}},
                        "productApplyDTO": {"id": str(pid)},
                        "patchOperationList": [{"path": "product",
                          "value": json.dumps(component, ensure_ascii=False, separators=(",", ":"))}]}
                body_path = A.ROOT/"candidates"/item/"detail-full-refresh-body.json"
                A.save(body_path, body)
                resp = cli.run_jdo_large(url="/rest/sp-product/v0/product-applies/{applyId}",
                                         method="PATCH", body_path=body_path, params={"applyId": str(pid)},
                                         skill_id="jdo-product-update", yes=True)
                A.save(A.ROOT/"candidates"/item/"detail-full-refresh-response.json", resp)
                row["status"] = "PATCH_SUBMITTED"; row["response"] = resp
        except Exception as e:
            row["status"] = "ERROR"; row["error"] = str(e)
        print(json.dumps({k: v for k, v in row.items() if k not in ("response",)}, ensure_ascii=False), flush=True)
        report["results"].append(row)
    tag = "detail-full-refresh-results.json" if args.apply else "detail-full-refresh-dryrun.json"
    A.save(A.ROOT/"reports"/tag, report)

if __name__ == "__main__":
    main()
