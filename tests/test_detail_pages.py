import copy, json, sys, tempfile, unittest
from pathlib import Path
from PIL import Image
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts')); sys.path.insert(0, str(ROOT / 'tests'))
import detail_pages as dp
from detail_fixtures import noise_image, seven_skus, valid_detail_set
from test_photo_contract import facts
from photo_contract import initial_manifest
from photo_generation import atomic_json, sha


def manifest_with(skus=None, **extra):
    f = facts(); f['skus'] = skus or f['skus']; f.update(extra)
    m = initial_manifest({'profile': 'pop-test', 'venderId': 1, 'shopName': '测试'}, ['photo.png'], '测试收纳架', None)
    m['facts'] = f
    return m


class PlanTests(unittest.TestCase):
    def build(self, m):
        return dp.build_detail_plan(m, Path('.'), identity_rules='只使用实拍商品。', style='干净的电商风格')
    def test_default_plan_has_six_to_eight_modules_not_three(self):
        plan = self.build(manifest_with())
        self.assertTrue(6 <= len(plan['modules']) <= 8, len(plan['modules']))
        self.assertGreater(len(plan['modules']), 3)
    def test_seven_sku_plan_covers_every_sku_not_one_generic_image(self):
        m = manifest_with(seven_skus()); plan = self.build(m)
        covered = {k for mod in plan['modules'] for k in mod['skuKeys']}
        self.assertEqual({s['key'] for s in m['facts']['skus']}, covered)
        self.assertGreaterEqual(sum(mod['kind'] == 'sku_options' for mod in plan['modules']), 2)
        self.assertTrue(6 <= len(plan['modules']) <= 8)
    def test_every_required_kind_is_present_and_documented(self):
        plan = self.build(manifest_with())
        kinds = {mod['kind'] for mod in plan['modules']}
        self.assertTrue(set(dp.REQUIRED_KINDS) <= kinds)
        for mod in plan['modules']:
            for key in ('purpose', 'factsUsed', 'skuKeys', 'prompt', 'forbidden', 'outputFile', 'photos'):
                self.assertIn(key, mod)
            self.assertIn('价格', mod['forbidden'])
    def test_prompts_never_carry_reference_assets_or_promo_text(self):
        plan = self.build(manifest_with())
        for mod in plan['modules']:
            self.assertNotIn('reference-cache', json.dumps(mod, ensure_ascii=False))
    def test_style_with_price_or_competitor_marker_is_rejected(self):
        with self.assertRaises(ValueError):
            dp.build_detail_plan(manifest_with(), Path('.'), identity_rules='x', style='学习它的99元秒杀角标', reference_terms=[])
        with self.assertRaises(ValueError):
            dp.build_detail_plan(manifest_with(), Path('.'), identity_rules='x', style='像某某旗舰店', reference_terms=['某某'])
    def test_unconfirmed_numbers_in_visible_text_are_rejected(self):
        facts_ = {'sellingPoints': ['吸盘壁挂']}
        self.assertTrue(dp.scan_visible_text(['承重50公斤'], facts_))
        self.assertTrue(dp.scan_visible_text(['限时秒杀'], facts_))
        self.assertEqual([], dp.scan_visible_text(['吸盘壁挂'], facts_))
    def test_user_confirmed_number_in_selling_points_is_allowed(self):
        m = manifest_with(); m['facts']['sellingPoints'] = ['承重50公斤']
        self.assertTrue(self.build(m)['modules'])
    def test_promo_in_selling_points_blocks_plan(self):
        m = manifest_with(); m['facts']['sellingPoints'] = ['限时特价']
        with self.assertRaises(ValueError):
            self.build(m)

class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
    def ready(self, skus=None, **extra):
        m = manifest_with(skus, **extra); plan = valid_detail_set(self.root, m); return m, plan
    def test_complete_set_passes(self):
        m, plan = self.ready(seven_skus()); r = dp.check_detail_set(m, self.root)
        self.assertTrue(r['ok'], r['problems']); self.assertEqual(len(plan['modules']), r['count'])
    def test_single_image_fails(self):
        m, plan = self.ready(); m['detailImages'] = m['detailImages'][:1]
        r = dp.check_detail_set(m, self.root); self.assertFalse(r['ok'])
    def test_missing_required_module_fails(self):
        m, plan = self.ready(); plan['modules'] = [x for x in plan['modules'] if x['kind'] != 'aftercare']
        atomic_json(self.root / 'detail-plan.json', plan)
        m['detailImages'] = [x for x in m['detailImages'] if x['module'] != 'aftercare']
        self.assertTrue(any('注意事项' in p for p in dp.check_detail_set(m, self.root)['problems']))
    def test_missing_scene_or_specs_module_fails(self):
        for kind, word in (('scene', '使用场景'), ('specs_package', '参数与包装')):
            m, plan = self.ready(); plan['modules'] = [x for x in plan['modules'] if x['kind'] != kind]
            atomic_json(self.root / 'detail-plan.json', plan); m['detailImages'] = [x for x in m['detailImages'] if x['module'] != kind]
            self.assertTrue(any(word in p for p in dp.check_detail_set(m, self.root)['problems']), kind)
    def test_missing_sku_coverage_fails(self):
        m, plan = self.ready(seven_skus())
        for mod in plan['modules']:
            if mod['kind'] == 'sku_options': mod['skuKeys'] = mod['skuKeys'][:-1]
        atomic_json(self.root / 'detail-plan.json', plan)
        self.assertTrue(any('未覆盖全部SKU' in p for p in dp.check_detail_set(m, self.root)['problems']))
    def test_explicit_single_image_requires_reason_and_is_reported(self):
        m, _ = self.ready(); m['detailImages'] = m['detailImages'][:1]
        m['facts']['detailPolicy'] = {'type': 'user-single-image', 'reason': '用户明确只要一张详情图'}
        r = dp.check_detail_set(m, self.root); self.assertTrue(r['ok'], r['problems']); self.assertIn('只要一张', r['exemption']['reason'])
        m['facts']['detailPolicy'] = {'type': 'user-single-image', 'reason': ''}
        self.assertFalse(dp.check_detail_set(m, self.root)['ok'])
    def test_reference_image_cannot_enter_upload_list(self):
        m, _ = self.ready(); cache = self.root / '.reference-cache' / 'detail'; cache.mkdir(parents=True)
        shutil_target = cache / '01.jpg'; shutil_target.write_bytes((self.root / m['detailImages'][0]['localPath']).read_bytes())
        atomic_json(self.root / '.reference-cache' / 'assets.json', {'entries': [{'sha256': sha(shutil_target)}]})
        r = dp.check_detail_set(m, self.root); self.assertTrue(any('参考链接原图' in p for p in r['problems']))
        m['detailImages'][0]['localPath'] = '.reference-cache/detail/01.jpg'
        self.assertFalse(dp.check_detail_set(m, self.root)['ok'])
    def test_wrong_width_unreadable_and_duplicates_fail(self):
        m, _ = self.ready(); first = self.root / m['detailImages'][0]['localPath']
        Image.new('RGB', (800, 1200), 'white').save(first); m['detailImages'][0].pop('sha256')
        self.assertTrue(any('750或790' in p for p in dp.check_detail_set(m, self.root)['problems']))
        m, _ = self.ready(); (self.root / m['detailImages'][1]['localPath']).write_bytes(b'broken')
        self.assertTrue(any('不可读' in p for p in dp.check_detail_set(m, self.root)['problems']))
        m, _ = self.ready()
        (self.root / m['detailImages'][1]['localPath']).write_bytes((self.root / m['detailImages'][0]['localPath']).read_bytes())
        m['detailImages'][1].pop('sha256')
        self.assertTrue(any('重复' in p for p in dp.check_detail_set(m, self.root)['problems']))
    def test_listing_gate_blocks_short_generic_listing_unless_reasoned(self):
        images = [{'role': 'detail'}] * 3
        self.assertTrue(dp.listing_detail_errors(images, {}))
        self.assertEqual([], dp.listing_detail_errors([{'role': 'detail'}] * 6, {}))
        self.assertTrue(dp.listing_detail_errors(images, {'detailPolicy': 'user-single-image'}))
        self.assertEqual([], dp.listing_detail_errors(images, {'detailPolicy': 'user-single-image', 'detailExemptionReason': '用户要求'}))


class ReadbackTests(unittest.TestCase):
    urls = ['https://img10.360buyimg.com/imgzone/jfs/a.jpg', 'https://img10.360buyimg.com/imgzone/jfs/b.jpg']
    def desc(self, urls): return dp.detail_html(urls)
    def test_matching_pc_and_mobile_pass(self):
        r = dp.readback_detail({'desc': self.desc(self.urls), 'mobileDesc': self.desc(self.urls)}, self.urls)
        self.assertEqual([], r['problems']); self.assertTrue(r['pcOrderMatches'] and r['mobileOrderMatches'])
    def test_mobile_count_mismatch_is_failure(self):
        r = dp.readback_detail({'desc': self.desc(self.urls), 'mobileDesc': self.desc(self.urls[:1])}, self.urls)
        self.assertTrue(any('mobile' in p for p in r['problems']))
    def test_pc_order_mismatch_is_failure(self):
        r = dp.readback_detail({'desc': self.desc(self.urls[::-1]), 'mobileDesc': self.desc(self.urls)}, self.urls)
        self.assertTrue(any('pc' in p for p in r['problems']))
    def test_missing_mobile_is_failure(self):
        self.assertTrue(dp.readback_detail({'desc': self.desc(self.urls)}, self.urls)['problems'])


class StatementTests(unittest.TestCase):
    def test_validation_passed_but_detail_failed_is_never_complete(self):
        text = dp.completion_statement({'platformValidationPassed': True, 'productOnSale': True, 'detailQualityPassed': False})
        self.assertNotIn('上架完成', text); self.assertNotIn('全部完成', text); self.assertIn('detailQualityPassed', text)
    def test_video_not_bound_cannot_say_all_complete(self):
        text = dp.completion_statement({'platformValidationPassed': True, 'productOnSale': True, 'detailQualityPassed': True, 'videoBound': False})
        self.assertNotIn('全部完成', text.replace('不能称全部完成', ''))
    def test_all_four_true_is_all_complete(self):
        self.assertEqual('全部完成', dp.completion_statement({k: True for k in ('platformValidationPassed', 'productOnSale', 'detailQualityPassed', 'videoBound')}))


class PatchBodyTests(unittest.TestCase):
    def test_only_wareeditor_with_pc_and_mobile(self):
        body = dp.build_detail_patch_body(9, 7, ['https://img10.360buyimg.com/imgzone/jfs/a.jpg'])
        self.assertEqual(1, len(body['patchOperationList']))
        component = json.loads(body['patchOperationList'][0]['value'])
        self.assertEqual('WareEditor', component['componentCode'])
        self.assertEqual(['pc', 'mobile'], [next(p['value'] for p in e['properties'] if p['key'] == 'userAgent') for e in component['value']])
        text = json.dumps(body)
        for forbidden in ('WareSkuImg', 'SKUS', 'jdPrice', 'stock', 'video'):
            self.assertNotIn(forbidden, text)

if __name__ == '__main__': unittest.main()

