import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jdo_merchant_code_repair as repair


class CodeRepairTests(unittest.TestCase):
    def test_matches_specs_not_changed_row_order(self):
        variants = [{"sourceSku": "old-001", "sourceAttributes": {"款式": "甲", "套餐": "小"}},
                    {"sourceSku": "old-002", "sourceAttributes": {"款式": "乙", "套餐": "大"}}]
        rows = [{"style": "乙", "package": "大", "vendorSku": "B001", "rowNumber": 2},
                {"style": "甲", "package": "小", "vendorSku": "A001", "rowNumber": 3}]
        matched = repair.match_workbook(variants, rows)
        self.assertEqual(matched["old-001"]["vendorSku"], "A001")

    def test_blank_code_is_not_generated(self):
        with self.assertRaises(ValueError):
            repair.match_workbook([{ "sourceSku": "one", "sourceAttributes": {"款式": "甲"}}],
                                  [{"style": "甲", "package": "", "vendorSku": "", "rowNumber": 2}])

    def test_noncode_fingerprint_ignores_only_merchant_code(self):
        detail = {"productInfo": {"productId": 1}, "skuList": [{"skuId": 2, "outerId": "old", "priceInfo": {"jdPrice": 9}}]}
        changed = {"productInfo": {"productId": 1}, "skuList": [{"skuId": 2, "outerId": "new", "priceInfo": {"jdPrice": 9}}]}
        self.assertEqual(repair.noncode_signature(detail), repair.noncode_signature(changed))
        changed["skuList"][0]["priceInfo"]["jdPrice"] = 10
        self.assertNotEqual(repair.noncode_signature(detail), repair.noncode_signature(changed))

    def test_payload_changes_only_codes(self):
        payload = repair.make_body(1, {"lastCategoryId": 35381}, [{"skuId": 2, "oldCode": "OLD", "merchantCode": "GZH906A"}])
        repair.validate_body(payload, 1, [{"skuId": 2, "oldCode": "OLD", "merchantCode": "GZH906A"}])
        payload["patchOperationList"][0]["value"] = '{"componentCode":"SkuJdprice","value":"10"}'
        with self.assertRaises(ValueError):
            repair.validate_body(payload, 1, [{"skuId": 2, "oldCode": "OLD", "merchantCode": "GZH906A"}])

    def test_duplicate_specs_are_blocked(self):
        rows = [{"style": "甲", "package": "", "vendorSku": "A", "rowNumber": 2},
                {"style": "甲", "package": "", "vendorSku": "B", "rowNumber": 3}]
        with self.assertRaises(ValueError):
            repair.match_workbook([], rows)

    def test_full_edit_keeps_ids_and_drops_stock(self):
        detail = {"productInfo": {"productId": 1, "categoryDetail": {"lastCategoryId": 35381},
                                  "productName": "title", "brandInfo": {"brandId": -1}, "itemNum": "item",
                                  "features": [{"key": "is7ToReturn", "value": "1"}],
                                  "logisticsInfo": {"transportId": 10}, "productDetailDesc": {"desc": "html"}},
                  "material": {"mainImages": []},
                  "skuList": [{"skuId": 2, "outerId": "OLD", "priceInfo": {"jdPrice": 5},
                               "saleAttrs": [], "skuEnableStatus": 1}]}
        template = {"schema": '[{"componentCode":"SKUS","value":[]}]'}
        rows = [{"skuId": 2, "oldCode": "OLD", "merchantCode": "NEW"}]
        body = repair.make_full_edit(template, detail, rows)
        repair.validate_full_edit(body, template, detail, rows)
        import json
        components = json.loads(body["schema"])[0]["value"][0]["value"]
        values = {item["componentCode"]: item.get("value") for item in components}
        self.assertEqual(values["SkuId"], 2)
        self.assertEqual(values["SkuVendorSku"], "NEW")
        self.assertNotIn("SkuStockNum", values)

    def test_duplicate_codes_blocked(self):
        with self.assertRaises(ValueError):
            repair.match_workbook([], [{"style": "甲", "package": "", "vendorSku": "A", "rowNumber": 2},
                                       {"style": "乙", "package": "", "vendorSku": "A", "rowNumber": 3}])

    def test_plan_change_invalidates_confirmation(self):
        plan = {"codes": ["A"]}
        token = repair.digest_json(plan)
        plan["codes"] = ["B"]
        with self.assertRaises(ValueError):
            repair.require_confirmation(plan, token, "REPAIR_MERCHANT_CODES")

    def test_edit_uses_sales_attribute_order_not_detail_order(self):
        skus = [{"skuId": 3, "saleAttrs": [{"indexId": 1, "attrValuesSeqNo": 3}]},
                {"skuId": 1, "saleAttrs": [{"indexId": 1, "attrValuesSeqNo": 1}]},
                {"skuId": 2, "saleAttrs": [{"indexId": 1, "attrValuesSeqNo": 2}]}]
        self.assertEqual([row["skuId"] for row in repair.ordered_skus(skus)], [1, 2, 3])

    def test_partial_code_repair_preserves_unmapped_sku_code_and_ids(self):
        import json
        detail = {"productInfo": {"productId": 1, "categoryDetail": {"lastCategoryId": 35381},
                                  "productName": "title", "brandInfo": {"brandId": -1}, "itemNum": "item",
                                  "features": [], "logisticsInfo": {"transportId": 10},
                                  "productDetailDesc": {"desc": "html"}},
                  "material": {"mainImages": []},
                  "skuList": [{"skuId": sku_id, "outerId": code, "priceInfo": {"jdPrice": 5},
                               "saleAttrs": [], "skuEnableStatus": 1}
                              for sku_id, code in ((2, "OLD"), (3, "KEEP_UNCONFIRMED"))]}
        template = {"schema": '[{"componentCode":"SKUS","value":[]}]'}
        body = repair.make_full_edit(template, detail, [{"skuId": 2, "merchantCode": "MERCHANT"}])
        skus = json.loads(body["schema"])[0]["value"]
        values = [{item["componentCode"]: item.get("value") for item in sku["value"]} for sku in skus]
        self.assertEqual({item["SkuId"]: item["SkuVendorSku"] for item in values},
                         {2: "MERCHANT", 3: "KEEP_UNCONFIRMED"})
        self.assertTrue(all("SkuStockNum" not in item for item in values))


if __name__ == "__main__":
    unittest.main()
