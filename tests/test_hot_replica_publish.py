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
