import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import taobao_product_mvp as taobao
import jd_product_mover as mover
from test_browser_source_read import taobao_response


class GenericTaobaoCaptureTests(unittest.TestCase):
    def test_repeated_detail_images_preserve_source_order_and_count(self):
        urls = ["//img.example/first.jpg", "https://img.example/second.jpg", "//img.example/first.jpg"]
        payload = {"images": [{"attrs": {"data-name": "singleImage", "data-src": url}} for url in urls]}
        self.assertEqual(taobao.detail_image_urls(payload), ["https://img.example/first.jpg", "https://img.example/second.jpg", "https://img.example/first.jpg"])

    def response(self):
        response = taobao_response()
        response["item"]["pcADescUrl"] = "https://market.m.taobao.com/app/detail-project/desc/index.html?id=123&descVersion=7"
        response["skuBase"]["props"] = [{"pid": "color", "name": "颜色", "values": [
            {"vid": "one", "name": "蓝色小号 500ml", "image": "//img.example/blue.jpg"},
            {"vid": "two", "name": "蓝色大号 1000ml"}]}]
        response["skuBase"]["skus"] = [{"skuId": "sku-1", "propPath": "color:one", "outerId": "original-code"}]
        return response

    def description(self):
        return {"ok": True, "pageUrl": self.response()["item"]["pcADescUrl"],
                "images": [{"attrs": {"data-name": "singleImage", "data-src": "https://img.example/detail.jpg"}}]}

    def collect(self, response=None, description=None, own_images=False):
        fixture = {"response": response or self.response(), "description": description, "ownImages": own_images}
        program = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const sandbox = {URL, location:{href:'https://detail.tmall.com/item.htm?id=123'},
  window:{__ICE_APP_CONTEXT__:{loaderData:{home:{data:{res:fixture.response}}}}},
  document:{querySelectorAll:()=>fixture.ownImages ? [{getAttribute: name => name === 'src' ? 'https://img.example/detail.jpg' : null}] : []}};
const collector = vm.runInNewContext('(' + fs.readFileSync(process.argv[1], 'utf8') + ')', sandbox);
process.stdout.write(JSON.stringify(collector(fixture.description)));
"""
        result = subprocess.run(["node", "-e", program, str(ROOT / "scripts/browser_collectors/taobao_capture.js")],
                                input=json.dumps(fixture), text=True, encoding="utf-8", capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_missing_inline_description_requests_verified_link(self):
        result = self.collect()
        self.assertEqual(result["reason"], "DESCRIPTION_PAGE_REQUIRED")
        self.assertEqual(result["descriptionUrl"], self.description()["pageUrl"])

    def test_linked_description_roundtrip_and_privacy(self):
        response = self.response()
        response["item"]["pcADescUrl"] += "&token=synthetic-private"
        response["skuCore"]["sku2info"]["sku-1"]["buyExParams"] = "synthetic-private"
        response["skuCore"]["sku2info"]["sku-1"]["quantity"] = 200
        result = self.collect(response, self.description())
        self.assertTrue(result["ok"])
        self.assertNotIn("synthetic-private", json.dumps(result))
        self.assertEqual(result["detailScope"], "linked-description-page")
        parsed, detail = taobao.load_browser_capture(result, result["pageUrl"])
        self.assertEqual(len(detail["images"]), 1)
        self.assertEqual(parsed["skuBase"]["skus"][0]["outerId"], "original-code")
        self.assertEqual(parsed["skuCore"]["sku2info"]["sku-1"]["quantity"], 200)

    def test_wrong_item_host_or_unlinked_description_fails(self):
        for url in [self.description()["pageUrl"].replace("id=123", "id=456"),
                    "https://unrelated.example/desc?id=123",
                    self.description()["pageUrl"].replace("descVersion=7", "descVersion=8")]:
            description = self.description()
            description["pageUrl"] = url
            self.assertFalse(self.collect(description=description)["ok"])

    def test_python_rechecks_description_provenance(self):
        result = self.collect(description=self.description())
        self.assertTrue(result["ok"])
        for field in ["detailPageUrl", "response"]:
            invalid = copy.deepcopy(result)
            if field == "response":
                invalid["response"]["item"]["pcADescUrl"] = self.description()["pageUrl"].replace("123", "456")
            else:
                invalid[field] = "https://unrelated.example/?id=123"
            with self.assertRaises(ValueError):
                taobao.load_browser_capture(invalid, result["pageUrl"])

    def test_current_document_still_supported(self):
        result = self.collect(own_images=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["detailScope"], "current-document")

    def test_one_dimension_preserves_full_label_and_original_code(self):
        source = taobao.build_source(self.response(), {"images": []}, "https://detail.tmall.com/item.htm?id=123")
        self.assertEqual(source["variants"][0]["sourceAttributes"], {"颜色": "蓝色小号 500ml"})
        self.assertEqual(source["variants"][0]["vendorSku"], "original-code")
        self.assertEqual(source["categoryIds"], [])
        self.assertEqual(source["sourceCategoryIds"], [])

    def test_fixed_properties_do_not_create_dimension_or_lose_image(self):
        response = self.response()
        response["skuBase"]["props"][0]["values"] = response["skuBase"]["props"][0]["values"][:1]
        source = taobao.build_source(response, {"images": []}, "https://detail.tmall.com/item.htm?id=123")
        self.assertEqual(source["saleProperties"], {})
        self.assertEqual(source["sourceFixedSkuProperties"], {"颜色": ["蓝色小号 500ml"]})
        self.assertEqual(source["variants"][0]["sourceImageUrl"], "https://img.example/blue.jpg")

    def test_missing_ambiguous_or_unknown_property_path_fails(self):
        for path in ["", "color:unknown", "color:one;color:two", "color:one;unknown:one"]:
            response = self.response()
            response["skuBase"]["skus"][0]["propPath"] = path
            with self.assertRaises(ValueError):
                taobao.build_source(response, {"images": []}, "https://detail.tmall.com/item.htm?id=123")

    def test_zero_price_fails(self):
        response = self.response()
        response["skuCore"]["sku2info"]["sku-1"]["price"]["priceMoney"] = "0"
        with self.assertRaises(ValueError):
            taobao.build_source(response, {"images": []}, "https://detail.tmall.com/item.htm?id=123")

    def test_category_review_is_item_bound_without_fake_source_category(self):
        source = taobao.build_source(self.response(), {"images": []}, "https://detail.tmall.com/item.htm?id=123")
        review = {"sourceItemId": "123", "targetCategoryId": 53164, "decision": "PROCEED", "reason": "Reviewed source facts"}
        mapped = taobao.apply_category_review(source, review, {"category": {"id": 53164}}, {"category": {"id": 53164}})
        self.assertEqual(mapped["sourceCategoryIds"], [])
        self.assertEqual(mapped["categoryIds"], [])
        self.assertEqual(mapped["categoryReview"], review)
        for key, value in [("sourceItemId", "999"), ("targetCategoryId", 35381), ("decision", "SKIP"), ("reason", "")]:
            invalid = {**review, key: value}
            with self.assertRaises(ValueError):
                taobao.apply_category_review(source, invalid, {"category": {"id": 53164}}, {"category": {"id": 53164}})

    def test_generic_rules_not_overwritten_with_decorative_attributes(self):
        rules = {"category": {"id": 53164}, "categoryAttributes": [{"componentCode": "real-material"}],
                 "skuAttributes": {"defaults": {"real-function": {"valueId": "1"}}}}
        self.assertEqual(taobao.taobao_rules(rules), rules)

    def test_reviewed_single_dimension_reaches_listing_with_supplied_package_facts(self):
        source = taobao.build_source(self.response(), {"images": []}, "https://detail.tmall.com/item.htm?id=123")
        source.update(mainImages=[], detailImages=[])
        source["variants"][0]["skuImage"] = "sku.png"
        rules = json.loads((ROOT / "config/modern-decorative-painting.rules.json").read_text(encoding="utf-8"))
        rules.update(category={"id": 53164, "name": "热水袋/暖手宝"}, categoryAttributes=[], skuAttributes={})
        rules["productDefaults"].update(packageLengthMm=210, packageWidthMm=90, packageHeightMm=20, weightKg="0.2")
        reference = {"category": {"id": 53164}, "componentCatalog": [], "media": {"defaultCategoryId": 1}}
        review = {"sourceItemId": "123", "targetCategoryId": 53164, "decision": "PROCEED", "reason": "Fixture review"}
        source = taobao.apply_category_review(source, review, rules, reference)
        listing = mover.build_listing(source, ROOT, rules, reference, 10000)
        self.assertEqual(listing["skus"][0]["styleAlias"], "蓝色小号 500ml")
        self.assertEqual(listing["skus"][0]["vendorSku"], "original-code")
        self.assertEqual(listing["product"]["weightKg"], "0.2")
        self.assertEqual(listing["product"]["packageLengthMm"], 210)
        self.assertFalse(listing["product"]["includeSizeSaleAttribute"])
        source["sourceParentSku"] = "123"
        source["sourceSku"] = "123-P01"
        mover.build_listing(source, ROOT, rules, reference, 10000)
        source["categoryReview"]["sourceItemId"] = "999"
        with self.assertRaises(mover.MoverError):
            mover.build_listing(source, ROOT, rules, reference, 10000)


if __name__ == "__main__":
    unittest.main()
