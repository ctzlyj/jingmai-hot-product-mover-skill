from __future__ import annotations

import copy
import json

from pdd_ab_image_publish import digest_json, original_fingerprint
from pdd_local_export_mvp import first_dimension_is_size


def match_workbook(variants, rows):
    swap = first_dimension_is_size(rows)
    indexed = {}
    codes = set()
    for row in rows:
        code = row.get("vendorSku")
        if not isinstance(code, str) or not code.strip() or code.startswith("="):
            raise ValueError(f"第{row.get('rowNumber')}行缺少有效商家编码")
        if code in codes:
            raise ValueError(f"商家编码重复：{code}")
        codes.add(code)
        attributes = {"款式": row["package"] if swap else row["style"]}
        if row["package"]:
            attributes["套餐"] = row["style"] if swap else row["package"]
        key = tuple(sorted(attributes.items()))
        if key in indexed:
            raise ValueError("当前表格存在重复规格组合")
        indexed[key] = row
    matched = {}
    used = set()
    for variant in variants:
        key = tuple(sorted(variant["sourceAttributes"].items()))
        source = variant["sourceSku"]
        if key not in indexed or key in used or source in matched:
            raise ValueError(f"来源SKU规格不能唯一匹配当前表格：{source}")
        used.add(key)
        matched[source] = copy.deepcopy(indexed[key])
    if used != set(indexed):
        raise ValueError("当前表格与历史商品规格集合不一致")
    return matched


def noncode_signature(detail):
    stable = copy.deepcopy(detail)
    for sku in stable.get("skuList") or []:
        sku.pop("outerId", None)
    return original_fingerprint(stable)


def make_body(product_id, category_detail, rows):
    return {
        "applyId": str(product_id), "opType": 3, "scene": "pop",
        "productInfo": {"productId": int(product_id), "categoryDetail": copy.deepcopy(category_detail)},
        "productApplyDTO": {"id": str(product_id), "limitInfoSubmitMethod": 2},
        "patchOperationList": [
            {"path": f"skus[skuId=={int(row['skuId'])}]",
             "value": json.dumps({"componentCode": "SkuVendorSku", "value": row["merchantCode"]},
                                 ensure_ascii=False, separators=(",", ":"))}
            for row in rows
        ],
    }


def validate_body(payload, product_id, rows):
    if not rows or len({int(row["skuId"]) for row in rows}) != len(rows):
        raise ValueError("Empty or duplicate SKU patch")
    if any(not isinstance(row["merchantCode"], str) or not row["merchantCode"].strip() for row in rows):
        raise ValueError("Merchant codes must be nonempty strings")
    expected = make_body(product_id, payload.get("productInfo", {}).get("categoryDetail"), rows)
    if payload != expected:
        raise ValueError("Payload must update only the approved merchant-code fields")


def require_confirmation(plan, confirm_token, authorization):
    if authorization != "REPAIR_MERCHANT_CODES" or confirm_token != digest_json(plan):
        raise ValueError("Merchant-code repair authorization or exact plan digest mismatch")


def properties(values):
    return [{"key": key, "value": value} for key, value in values.items()]


def image_components(images, color_id):
    return [{"componentCode": "img", "properties": properties({
        "imgRefId": str(color_id), "colorId": str(color_id), "orderSort": image["orderSort"],
        "imgUrl": image["imgUrl"], "isPrimary": int(image.get("primaryFlag", False)),
        "isGgt": int(image.get("orderSort") == 1),
    })} for image in images]


def attribute_components(attributes, scope):
    result = []
    for attribute in attributes:
        values = ",".join(str(value["attrValue"]) for value in attribute["values"])
        if scope == "sku":
            values = f"[{values}]"
        props = {"attrValues": values}
        for field, key in (("expand", "expands"), ("unit", "units")):
            if any(value.get(field) for value in attribute["values"]):
                props[key] = ",".join(str(value.get(field) or "") for value in attribute["values"])
        result.append({"componentCode": f"prop-{scope}-{attribute['attrId']}", "properties": properties(props)})
    return result


def ordered_skus(skus):
    return sorted(skus, key=lambda sku: tuple(int(attribute["attrValuesSeqNo"]) for attribute in
                  sorted(sku["saleAttrs"], key=lambda attribute: int(attribute["indexId"]))))


def make_full_edit(template, detail, rows):
    info = detail["productInfo"]
    desired = {int(row["skuId"]): row["merchantCode"] for row in rows}
    if not set(desired).issubset({int(sku["skuId"]) for sku in detail["skuList"]}):
        raise ValueError("Target SKU missing from live product")
    groups = {str(group["uuid"]): group["imageInfoList"] for group in detail["material"]["mainImages"]}
    features = {item["key"]: item["value"] for item in info.get("features", [])}
    replacements = {
        "WareTitle": info["productName"], "WareBrand": info["brandInfo"]["brandId"],
        "WareItemnum": info.get("itemNum", ""), "WareSevenDayReturn": features.get("is7ToReturn", "1"),
        "WareTransport": str(info["logisticsInfo"]["transportId"]),
        "CategoryGroupList": attribute_components(info.get("goodsAttrInfos", []), "product"),
        "WareSkuImg": image_components(groups.get("0000000000", []), "0000000000"),
        "WareEditor": [{"componentCode": "detail", "properties": properties({
            "desc": info["productDetailDesc"]["desc"], "zhuangBaUseFlag": "0", "userAgent": "pc"})}],
    }
    sku_components = []
    for sku in ordered_skus(detail["skuList"]):
        sku_id = int(sku["skuId"])
        values = [
            {"componentCode": "SkuUUid", "value": str(sku_id)},
            {"componentCode": "SkuId", "value": sku_id},
            {"componentCode": "SkuJdprice", "value": str(sku["priceInfo"]["jdPrice"])},
            {"componentCode": "SkuVendorSku", "value": desired.get(sku_id, sku.get("outerId", ""))},
            {"componentCode": "SkuEnableStatus", "value": str(sku["skuEnableStatus"])},
            {"componentCode": "SkuAttr", "value": attribute_components(sku.get("goodsAttrInfos", []), "sku")},
        ]
        short_title = next((item["value"] for item in sku.get("features", []) if item["key"] == "shortTitle"), None)
        if short_title is not None:
            values.append({"componentCode": "SkuShortTitle", "value": short_title})
        sale_attrs = []
        for attribute in sku["saleAttrs"]:
            sale_attrs.append({"componentCode": f"sale-prop-{attribute['attrId']}", "properties": properties({
                "attrValueAlias": f"[{attribute['attrValueAlias']}]", "attrName": attribute["attrName"],
                "mappingId": str(attribute["attrValues"]), "index": str(attribute["indexId"]),
                "attrValuesSeqNo": attribute["attrValuesSeqNo"],
            })})
            if str(attribute["attrId"]) == "1001080021":
                color_id = str(attribute["attrValues"])
                if color_id not in groups:
                    raise ValueError(f"Live color image group missing: {color_id}")
                values.append({"componentCode": "SkuMainImage", "value": image_components(groups[color_id], color_id)})
        values.append({"componentCode": "VenderCategoryValue", "value": sale_attrs})
        sku_components.append({"componentCode": "SKU", "value": values})
    replacements["SKUS"] = sku_components
    schema = copy.deepcopy(json.loads(template["schema"]))
    if len({component["componentCode"] for component in schema}) != len(schema):
        raise ValueError("Duplicate schema component")
    for component in schema:
        code = component["componentCode"]
        if code not in replacements:
            raise ValueError(f"Unreviewed full-edit component: {code}")
        component["value"] = replacements[code]
    if not any(component["componentCode"] == "SKUS" for component in schema):
        raise ValueError("Missing full-edit SKU set")
    return {"scene": "pop", "schema": json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            "productApplyDTO": {"productId": int(info["productId"]), "saveActionType": 0,
                                "categoryDTO": {"lastCategoryId": int(info["categoryDetail"]["lastCategoryId"])}}}


def validate_full_edit(body, template, detail, rows):
    if body != make_full_edit(template, detail, rows):
        raise ValueError("Full edit differs from live-preserving approved payload")
    if '"SkuStockNum"' in body["schema"]:
        raise ValueError("Existing-product edits must not submit stock")
    schema = json.loads(body["schema"])
    skus = next(component["value"] for component in schema if component["componentCode"] == "SKUS")
    ids = [next(component["value"] for component in sku["value"] if component["componentCode"] == "SkuId") for sku in skus]
    if len(ids) != len(set(ids)) or set(ids) != {int(sku["skuId"]) for sku in detail["skuList"]}:
        raise ValueError("Full edit must preserve exactly all existing SKU IDs")
