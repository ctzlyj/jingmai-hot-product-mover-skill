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
        self.assertIn('version: 1.2.6', skill)
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
        log = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
        self.assertTrue(log.startswith('# Changelog\n\n## 1.2.6')); self.assertIn('## 1.2.5', log); self.assertIn('## 1.2.4', log); self.assertIn('## 1.2.3', log)


class VideoHandoffGuidanceTests(unittest.TestCase):
    def test_docs_describe_the_gate_listening_phrases_and_legacy_policy(self):
        doc = (ROOT / 'references' / 'video-binding-handoff.md').read_text(encoding='utf-8')
        for word in ('verify-handoff', 'manifest.video.localPath', '已试听，确认绑定', '接受口播风险，确认绑定', 'voiceReview', 'sha256', 'runId', '不得手工把当前文件的 SHA 写进 manifest', '--diagnose', '不扫描目录'):
            self.assertIn(word, doc)
        skill = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('verify-handoff', skill); self.assertIn('video-binding-handoff.md', skill)
        self.assertNotIn('--mp4 <本地.mp4> --confirm', (ROOT / 'references' / 'photo-new-listing.md').read_text(encoding='utf-8'))

    def test_the_gate_runs_before_any_platform_access_in_bind_video(self):
        text = (ROOT / 'scripts' / 'photo_video.py').read_text(encoding='utf-8')
        gate = text.index('handoff.check_handoff'); self.assertLess(gate, text.index('verify_store(m[')); self.assertLess(gate, text.index('probe_video(source)'))

    def test_handoff_module_has_no_media_or_platform_writers(self):
        import ast
        tree = ast.parse((ROOT / 'scripts' / 'video_handoff.py').read_text(encoding='utf-8'))
        imported = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        for forbidden in ('requests', 'jdo_listing_mvp', 'jd_product_pipeline', 'photo_listing', 'photo_video'):
            self.assertNotIn(forbidden, imported)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for forbidden in ('run_jdo_large', 'upload', 'post'):
            self.assertNotIn(forbidden, names)


class LargeRequestGuidanceTests(unittest.TestCase):
    def test_doc_records_the_feasibility_conclusions_and_the_credential_boundary(self):
        doc = (ROOT / 'references' / 'large-requests-and-cli-versions.md').read_text(encoding='utf-8')
        for word in ('方案甲', '方案乙', '不可行', '负责人先确认合规', '0.3.4', '0.3.7', '--body-file', 'jingmai-cli-linker', 'environmentReady', '33000'):
            self.assertIn(word, doc)
        self.assertIn('large-requests-and-cli-versions.md', (ROOT / 'SKILL.md').read_text(encoding='utf-8'))
    def test_no_script_pins_a_cli_runtime_hash_or_version(self):
        for path in sorted((ROOT / 'scripts').glob('*.py')):
            text = path.read_text(encoding='utf-8', errors='replace')
            self.assertNotIn('e1871f6a32d54ec4', text, path.name); self.assertNotIn('runtime bridge requires', text, path.name)
    def test_the_skill_never_reads_credentials_for_signing(self):
        for name in ('jdo_bridge_probe.py', 'jdo_large_body_bridge.py'):
            text = (ROOT / 'scripts' / name).read_text(encoding='utf-8')
            for forbidden in ('CredReadW', 'read_app_secret', 'read_token_bundle', 'calc_sign', 'hmac'):
                self.assertNotIn(forbidden, text, name)


if __name__ == '__main__':
    unittest.main()
