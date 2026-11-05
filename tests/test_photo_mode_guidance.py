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

if __name__ == '__main__':
    unittest.main()
