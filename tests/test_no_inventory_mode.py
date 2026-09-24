# -*- coding: utf-8 -*-
"""No-inventory mode tests: code generation/reuse, pricing, compliance, person removal."""
import importlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "ni"))

from source_pricing import component_source_audit, component_source_plan


NO_INVENTORY_PRICING = {
    "targetArrivalMultiplier": "1.35",
    "belowCompetitor": "0",
    "nationalSubsidyRate": "0",
    "activityDiscountRate": "0.15",
    "coupon": "0",
    "roundingMode": "component",
}


def make_batch_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="ni-batch-"))
    (root / "source").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "images" / "raw").mkdir(parents=True)
    config = {
        "batchId": "test-batch",
        "profile": "test-profile",
        "venderId": 123,
        "shopName": "测试店铺",
        "freightTemplateId": 1,
        "codePrefix": "TST",
        "stock": 200,
        "sevenDayReturn": "1",
        "personHandling": "remove",
        "imageQualityGate": "legacy",  # existing fixture; new batches default strict
        "pricing": NO_INVENTORY_PRICING,
    }
    (root / "batch-config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def make_record(root: Path, item_id: str, row: int) -> dict:
    raw = root / "images" / "raw" / item_id
    raw.mkdir(parents=True, exist_ok=True)
    for name in ("main-01.jpg", "main-02.jpg", "sku-01.jpg", "detail-01.jpg", "detail-02.jpg"):
        (raw / name).write_bytes(b"x" * 300)
    return {
        "_row": row,
        "商品ID": item_id,
        "商品名称": "进口 官方 精油香薰 天然 好物",
        "商品销售链接": f"https://item.example/{item_id}",
        "商品属性": json.dumps({"品牌": "示例品牌"}, ensure_ascii=False),
        "SKU属性": "50ml 示例品牌",
        "SKU价格": "10.00",
        "SKU库存": "5",
        "SKU属性图": f"http://img.example/{item_id}/sku1.jpg",
        "主图1": f"http://img.example/{item_id}/m1.jpg",
        "主图2": f"http://img.example/{item_id}/m2.jpg",
        "商品描述图": "<p><img src='http://img.example/d1.jpg'/></p>"
                     "<p><img src='http://img.example/d2.jpg'/></p>",
        "商品视频": f"http://img.example/{item_id}/v.mp4",
    }


class NoInventoryBatchFixture(unittest.TestCase):
    """Builds a phase-1-only batch root and runs build_candidates twice."""

    @classmethod
    def setUpClass(cls):
        cls.root = make_batch_root()
        records = [make_record(cls.root, "ITEM1", 2), make_record(cls.root, "ITEM2", 3)]
        (cls.root / "source" / "workbook-1-dump.json").write_text(
            json.dumps({"sourceFile": "1.xlsx", "sha256": "0" * 64, "records": records},
                       ensure_ascii=False), encoding="utf-8")
        mapping = [
            {"row": 2, "status": "MAPPED", "categoryId": 100, "categoryName": "测试类目",
             "keyword": "测试关键词"},
            {"row": 3, "status": "MAPPED", "categoryId": 100, "categoryName": "测试类目",
             "keyword": "测试关键词"},
        ]
        (cls.root / "reports" / "category-mapping.json").write_text(
            json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        cls.old_env = os.environ.get("NO_INVENTORY_ROOT")
        os.environ["NO_INVENTORY_ROOT"] = str(cls.root)
        cls.old_argv = list(sys.argv)
        sys.argv = [sys.argv[0]]
        import build_candidates
        cls.build = importlib.reload(build_candidates)
        for _ in range(2):
            with redirect_stdout(io.StringIO()):
                cls.build.main()

    @classmethod
    def tearDownClass(cls):
        if cls.old_env is None:
            os.environ.pop("NO_INVENTORY_ROOT", None)
        else:
            os.environ["NO_INVENTORY_ROOT"] = cls.old_env
        sys.argv = cls.old_argv

    def candidate(self, item_id: str) -> dict:
        return json.loads(
            (self.root / "candidates" / item_id / "source-normalized.json").read_text(encoding="utf-8"))

    def test_codes_are_random_format_and_reused(self):
        c1, c2 = self.candidate("ITEM1"), self.candidate("ITEM2")
        self.assertRegex(c1["itemNumber"], r"^TST[A-Z0-9]{9}$")
        self.assertRegex(c2["itemNumber"], r"^TST[A-Z0-9]{9}$")
        self.assertNotEqual(c1["itemNumber"], c2["itemNumber"])
        self.assertNotIn("ITEM1", c1["itemNumber"])
        for candidate in (c1, c2):
            for sku in candidate["skus"]:
                self.assertNotEqual(sku["vendorSku"], candidate["itemNumber"])
                self.assertTrue(sku["vendorSku"])
        ledger = json.loads((self.root / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["codes"][f"spu:ITEM1"], c1["itemNumber"])
        self.assertEqual(ledger["codes"][f"spu:ITEM2"], c2["itemNumber"])

    def test_title_keyword_prefix_and_forbidden_terms_removed(self):
        for item_id in ("ITEM1", "ITEM2"):
            candidate = self.candidate(item_id)
            title = candidate["title"]
            self.assertTrue(title.startswith("测试关键词"))
            for term in ("进口", "官方", "日本", "德国", "淘宝", "天猫", "旗舰",
                         "正品", "立减", "厂家直销", "国补", "最低", "第一"):
                self.assertNotIn(term, title)
            for sku in candidate["skus"]:
                self.assertNotIn("示例品牌", sku["saleAlias"])
                self.assertRegex(sku["saleAlias"], r"^[\u4e00-\u9fffa-zA-Z0-9\s]+$")

    def test_default_fields(self):
        candidate = self.candidate("ITEM1")
        self.assertEqual(candidate["brandId"], -1)
        self.assertEqual(candidate["stock"], 200)
        self.assertEqual(candidate["sevenDayReturnCode"], "1")
        self.assertEqual(candidate["pricing"], NO_INVENTORY_PRICING)


class ComponentSourcePlanTests(unittest.TestCase):
    def test_no_inventory_config_is_exact_inversion(self):
        plan = component_source_plan("10", NO_INVENTORY_PRICING)
        self.assertEqual(str(plan.target_final_price), "13.50")
        audit = component_source_audit("10", str(plan.jd_price), NO_INVENTORY_PRICING)
        self.assertTrue(audit["ok"], audit)
        self.assertEqual(audit["expectedArrivalPrice"], "13.50")

    def test_below_competitor_zero_means_no_hidden_deduction(self):
        plan = component_source_plan("39.53", NO_INVENTORY_PRICING)
        audit = component_source_audit("39.53", str(plan.jd_price), NO_INVENTORY_PRICING)
        self.assertTrue(audit["ok"], audit)
        self.assertEqual(audit["expectedArrivalPrice"], str(plan.target_final_price))


class ComplianceGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_env = os.environ.get("NO_INVENTORY_ROOT")
        root = make_batch_root()
        os.environ["NO_INVENTORY_ROOT"] = str(root)
        cls.old_argv = list(sys.argv)
        sys.argv = [sys.argv[0]]
        import compliance_gate
        import listing_batch
        cls.gate = compliance_gate
        cls.listing = listing_batch

    @classmethod
    def tearDownClass(cls):
        if cls.old_env is None:
            os.environ.pop("NO_INVENTORY_ROOT", None)
        else:
            os.environ["NO_INVENTORY_ROOT"] = cls.old_env
        sys.argv = cls.old_argv

    def test_must_remove_terms_hit(self):
        hits = self.gate.scan_line("淘宝日本官方立减厂家直销国补", self.gate.MUST_REMOVE)
        categories = {category for category, _ in hits}
        self.assertIn("PLATFORM", categories)
        self.assertIn("GEO", categories)
        self.assertIn("PROMO", categories)

    def test_clean_text_has_no_hits(self):
        self.assertEqual(self.gate.scan_line("天然植物精油 无火散香", self.gate.MUST_REMOVE), [])

    def test_listing_clean_alias_strips_terms_and_symbols(self):
        cleaned = self.listing.clean_alias("官方正品日本进口精油【包邮】!!!")
        for term in ("官方", "正品", "日本", "进口", "包邮", "!", "【", "】"):
            self.assertNotIn(term, cleaned)
        self.assertRegex(cleaned, r"^[\u4e00-\u9fffa-zA-Z0-9\s]+$")


class PersonRemovalPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_env = os.environ.get("NO_INVENTORY_ROOT")
        root = make_batch_root()
        os.environ["NO_INVENTORY_ROOT"] = str(root)
        cls.old_argv = list(sys.argv)
        sys.argv = [sys.argv[0]]
        import oxygen_pool
        import oxygen_verify
        import sufy_fallback
        cls.pool = oxygen_pool
        cls.verify = oxygen_verify
        cls.sufy = sufy_fallback

    @classmethod
    def tearDownClass(cls):
        if cls.old_env is None:
            os.environ.pop("NO_INVENTORY_ROOT", None)
        else:
            os.environ["NO_INVENTORY_ROOT"] = cls.old_env
        sys.argv = cls.old_argv

    def test_sufy_prompt_uses_removal_not_replacement(self):
        prompt = self.sufy.build_prompt(["person"], False)
        self.assertIn("整体移除", prompt)
        self.assertIn("自然填补", prompt)
        self.assertNotIn("替换", prompt)

    def test_oxygen_person_prompt_uses_removal(self):
        prompt = self.pool.build_person_prompt(["person", "brand"], False)
        self.assertIn("整体移除", prompt)
        self.assertIn("自然填补", prompt)
        self.assertNotIn("替换", prompt)
        self.assertIn("品牌", prompt)  # other violations still cleared

    def test_verification_default_is_task_aware_removal(self):
        prompt = self.verify.build_user_prompt(["person"], False)
        self.assertIn("移除人物", prompt)
        self.assertIn("已消失是预期结果", prompt)



class TwoTierVisionCascadeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_env = os.environ.get("NO_INVENTORY_ROOT")
        root = make_batch_root()
        os.environ["NO_INVENTORY_ROOT"] = str(root)
        cls.old_argv = list(sys.argv)
        sys.argv = [sys.argv[0]]
        import oxygen_verify
        import vision_audit
        cls.verify = oxygen_verify
        cls.audit = vision_audit

    @classmethod
    def tearDownClass(cls):
        if cls.old_env is None:
            os.environ.pop("NO_INVENTORY_ROOT", None)
        else:
            os.environ["NO_INVENTORY_ROOT"] = cls.old_env
        sys.argv = cls.old_argv

    def test_main_count_reduction_refused_even_with_allow_count_change(self):
        from unittest import mock
        import update_main_images as updater
        candidate = {"saleComponents": {"style": {"componentCode": "sale-prop-1"}}, "skus": []}
        detail = {"material": {"mainImages": [{"uuid": "0000000000", "imageInfoList": [
            {"orderSort": 1, "imgUrl": "a"}, {"orderSort": 2, "imgUrl": "bad"}]}]}}
        with mock.patch.object(updater.cli, "online_style_color_ids", return_value={}):
            changed, _unchanged, errors, _expected = updater.compare_groups(
                candidate, detail, {"0000000000": ["a"]}, allow_count_change=True)
        self.assertEqual(changed, [])
        self.assertTrue(any("delete-unsupported" in message for message in errors))

    def test_defaults_are_lite_first_then_flash(self):
        self.assertEqual(self.verify.PRIMARY_MODEL, "gemini-2.5-flash-lite")
        self.assertEqual(self.verify.REVIEW_MODEL, "gemini-2.5-flash")
        self.assertEqual(self.audit.PRIMARY_MODEL, "gemini-2.5-flash-lite")
        self.assertEqual(self.audit.REVIEW_MODEL, "gemini-2.5-flash")

    def test_verdict_ok_requires_all_four_checks(self):
        good = {"productSame": True, "violationsCleared": True,
                "noNewArtifacts": True, "qualityOk": True}
        self.assertTrue(self.verify.verdict_ok(good))
        bad = dict(good); bad["violationsCleared"] = False
        self.assertFalse(self.verify.verdict_ok(bad))
        self.assertFalse(self.verify.verdict_ok({"parseFail": True, "raw": "noise"}))
        self.assertFalse(self.verify.verdict_ok({}))

    def test_audit_reviews_only_flagged_or_unclear(self):
        self.assertFalse(self.audit.needs_review({"ok": True, "parsed": {"violations": [], "flags": [], "legible": True, "layoutOk": True, "valuable": True}}))
        self.assertTrue(self.audit.needs_review({"ok": True, "parsed": {"violations": []}}))
        self.assertTrue(self.audit.needs_review(
            {"ok": True, "parsed": {"violations": [{"term": "TOP"}]}}))
        self.assertTrue(self.audit.needs_review({"ok": True, "parsed": None}))
        self.assertTrue(self.audit.needs_review({"ok": False, "error": "HTTP 500"}))


if __name__ == "__main__":
    unittest.main()
