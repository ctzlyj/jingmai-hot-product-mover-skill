import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class PhotoModeGuidanceTests(unittest.TestCase):
    def test_photo_mode_has_explicit_three_company_gpt_whitelist(self):
        path = ROOT / 'references' / 'photo-new-listing.md'
        self.assertTrue(path.is_file(), 'Photo-mode model restriction has not been documented')
        text = path.read_text(encoding='utf-8')
        for model in ('GPT-Image-2.5-Flare-joybuilder', 'GPT-Image-2.5-Sunburst-joybuilder', 'GPT-image-2-joybuilder'):
            self.assertIn(model, text)
        self.assertIn('禁止调用白名单以外', text)
        self.assertIn('不得直接沿用 SDK 默认降级链', text)

    def test_photo_mode_parallel_rule_is_scoped_and_status_truthful(self):
        text = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('实拍图上新', text)
        self.assertIn('三款独立并发', text)
        self.assertIn('尚待真实商品验收', text)
        self.assertIn('不改变旧矩阵模式', text)

class DetailPageGuidanceTests(unittest.TestCase):
    def test_skill_and_references_state_the_detail_gate_without_asking_the_user(self):
        skill = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('version: 1.2.3', skill)
        for word in ('detail-plan.json', '6–8', '覆盖全部 SKU', 'deliveryChecks', 'detail_repair.py', '不需要提醒'):
            self.assertIn(word, skill)
        doc = (ROOT / 'references' / 'detail-pages.md').read_text(encoding='utf-8')
        for word in ('DETAIL_PATCH', 'WareEditor', 'mobileDesc', 'platformValidationPassed', 'detailQualityPassed', 'videoBound', 'image-studio'):
            self.assertIn(word, doc)

    def test_detail_repair_never_names_price_stock_sku_or_video_writers(self):
        text = (ROOT / 'scripts' / 'detail_repair.py').read_text(encoding='utf-8')
        for forbidden in ('photo_video', 'sku-materials', 'WareSkuImg', 'SkuJdprice', 'SkuStockNum'):
            self.assertNotIn(forbidden, text)

    def test_detail_modules_do_not_call_non_company_image_models(self):
        text = (ROOT / 'scripts' / 'detail_pages.py').read_text(encoding='utf-8')
        for forbidden in ('Oxygen', 'sufy', 'requests'):
            self.assertNotIn(forbidden, text)


class StructuredAttributeGuidanceTests(unittest.TestCase):
    def test_docs_state_prevalidation_is_not_proof_and_the_discovery_order(self):
        skill = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('平台预校验通过不代表其别名正确', skill); self.assertIn('discover-sale-templates', skill)
        doc = (ROOT / 'references' / 'structured-sale-attributes.md').read_text(encoding='utf-8')
        for word in ('平台预校验通过不代表属性正确', 'sale-attr-templates', '同店、同末级类目', 'templateSkuProps', 'confirm-not-created', 'REJECTED_CONFIRMED', '103'):
            self.assertIn(word, doc)
        self.assertTrue((ROOT / 'CHANGELOG.md').read_text(encoding='utf-8').startswith('# Changelog\n\n## 1.2.3'))


if __name__ == '__main__':
    unittest.main()
