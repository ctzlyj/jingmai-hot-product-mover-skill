# -*- coding: utf-8 -*-
"""Refresh main and SKU images for already-live no-inventory listings.

The patch is limited to WareSkuImg groups that actually differ from the local
compliant images.  The public SPU group and each style-specific group keep the
same image count and SKU ownership; only their URLs are replaced.
"""
from __future__ import annotations

import argparse
import json
import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jdo_listing_mvp as cli  # noqa: E402
import batch as ni_batch  # noqa: E402
import listing_batch as A  # noqa: E402
import jdo_listing_mvp as cli


def style_attribute_id(candidate: dict) -> str:
    component_code = str(
        ((candidate.get("saleComponents") or {}).get("style") or {}).get("componentCode") or ""
    )
    match = re.fullmatch(r"sale-prop-(\d+)", component_code)
    if not match:
        raise RuntimeError(f"invalid style sale component: {component_code}")
    return match.group(1)


def expected_uploads(candidate: dict, cache: dict, cache_path: Path) -> dict[str, list[str]]:
    reference = A.load(A.ROOT / A.CATEGORY_RULES[candidate["categoryId"]]["reference"])
    common = [
        A.upload_path(path, cache, cache_path, reference)
        for path in candidate["mainImages"]
    ]
    groups = {"0000000000": common}

    style_rows: dict[str, dict] = {}
    for sku in candidate["skus"]:
        alias = cli.normalized_sale_alias(str(sku.get("styleAlias") or ""))
        current = style_rows.get(alias)
        if current is None or int(sku.get("styleOrder") or 0) < int(current.get("styleOrder") or 0):
            style_rows[alias] = sku
    for alias, sku in style_rows.items():
        first = A.upload_path(sku["mainImage"], cache, cache_path, reference)
        groups[alias] = [first, *common[1:]]
    return groups


def image_component(color_id: str, order_sort: int, image_url: str) -> dict:
    return {
        "componentCode": "img",
        "inputType": "multiText",
        "properties": [
            cli.property_item("colorId", color_id),
            cli.property_item("imgUrl", image_url),
            cli.property_item("imgRefId", color_id),
            cli.property_item("orderSort", str(order_sort)),
        ],
    }


def compare_groups(
    candidate: dict,
    detail: dict,
    expected: dict[str, list[str]],
    allow_count_change: bool = False,
) -> tuple[list[dict], list[dict], list[str], dict[str, list[str]]]:
    style_id = style_attribute_id(candidate)
    online_ids = cli.online_style_color_ids(detail, style_id)
    online_groups = {
        str(group.get("uuid")): sorted(
            group.get("imageInfoList") or [],
            key=lambda item: int(item.get("orderSort") or 0),
        )
        for group in (detail.get("material") or {}).get("mainImages") or []
    }

    expected_by_color = {"0000000000": expected["0000000000"]}
    expected_groups = [{"colorId": "0000000000", "alias": "SPU", "urls": expected["0000000000"]}]
    for alias in sorted(
        set(expected) - {"0000000000"},
        key=lambda alias: next(s["styleOrder"] for s in candidate["skus"] if cli.normalized_sale_alias(s["styleAlias"]) == alias),
    ):
        color_id = online_ids.get(alias)
        if not color_id:
            raise RuntimeError(f"online style colorId missing: {alias}")
        expected_by_color[color_id] = expected[alias]
        expected_groups.append({"colorId": color_id, "alias": alias, "urls": expected[alias]})

    if set(online_groups) != {group["colorId"] for group in expected_groups}:
        raise RuntimeError(
            f"online group set mismatch: online={sorted(online_groups)} expected="
            f"{sorted(group['colorId'] for group in expected_groups)}"
        )

    changed: list[dict] = []
    unchanged: list[dict] = []
    errors: list[str] = []
    for group in expected_groups:
        color_id = group["colorId"]
        online_images = online_groups[color_id]
        if len(online_images) != len(group["urls"]):
            if not allow_count_change:
                errors.append(
                    f"group {color_id} image count mismatch: online={len(online_images)} "
                    f"expected={len(group['urls'])}"
                )
                continue
            group["countChange"] = {"online": len(online_images), "expected": len(group["urls"])}
        positions = {int(image.get("orderSort") or 0): cli.detail_image_url(str(image.get("imgUrl") or "")) for image in online_images}
        diffs = []
        for index, image_url in enumerate(group["urls"], start=1):
            actual = positions.get(index)
            wanted = cli.detail_image_url(str(image_url))
            if actual != wanted:
                diffs.append({"position": index, "actual": actual, "expected": wanted})
        row = {
            "colorId": color_id,
            "alias": group["alias"],
            "imageCount": len(group["urls"]),
            "changedPositions": [item["position"] for item in diffs],
            "diffs": diffs,
        }
        (changed if diffs else unchanged).append(row)
    return changed, unchanged, errors, expected_by_color


def build_body(product_id: int, detail: dict, changed: list[dict], expected: dict[str, list[str]]) -> dict:
    operations = []
    for group in changed:
        values = [
            image_component(group["colorId"], index, expected[group["colorId"]][index - 1])
            for index in range(1, len(expected[group["colorId"]]) + 1)
        ]
        component = {
            "componentCode": "WareSkuImg",
            "inputType": "multiText",
            "value": values,
        }
        operations.append({
            "path": "product",
            "value": json.dumps(component, ensure_ascii=False, separators=(",", ":")),
        })
    product_info = detail.get("productInfo") or detail
    category_id = int((product_info.get("categoryDetail") or {}).get("lastCategoryId") or 0)
    if not category_id:
        raise RuntimeError("online category id missing")
    return {
        "applyId": str(product_id),
        "opType": 3,
        "scene": "pop",
        "productInfo": {
            "productId": product_id,
            "categoryDetail": {"lastCategoryId": category_id},
        },
        "productApplyDTO": {"id": str(product_id)},
        "patchOperationList": operations,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    ni_batch.add_root_argument(parser)
    parser.add_argument("--items", required=True, help="comma list itemId:productId")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--allow-count-change", action="store_true",
                        help="explicitly allow main-image count change (e.g. deleting a brand-info page)")
    args = parser.parse_args()
    A.set_root(args.root)
    if args.apply and args.confirm != "UPDATE_PRODUCT_IMAGES":
        raise RuntimeError("real image update requires --confirm UPDATE_PRODUCT_IMAGES")

    A.identity()
    cache_path = A.ROOT / "image-upload-cache.json"
    cache = A.load(cache_path) if cache_path.exists() else {}
    report = {"updatedAt": A.now(), "results": []}
    for pair in [part.strip() for part in args.items.split(",") if part.strip()]:
        item, _, product_id_text = pair.partition(":")
        product_id = int(product_id_text)
        row = {"sourceItemId": item, "productId": product_id}
        try:
            candidate = A.load(A.ROOT / "candidates" / item / "candidate.json")
            detail = cli.get(
                "jdo-product-update",
                "/rest/sp-product/v0/products/{productId}",
                {"productId": str(product_id), "scene": "pop"},
            ).get("data") or {}
            expected = expected_uploads(candidate, cache, cache_path)
            changed, unchanged, errors, expected_by_color = compare_groups(
                candidate, detail, expected, allow_count_change=args.allow_count_change
            )
            row.update({
                "changedGroupCount": len(changed),
                "intendedCountChange": bool(args.allow_count_change),
                "unchangedGroupCount": len(unchanged),
                "changedPositions": [
                    {"colorId": group["colorId"], "alias": group["alias"], "positions": group["changedPositions"]}
                    for group in changed
                ],
                "errors": errors,
            })
            if errors:
                row["status"] = "REFUSED_GROUP_MISMATCH"
                report["results"].append(row)
                print(json.dumps({"sourceItemId": item, "status": row["status"], "errors": errors}, ensure_ascii=False), flush=True)
                continue
            if not changed:
                row["status"] = "ALREADY_CURRENT"
                report["results"].append(row)
                print(json.dumps({"sourceItemId": item, "status": "ALREADY_CURRENT"}, ensure_ascii=False), flush=True)
                continue
            if not args.apply:
                row["status"] = "DRY_RUN_WOULD_PATCH"
                report["results"].append(row)
                print(json.dumps({
                    "sourceItemId": item,
                    "status": "DRY_RUN_WOULD_PATCH",
                    "changedGroupCount": len(changed),
                }, ensure_ascii=False), flush=True)
                continue

            body = build_body(product_id, detail, changed, expected_by_color)
            body_path = A.ROOT / "candidates" / item / "main-image-update-body.json"
            A.save(body_path, body)
            response = cli.run_jdo_large(
                url="/rest/sp-product/v0/product-applies/{applyId}",
                method="PATCH",
                body_path=body_path,
                params={"applyId": str(product_id)},
                skill_id="jdo-product-update",
                yes=True,
            )
            A.save(A.ROOT / "candidates" / item / "main-image-update-response.json", response)
            row["status"] = "PATCH_SUBMITTED"
            row["response"] = response
            report["results"].append(row)
            print(json.dumps({
                "sourceItemId": item,
                "status": "PATCH_SUBMITTED",
                "data": response.get("data"),
            }, ensure_ascii=False), flush=True)
        except Exception as error:  # noqa: BLE001
            row["status"] = "ERROR"
            row["error"] = str(error)
            report["results"].append(row)
            print(json.dumps({"sourceItemId": item, "status": "ERROR", "error": str(error)}, ensure_ascii=False), flush=True)

    tag = "main-image-update-results.json" if args.apply else "main-image-update-dryrun.json"
    A.save(A.ROOT / "reports" / tag, report)


if __name__ == "__main__":
    main()
