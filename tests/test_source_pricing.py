from decimal import Decimal
from pathlib import Path
from argparse import Namespace
from contextlib import redirect_stdout
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from jd_product_mover import target_price
from jdo_listing_mvp import validate_manifest
from taobao_product_mvp import pricing_audit
import jdo_listing_mvp as jingmai


class SourcePricingTests(unittest.TestCase):
    def setUp(self):
        self.pricing = {
            "targetArrivalMultiplier": "1.35", "belowCompetitor": "0",
            "activityDiscountRate": "0.15", "nationalSubsidyRate": "0.15",
            "coupon": "0", "roundingMode": "component",
        }

    def listing(self, jd_price):
        return {
            "category": {"id": 123},
            "source": {"pricing": self.pricing},
            "product": {},
            "skus": [{"sourceSku": "source-1", "sourcePrice": "39.53", "jdPrice": jd_price}],
        }

    def test_multiplier_rounds_target_before_exact_inversion(self):
        self.assertEqual(target_price("39.53", self.pricing), Decimal("73.87"))

    def test_correct_price_passes_component_audit(self):
        audit = pricing_audit(self.listing("73.87"))
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["rows"][0]["arrivalPrice"], "53.37")

    def test_fractional_cent_source_target_does_not_accept_simple_division(self):
        audit = pricing_audit(self.listing("73.86"))
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["rows"][0]["arrivalPrice"], "53.36")

    def test_component_audit_does_not_use_combined_rounding(self):
        self.pricing["targetArrivalMultiplier"] = "2"
        audit = pricing_audit(self.listing("109.43"))
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["rows"][0]["arrivalPrice"], "79.07")

    def test_preflight_uses_the_same_explicit_component_policy(self):
        self.pricing["targetArrivalMultiplier"] = "2"
        reference = {"category": {"id": 123}, "brands": [], "componentCatalog": []}
        errors, _ = validate_manifest(Path("listing.json"), self.listing("109.42"), reference)
        self.assertFalse(any("价计算不一致" in error or "到手价不等于" in error for error in errors))
        errors, _ = validate_manifest(Path("listing.json"), self.listing("109.43"), reference)
        self.assertTrue(any("价计算不一致" in error or "到手价不等于" in error for error in errors))

    def test_exact_inversion_matches_independent_integer_arithmetic(self):
        for source_cents in (1, 7, 99, 264, 599, 999, 1499, 1999, 3953, 5999, 10000):
            with self.subTest(source_cents=source_cents):
                jd_cents = int(target_price(Decimal(source_cents) / 100, self.pricing) * 100)
                direct_cents = (jd_cents * 15 + 50) // 100
                subsidy_cents = (jd_cents * 85 * 15 + 5000) // 10000
                target_cents = (source_cents * 135 + 50) // 100
                self.assertEqual(jd_cents - direct_cents - subsidy_cents, target_cents)

    def test_component_policy_rejects_unsupported_coupon(self):
        self.pricing["coupon"] = "1"
        with self.assertRaises(ValueError):
            target_price("39.53", self.pricing)

    def test_invalid_mode_does_not_silently_fall_back(self):
        self.pricing["roundingMode"] = "componnent"
        with self.assertRaises(ValueError):
            target_price("39.53", self.pricing)

    def test_invalid_source_multiplier_and_rates_are_rejected(self):
        for key, value in (("targetArrivalMultiplier", "0"), ("targetArrivalMultiplier", "NaN"),
                           ("activityDiscountRate", "-0.1"), ("nationalSubsidyRate", "1")):
            with self.subTest(key=key, value=value):
                pricing = {**self.pricing, key: value}
                with self.assertRaises(ValueError):
                    target_price("39.53", pricing)

    def live_audit(self, jd_price):
        self.pricing["targetArrivalMultiplier"] = "2"
        manifest = self.listing(jd_price)
        manifest["product"] = {
            "includeSizeSaleAttribute": False,
            "saleAttributeComponents": {"style": {"componentCode": "sale-prop-777"}},
        }
        manifest["skus"][0]["styleAlias"] = "测试规格"
        remote = {
            "productInfo": {"productStatus": {"productStatusNew": 103}},
            "skuList": [{"skuId": 101, "priceInfo": {"jdPrice": jd_price},
                         "skuStatus": {"onOffShelfStatus": 1}, "skuEnableStatus": 1,
                         "saleAttrs": [{"attrId": 777, "attrValueAlias": "测试规格"}]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audit.json"
            arguments = Namespace(input=Path("listing.json"), output=output, product_id=100)
            with patch.object(jingmai, "load_json", return_value=manifest), \
                 patch.object(jingmai, "get", return_value={"data": remote}), \
                 patch.object(jingmai, "query_sku_stocks", return_value=[{"stockNum": 10000}]), \
                 patch.object(jingmai, "shared_image_audit", return_value={"ok": True}), \
                 redirect_stdout(io.StringIO()):
                code = jingmai.command_verify_product(arguments)
            return code, json.loads(output.read_text(encoding="utf-8"))

    def test_independent_readback_calculates_arrival_from_remote_price(self):
        code, audit = self.live_audit("109.42")
        self.assertEqual(code, 0)
        self.assertEqual(audit["pricingFormulaAudit"]["status"], "verified")
        self.assertEqual(audit["pricingFormulaAudit"]["rows"][0]["arrivalPrice"], "79.06")

    def test_remote_price_matching_bad_plan_cannot_claim_formula_verified(self):
        code, audit = self.live_audit("109.43")
        self.assertEqual(code, 2)
        self.assertEqual(audit["priceMismatchCount"], 0)
        self.assertEqual(audit["pricingFormulaAudit"]["status"], "failed")
        self.assertEqual(audit["pricingFormulaAudit"]["rows"][0]["arrivalPrice"], "79.07")


if __name__ == "__main__":
    unittest.main()
