import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pdd_repaired_supplement as supplement


class SupplementTests(unittest.TestCase):
    def test_limited_batch_is_not_reported_complete(self):
        progress = {"requested": 24, "results": [{"ok": True}, {"ok": False}]}
        supplement.finalize_progress(progress)
        self.assertEqual(progress["pending"], 22)
        self.assertEqual(progress["stage"], "PARTIAL")

    def test_complete_batch_can_contain_skipped_failures(self):
        progress = {"requested": 2, "results": [{"ok": True}, {"ok": False}]}
        supplement.finalize_progress(progress)
        self.assertEqual(progress["pending"], 0)
        self.assertEqual(progress["stage"], "COMPLETED_WITH_FAILURES")

    def test_content_replacements_must_belong_to_same_product(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "replacement.jpg"
            image.write_bytes(b"verified replacement")
            review = root / "review.json"
            supplement.mover.write_json(review, {"sourceSku": "other", "decision": "PASS", "replacements": [{
                "role": "详情图", "sourceSha256": "original", "replacementPath": str(image),
                "replacementSha256": supplement.file_hash(image)}]})
            self.assertEqual(supplement.verified_replacements([review], "requested"), {})
            self.assertIn("original", supplement.verified_replacements([review], "other"))

    def test_requires_all_supplied_codes(self):
        with self.assertRaisesRegex(ValueError, "商家SKU为空"):
            supplement.require_codes([{"vendorSku": ""}])

    def test_rejects_duplicate_codes_without_renumbering(self):
        rows = [{"vendorSku": "GZH778A"}, {"vendorSku": "GZH778A"}]
        with self.assertRaisesRegex(ValueError, "商家SKU重复"):
            supplement.require_codes(rows)
        self.assertEqual(rows[1]["vendorSku"], "GZH778A")

    def test_exact_twice_price_and_preserved_code(self):
        listing = {"source": {"pricing": {}}, "skus": [{"sourceSku": "source-SKU001", "vendorSku": "GZH778A"}]}
        source = {"variants": [{"sourceSku": "source-SKU001", "vendorSku": "GZH778A", "sourcePrice": "39.53"}]}
        audit = supplement.set_exact_prices(listing, source)
        self.assertEqual(str(audit[0]["targetArrival"]), "79.06")
        self.assertEqual(str(audit[0]["formulaArrival"]), "79.06")
        self.assertEqual(listing["skus"][0]["vendorSku"], "GZH778A")

    def test_rejects_code_changes(self):
        listing = {"source": {}, "skus": [{"sourceSku": "source-SKU001", "vendorSku": "changed"}]}
        source = {"variants": [{"sourceSku": "source-SKU001", "vendorSku": "original", "sourcePrice": "10"}]}
        with self.assertRaisesRegex(ValueError, "商家SKU发生变化"):
            supplement.set_exact_prices(listing, source)

    def test_ambiguous_state_never_resubmits(self):
        with self.assertRaisesRegex(ValueError, "不重复提交"):
            supplement.require_resumable_state({"stage": "SUBMITTING"})
        supplement.require_resumable_state({"stage": "PUBLISHED", "productId": 123})

    def test_new_payload_has_no_platform_ids(self):
        with self.assertRaises(ValueError):
            supplement.require_new_payload({"productApplyDTO": {"productId": 123}, "schema": "[]"})
        with self.assertRaises(ValueError):
            supplement.require_new_payload({"productApplyDTO": {}, "schema": '[{"componentCode":"SkuId"}]'})


if __name__ == "__main__":
    unittest.main()
