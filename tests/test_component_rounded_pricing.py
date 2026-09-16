from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sys
import unittest

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from component_rounded_pricing import calculate_final_price, create_price_plan


class ComponentRoundedPricingTests(unittest.TestCase):
    def test_explicit_rounding_mode_is_routed(self):
        self.assertIn("references/component-rounded-pricing.md", (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue((SKILL_ROOT / "references/component-rounded-pricing.md").is_file())

    def test_combined_rounding_does_not_prove_exact_arrival(self):
        target = Decimal("39.53") * 2
        naive = (target / Decimal(".7225")).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        calculated = calculate_final_price(naive, Decimal(".15"), Decimal(".15"))
        self.assertEqual(naive, Decimal("109.43"))
        self.assertEqual(calculated.final_price, Decimal("79.07"))
        self.assertNotEqual(calculated.final_price, target)

    def test_existing_inverter_exactly_reaches_target(self):
        plan = create_price_plan(Decimal("79.06"), Decimal(".15"), Decimal(".15"), target_gap=Decimal("0"))
        self.assertEqual(plan.jd_price, Decimal("109.42"))
        self.assertEqual(plan.final_price, Decimal("79.06"))

    def test_independent_discount_amounts(self):
        result = calculate_final_price(Decimal("109.42"), Decimal(".15"), Decimal(".15"))
        self.assertEqual(result.direct_reduction, Decimal("16.41"))
        self.assertEqual(result.government_subsidy, Decimal("13.95"))
        self.assertEqual(result.final_price, Decimal("79.06"))


if __name__ == "__main__":
    unittest.main()
