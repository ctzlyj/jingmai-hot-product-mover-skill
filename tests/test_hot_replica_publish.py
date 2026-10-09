# -*- coding: utf-8 -*-
import json
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import hot_replica_publish as publish  # noqa: E402


def _edit_body(bindings):
    """bindings: list of (imgRefId, imgUrl) per SKU component."""
    skus = []
    for ref, url in bindings:
        skus.append({"componentCode": "SKUS-ITEM", "value": [
            {"componentCode": "SkuId", "value": ref},
            {"componentCode": "SkuGgtImage", "value": [
                {"componentCode": "img", "properties": [
                    {"key": "imgUrl", "value": url},
                    {"key": "imgRefId", "value": ref}]}]}]})
    return {"schema": json.dumps([{"componentCode": "SKUS", "value": skus}])}


def _apply_data(sku_ids):
    return {"skuList": [{"skuId": sid, "material": None} for sid in sku_ids]}


def test_ggt_report_counts_from_edit_body_not_material():
    # 2026-10-08 root cause: apply-detail material is ALWAYS null (no spec-image
    # data at all), so the old material-based count reported 0/12 forever.
    body = _edit_body([("102001", "https://img/ggt-a.jpg"), ("102002", "https://img/ggt-b.jpg")])
    report = publish.ggt_binding_report(body, _apply_data(["102001", "102002"]))
    assert report["expected"] == 2 and report["bound"] == 2
    assert report["source"] == "edit-body"
    assert not report["missingUrl"] and not report["unknownRef"]
    assert report["onlineSkuCount"] == 2


def test_ggt_report_flags_unknown_ref_and_missing_url():
    body = _edit_body([("102001", "https://img/ggt-a.jpg"),
                       ("999999", "https://img/ggt-b.jpg"),
                       ("102002", "")])
    report = publish.ggt_binding_report(body, _apply_data(["102001", "102002"]))
    assert report["expected"] == 3 and report["bound"] == 1
    assert report["unknownRef"] == ["999999"]
    assert report["missingUrl"] == ["102002"]


def test_ggt_report_without_online_skus_counts_urls():
    # Empty/failed recheck: fall back to URL presence in the submitted body.
    body = _edit_body([("102001", "https://img/ggt-a.jpg")])
    report = publish.ggt_binding_report(body, {"skuList": []})
    assert report["expected"] == 1 and report["bound"] == 1
    assert report["onlineSkuCount"] == 0


def _series_report_fixture():
    listing = {"skus": [
        {"skuKey": "P-SKU-001", "styleAlias": "款式A"},
        {"skuKey": "P-SKU-002", "styleAlias": "款式B"},
    ]}
    uploaded = {"images": [
        {"imageKey": "SKU-MAIN-001", "skuKey": "P-SKU-001", "role": "sku_main",
         "upload": {"imgUrl": "jfs/a1"}},
        {"imageKey": "SKU-MAIN-002", "skuKey": "P-SKU-002", "role": "sku_main",
         "upload": {"imgUrl": "jfs/b1"}},
        {"imageKey": "STYLE-01-POS02", "role": "style_scene",
         "upload": {"imgUrl": "jfs/a2"}},
        {"imageKey": "SPU-MAIN-02", "role": "spu_main",
         "upload": {"imgUrl": "jfs/shared2"}},
    ]}
    plan = {"styleImageMode": "series", "styleSceneMapping": [
        {"style": "款式A", "pos": 2, "imageKey": "STYLE-01-POS02"},
        {"style": "款式B", "pos": 2, "imageKey": "SPU-MAIN-02"},
    ]}
    apply_data = {"skuList": [
        {"skuId": "9001", "saleAttrs": [
            {"attrName": "颜色", "attrValueAlias": "款式A", "attrValues": "101A", "mappingId": ""}]},
        {"skuId": "9002", "saleAttrs": [
            {"attrName": "颜色", "attrValueAlias": "款式B", "attrValues": "101B", "mappingId": ""}]},
    ], "material": {"mainImages": [
        {"uuid": "101A", "imageInfoList": [
            {"orderSort": 1, "imgUrl": "jfs/a1"},
            {"orderSort": 2, "imgUrl": "jfs/a2"},
            {"orderSort": 3, "imgUrl": "jfs/a1"},
        ]},
        {"uuid": "101B", "imageInfoList": [
            {"orderSort": 1, "imgUrl": "jfs/b1"},
            {"orderSort": 2, "imgUrl": "jfs/shared2"},
        ]},
    ]}}
    return listing, uploaded, plan, apply_data


def test_series_report_verifies_distinct_live_sequences_and_accepts_server_tail():
    listing, uploaded, plan, apply_data = _series_report_fixture()
    report = publish.series_image_binding_report(listing, uploaded, plan, apply_data)
    assert report["ok"] is True
    assert report["mismatch"] == 0
    assert report["distinctSequences"] == 2
    assert report["styles"]["款式A"]["serverFilledTail"] is True
    assert report["styles"]["款式B"]["serverFilledTail"] is False

    apply_data["material"]["mainImages"][1]["imageInfoList"][1]["imgUrl"] = "jfs/wrong"
    report = publish.series_image_binding_report(listing, uploaded, plan, apply_data)
    assert report["ok"] is False and report["mismatch"] == 1


def _series_edit_fixture(tmp_path):
    listing = {"skus": [
        {"skuKey": "P-SKU-001", "styleAlias": "款式A", "sizeAlias": ""},
        {"skuKey": "P-SKU-002", "styleAlias": "款式B", "sizeAlias": ""},
    ], "images": [
        {"imageKey": "SKU-MAIN-001", "skuKey": "P-SKU-001", "role": "sku_main", "localPath": "SKU图/1.png"},
        {"imageKey": "SKU-MAIN-002", "skuKey": "P-SKU-002", "role": "sku_main", "localPath": "SKU图/2.png"},
        {"imageKey": "SPU-MAIN-01", "role": "spu_main", "localPath": "主图/1.png"},
    ]}
    uploaded = {"images": [
        {"imageKey": "SKU-MAIN-001", "role": "sku_main", "upload": {"imgUrl": "jfs/a1"}},
        {"imageKey": "SKU-MAIN-002", "role": "sku_main", "upload": {"imgUrl": "jfs/b1"}},
        {"imageKey": "SPU-MAIN-01", "role": "spu_main", "upload": {"imgUrl": "jfs/pool1"}},
        {"imageKey": "SPU-MAIN-02", "role": "spu_main", "upload": {"imgUrl": "jfs/shared2"}},
        {"imageKey": "STYLE-01-POS02", "role": "style_scene", "upload": {"imgUrl": "jfs/a2"}},
    ]}
    plan = {"styleImageMode": "series", "styleSceneMapping": [
        {"style": "款式A", "pos": 2, "imageKey": "STYLE-01-POS02"},
        {"style": "款式B", "pos": 2, "imageKey": "SPU-MAIN-02"},
    ], "deannotate": []}

    def sku(mapping_id, first_url):
        return {"componentCode": "SKUS-ITEM", "value": [
            {"componentCode": "VenderCategoryValue", "value": [
                {"componentCode": "card", "properties": [{"key": "mappingId", "value": mapping_id}]}]},
            {"componentCode": "SkuMainImage", "value": [
                {"componentCode": "img", "properties": [
                    {"key": "colorId", "value": mapping_id},
                    {"key": "imgUrl", "value": first_url},
                    {"key": "imgRefId", "value": mapping_id},
                    {"key": "orderSort", "value": "1"}]}]},
        ]}

    body = {"productApplyDTO": {}, "schema": json.dumps([
        {"componentCode": "WareSkuImg", "value": [
            {"componentCode": "img", "properties": [
                {"key": "colorId", "value": "0000000000"},
                {"key": "imgUrl", "value": "jfs/pool1"},
                {"key": "imgRefId", "value": "0000000000"},
                {"key": "orderSort", "value": "1"}]}]},
        {"componentCode": "SKUS", "value": [sku("uuid-a", "old-a"), sku("uuid-b", "old-b")]},
    ], ensure_ascii=False)}
    publish_dir = tmp_path / ".pipeline" / "publish"
    publish_dir.mkdir(parents=True)
    (publish_dir / "publish-body.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    apply_data = {"skuList": [
        {"skuId": "9001", "saleAttrs": [
            {"attrName": "颜色", "attrValueAlias": "款式A", "attrValues": "101A", "mappingId": ""}]},
        {"skuId": "9002", "saleAttrs": [
            {"attrName": "颜色", "attrValueAlias": "款式B", "attrValues": "101B", "mappingId": ""}]},
    ]}
    return tmp_path, listing, uploaded, publish_dir, plan, apply_data


def test_build_edit_body_writes_one_sequence_per_series_style(tmp_path):
    folder, listing, uploaded, publish_dir, plan, apply_data = _series_edit_fixture(tmp_path)
    body_path, groups = publish.build_edit_body(
        folder, listing, uploaded, publish_dir, plan, {}, apply_data, 123,
    )
    assert groups == 2
    schema = json.loads(json.loads((folder / ".pipeline" / "edit-body.json").read_text(encoding="utf-8"))["schema"])
    wsi = next(c for c in schema if c["componentCode"] == "WareSkuImg")
    entries = [i for i in wsi["value"] if str(publish.prop(i.get("properties"), "colorId")) != "0000000000"]
    grouped = {}
    for item in entries:
        color = str(publish.prop(item.get("properties"), "colorId"))
        grouped.setdefault(color, {})[int(publish.prop(item.get("properties"), "orderSort"))] = \
            str(publish.prop(item.get("properties"), "imgUrl"))
    assert grouped == {
        "uuid-a": {1: "jfs/a1", 2: "jfs/a2"},
        "uuid-b": {1: "jfs/b1", 2: "jfs/shared2"},
    }
    skus = next(c for c in schema if c["componentCode"] == "SKUS")["value"]
    first_urls = []
    for sku_comp in skus:
        smi = next(c for c in sku_comp["value"] if c["componentCode"] == "SkuMainImage")
        first_urls.append(str(publish.prop(smi["value"][0].get("properties"), "imgUrl")))
    assert first_urls == ["jfs/a1", "jfs/b1"]
