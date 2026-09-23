import copy
import sys
import unittest
import tempfile
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pdd_ab_image_publish as ab


class AbImagePublishTests(unittest.TestCase):
    def setUp(self):
        self.template = {"source": {"pricing": {"targetArrivalMultiplier": "2"}},
                         "product": {"title": "old", "itemNumber": "source", "includeSizeSaleAttribute": True},
                         "skus": [{"vendorSku": "GZH10000A", "jdPrice": 1, "styleAlias": "old", "sizeAlias": "old"}]}
        self.detail = {"productInfo": {"productId": 123, "productName": "current"},
                       "skuList": [{"skuId": 456, "outerId": "GZH10000A", "priceInfo": {"jdPrice": 19.37},
                                    "skuEnableStatus": 1, "saleAttrs": [
                                        {"attrId": "1001080021", "attrValueAlias": "style"},
                                        {"attrId": "1001080022", "attrValueAlias": "30×40cm"}]}]}

    def test_clone_preserves_live_price_codes_specs_without_mutating_a(self):
        before = copy.deepcopy(self.template)
        result = ab.clone_listing(self.template, self.detail)
        self.assertEqual(self.template, before)
        self.assertEqual(result["skus"][0]["vendorSku"], "GZH10000A")
        self.assertEqual(result["skus"][0]["jdPrice"], 19.37)
        self.assertEqual(result["skus"][0]["styleAlias"], "style")
        self.assertEqual(result["skus"][0]["sizeAlias"], "30×40cm")
        self.assertEqual(result["product"]["itemNumber"], "ABIMG-123")
        self.assertEqual(result["source"], {})
        self.assertNotIn("productId", result["product"])

    def test_missing_or_duplicate_live_merchant_codes_stop(self):
        for code in ("", "different"):
            self.detail["skuList"][0]["outerId"] = code
            with self.assertRaises(ValueError):
                ab.clone_listing(self.template, self.detail)
        self.detail["skuList"][0]["outerId"] = "GZH10000A"
        self.detail["skuList"].append(copy.deepcopy(self.detail["skuList"][0]))
        with self.assertRaises(ValueError):
            ab.clone_listing(self.template, self.detail)

    def test_current_workbook_overrides_stale_generated_template_code(self):
        self.template["skus"][0].update(sourceSku="source-001", styleAlias="style", sizeAlias="30×40cm")
        self.detail["skuList"][0]["outerId"] = "MERCHANT-007"
        matched = {"source-001": {"vendorSku": "MERCHANT-007"}}
        resolved = ab.template_with_current_codes(self.template, self.detail, matched)
        self.assertEqual(resolved["skus"][0]["vendorSku"], "MERCHANT-007")
        self.assertEqual(self.template["skus"][0]["vendorSku"], "GZH10000A")

    def test_workbook_conflict_blocks_clone_instead_of_copying_generated_code(self):
        self.template["skus"][0].update(sourceSku="source-001", styleAlias="style", sizeAlias="30×40cm")
        with self.assertRaises(ValueError):
            ab.template_with_current_codes(self.template, self.detail, {"source-001": {"vendorSku": "MERCHANT-007"}})

    def test_workbook_code_cannot_match_another_specification(self):
        self.template["skus"][0].update(sourceSku="source-001", styleAlias="different", sizeAlias="30×40cm")
        with self.assertRaises(ValueError):
            ab.template_with_current_codes(self.template, self.detail, {"source-001": {"vendorSku": "GZH10000A"}})

    def test_prepare_without_current_source_root_fails_before_remote_access(self):
        with patch.object(ab.pipeline, "verify_store_identity") as verify:
            with self.assertRaisesRegex(ValueError, "current source-root"):
                ab.prepare(SimpleNamespace(source_root=None))
            verify.assert_not_called()

    def test_confirmation_binds_entire_plan(self):
        plan = {"originalProductId": 123, "price": 19.37}
        token = ab.digest_json(plan)
        ab.require_confirmation("PUBLISH_TO_JD", token, plan)
        with self.assertRaises(ValueError):
            ab.require_confirmation("", token, plan)
        with self.assertRaises(ValueError):
            ab.require_confirmation("PUBLISH_TO_JD", token, {**plan, "price": 20})

    def test_long_upload_name_is_bounded_without_renaming_file(self):
        import jdo_listing_mvp as jingmai
        name = "a" * 64 + ".jpg"
        safe = jingmai.safe_upload_image_name(name)
        self.assertLessEqual(jingmai.weighted_text_length(safe), 64)
        self.assertTrue(safe.endswith(".jpg"))
        self.assertEqual(safe, jingmai.safe_upload_image_name(name))
        self.assertEqual(jingmai.safe_upload_image_name("SKU001.jpg"), "SKU001.jpg")

    def test_original_fingerprint_detects_prices_specs_images_but_not_timestamps(self):
        changed = copy.deepcopy(self.detail)
        changed["skuList"][0]["modifiedTime"] = 42
        self.assertEqual(ab.original_fingerprint(self.detail), ab.original_fingerprint(changed))
        changed["skuList"][0]["priceInfo"]["jdPrice"] = 25
        self.assertNotEqual(ab.original_fingerprint(self.detail), ab.original_fingerprint(changed))

    def test_rejection_is_persisted_without_clearing_product_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            rejection = {"applyId": "789", "draftComputeStatus": 3}
            with patch.object(ab.mover, "read_json", side_effect=[{"originalProductId": 123}, {}, {}]), patch.object(ab.mover, "write_json"), patch.object(ab.jingmai, "command_verify_product", side_effect=ab.jingmai.JdoError("未查询到商品信息")), patch.object(ab.pipeline, "product_apply_rejection", return_value=rejection), patch.object(ab.pipeline, "save_state") as save:
                with self.assertRaisesRegex(ValueError, "preserved ID"):
                    ab.verify_pair(folder, 789, None)
                self.assertEqual(save.call_args.args[1], "PRODUCT_APPLY_REJECTED")
                self.assertEqual(save.call_args.kwargs["productId"], 789)
        changed = copy.deepcopy(self.detail)
        changed["material"] = {"mainImages": [{"uuid": "new"}]}
        self.assertNotEqual(ab.original_fingerprint(self.detail), ab.original_fingerprint(changed))


    def test_batch_continues_after_product_failure_and_preserves_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plans = [{"sourceSku": "one", "originalProductId": 1}, {"sourceSku": "two", "originalProductId": 2}]
            ab.mover.write_json(root / "prepared.json", {"pairs": plans})
            ab.mover.write_json(root / "pairs/one/state.json", {"stage": "PRODUCT_APPLY_REJECTED", "productId": 789})
            args = SimpleNamespace(confirm="PUBLISH_TO_JD", output=root, limit=0, skip_failures=True,
                                   store_config=root / "store.json", profile="test")
            ab.mover.write_json(args.store_config, {})
            ab.mover.write_json(root / "pairs/two/plan.json", {"store": {}, "originalProductId": 2})
            def submit(folder, args, *, defer_verification=False):
                self.assertTrue(defer_verification)
                ab.pipeline.save_state(folder / "state.json", "PUBLISHED", productId=790)
                return {"ok": False, "pending": True, "productId": 790}
            with patch.object(ab, "publish_pair", side_effect=submit) as publisher, patch.object(ab.pipeline, "verify_store_identity", return_value={}), patch.object(ab, "verify_pair", return_value={"ok": True, "productId": 790}):
                ab.run(args)
                publisher.assert_called_once()
            progress = ab.mover.read_json(root / "progress.json")
            self.assertEqual(progress["stage"], "COMPLETED_WITH_FAILURES")
            self.assertEqual(len(progress["results"]), 2)
            self.assertFalse(progress["results"][0]["ok"])
            self.assertTrue(progress["results"][1]["ok"])
            state = ab.mover.read_json(root / "pairs/one/state.json")
            self.assertEqual(state["productId"], 789)
            self.assertEqual(state["stage"], "PRODUCT_APPLY_REJECTED")


if __name__ == "__main__":
    unittest.main()
