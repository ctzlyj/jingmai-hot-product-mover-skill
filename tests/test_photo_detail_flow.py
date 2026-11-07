import json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts')); sys.path.insert(0, str(ROOT / 'tests'))
import photo_listing as pl
import detail_pages as dp
import detail_repair as dr
from detail_fixtures import noise_image, seven_skus, valid_detail_set
from test_photo_contract import facts, reference
from test_photo_listing import PhotoListingTests
from photo_contract import ReconcileRequired
from photo_generation import atomic_json, sha


def read(root): return json.loads((Path(root) / 'manifest.json').read_text(encoding='utf-8'))


class PlanAndMaterializeTests(unittest.TestCase):
    setUp = PhotoListingTests.setUp
    def test_make_plan_writes_detail_plan_with_six_to_eight_modules(self):
        r = pl.make_plan(self.root, reference(), preview=False)
        detail_tasks = [t for t in r['tasks'] if t['role'] == 'detail']
        self.assertTrue(6 <= len(detail_tasks) <= 8, len(detail_tasks))
        plan = json.loads((self.root / 'detail-plan.json').read_text(encoding='utf-8'))
        self.assertEqual([t['id'] for t in detail_tasks], [m['id'] for m in plan['modules']])
        self.assertTrue(all(t['photos'] == [str(self.root / 'photo.png')] for t in detail_tasks))
    def test_seven_sku_plan_task_for_each_sku_group(self):
        m = read(self.root); m['facts']['skus'] = seven_skus(); atomic_json(self.root / 'manifest.json', m)
        plan = pl.build_generation_plan(read(self.root), self.root, reference()) if False else None
        r = pl.make_plan(self.root, reference(), preview=False)
        covered = {k for mod in json.loads((self.root / 'detail-plan.json').read_text(encoding='utf-8'))['modules'] for k in mod['skuKeys']}
        self.assertEqual({s['key'] for s in seven_skus()}, covered)
        self.assertEqual(7, sum(t['role'] == 'sku' for t in r['tasks']))
    def test_preview_plan_has_no_detail_tasks(self):
        m = read(self.root); m['facts'] = {}; atomic_json(self.root / 'manifest.json', m)
        r = pl.make_plan(self.root, reference(), preview=True); self.assertFalse(any(t['role'] == 'detail' for t in r['tasks']))
    def test_materialize_keeps_modules_as_separate_ordered_790_images(self):
        r = pl.make_plan(self.root, reference(), preview=False); tasks = r['tasks']
        gen = self.root / '.generation' / 'generated'; gen.mkdir(parents=True); ledger = {}
        for i, t in enumerate(tasks):
            noise_image(gen / f"{t['id']}.jpg", i + 1, (1024, 1536 if t['role'] == 'detail' else 1024))
            ledger[t['id']] = {'status': 'SUCCEEDED', 'model': 'GPT-image-2-joybuilder', 'localPath': f"generated/{t['id']}.jpg"}
        (self.root / '.generation').mkdir(exist_ok=True)
        pl.materialize_generated(self.root, tasks, {'ledger': ledger, 'seconds': 1, 'modelsUsed': ['GPT-image-2-joybuilder'], 'successCount': len(tasks)})
        m = read(self.root); plan = json.loads((self.root / 'detail-plan.json').read_text(encoding='utf-8'))
        self.assertEqual([x['id'] for x in plan['modules']], [d['id'] for d in m['detailImages']])
        for d in m['detailImages']:
            with Image.open(self.root / d['localPath']) as im: self.assertEqual(790, im.width); self.assertGreater(im.height, im.width)
        self.assertFalse((self.root / '详情图' / '详情页长图.jpg').exists())
        self.assertTrue(dp.check_detail_set(m, self.root)['ok'])
    def test_detail_only_plan_does_not_clear_existing_main_or_sku_images(self):
        before = read(self.root)
        r = pl.make_plan(self.root, reference(), preview=False, only='detail')
        self.assertTrue(r['tasks'] and all(t['role'] == 'detail' for t in r['tasks']))
        gen = self.root / '.generation' / 'generated'; gen.mkdir(parents=True); ledger = {}
        for i, t in enumerate(r['tasks']):
            noise_image(gen / f"{t['id']}.jpg", 50 + i, (1024, 1536)); ledger[t['id']] = {'status': 'SUCCEEDED', 'model': 'GPT-image-2-joybuilder', 'localPath': f"generated/{t['id']}.jpg"}
        pl.materialize_generated(self.root, r['tasks'], {'ledger': ledger, 'seconds': 1, 'modelsUsed': ['x'], 'successCount': len(r['tasks'])})
        after = read(self.root); self.assertEqual(before['mainImages'], after['mainImages']); self.assertEqual(before['variants'], after['variants'])


class GateInFlowTests(unittest.TestCase):
    setUp = PhotoListingTests.setUp
    def one_detail(self):
        m = read(self.root); m['detailImages'] = m['detailImages'][:1]; atomic_json(self.root / 'manifest.json', m)
    def test_build_listing_refuses_single_detail_image(self):
        self.one_detail()
        with self.assertRaisesRegex(ValueError, '详情'): pl.build_listing(read(self.root), self.root, reference())
    def test_accept_images_and_prepare_refuse_before_any_upload(self):
        self.one_detail(); d = pl.review_package(self.root)['digest']
        with self.assertRaises(ValueError): pl.accept_images(self.root, d, confirm='PHOTOS_REVIEWED')
        with patch.object(pl.cli, 'upload') as up, patch.object(pl, 'verify_store'):
            with self.assertRaises(ValueError): pl.prepare_package(self.root, reference(), confirm='PREPARE_PHOTO_LISTING', digest=d)
            up.assert_not_called()
    def test_explicit_single_detail_passes_and_checklist_states_reason(self):
        self.one_detail(); m = read(self.root); m['facts']['detailPolicy'] = {'type': 'user-single-image', 'reason': '用户明确只要一张详情图'}; atomic_json(self.root / 'manifest.json', m)
        listing = pl.build_listing(read(self.root), self.root, reference()); self.assertEqual(1, sum(i['role'] == 'detail' for i in listing['images']))
        pl.review_package(self.root); text = (self.root / '确认清单.md').read_text(encoding='utf-8')
        self.assertIn('用户明确只要一张详情图', text)
    def test_checklist_lists_detail_count_modules_and_overview(self):
        pl.review_package(self.root); text = (self.root / '确认清单.md').read_text(encoding='utf-8')
        m = read(self.root); self.assertIn(f"详情页：{len(m['detailImages'])} 张", text)
        for d in m['detailImages']: self.assertIn(d['id'], text)
        self.assertTrue((self.root / '详情图' / '详情页总览.jpg').is_file()); self.assertIn('详情页总览.jpg', text)
        self.assertIn('详情页总览.jpg', (self.root / 'preview.html').read_text(encoding='utf-8'))
    def test_overview_never_enters_upload_list(self):
        pl.review_package(self.root); listing = pl.build_listing(read(self.root), self.root, reference())
        self.assertFalse(any('总览' in i['localPath'] for i in listing['images']))
    def test_publish_schema_for_photo_route_writes_pc_and_mobile_detail(self):
        listing = pl.build_listing(read(self.root), self.root, reference())
        for i in listing['images']: i['upload'] = {'imgUrl': 'jfs/x.jpg'}
        listing['category'] = {'id': 1}
        rows = pl.cli.build_publish_schema(listing)
        editor = next(r for r in rows if r['componentCode'] == 'WareEditor')
        agents = [next(p['value'] for p in e['properties'] if p['key'] == 'userAgent') for e in editor['value']]
        self.assertEqual(['pc', 'mobile'], agents)


class VerifyDetailTests(unittest.TestCase):
    setUp = PhotoListingTests.setUp
    def readback(self, mobile_urls=None, pc_urls=None):
        m = read(self.root); m['productId'] = 100; listing = pl.build_listing(m, self.root, reference()); sku = listing['skus'][0]; urls = []
        for n, item in enumerate(listing['images']):
            if item['role'] == 'detail':
                item['upload'] = {'imgUrl': f'jfs/detail{n}.jpg'}; urls.append(pl.cli.detail_image_url(item['upload']['imgUrl']))
            else: item['upload'] = {'imgUrl': {'spu_main': 'jfs/spu.jpg', 'sku_main': 'jfs/sku.jpg'}[item['role']]}
        m['variants'][0]['vendorSku'] = sku['vendorSku']; atomic_json(self.root / 'manifest.json', m); atomic_json(self.root / 'listing.json', listing)
        remote = {'skuId': 101, 'outerId': sku['vendorSku'], 'priceInfo': {'jdPrice': 10.50}, 'skuEnableStatus': 1, 'skuStats': {'onOffShelfStatus': 1}, 'saleAttrs': [{'attrValueAlias': '白色', 'attrValues': 'color-one'}]}
        detail = {'productInfo': {'productName': '测试收纳架', 'productStatus': {'productStatusNew': 1}, 'categoryDetail': {'lastCategoryId': 1}}, 'skuList': [remote]}
        desc = {'desc': dp.detail_html(pc_urls or urls), 'mobileDesc': dp.detail_html(mobile_urls or urls, mobile=True)}
        apply = {'applyInfo': {'draftComputeStatus': 2}, 'skuList': [remote], 'productInfo': {'productDetailDesc': desc}, 'material': {'mainImages': [{'uuid': '0000000000', 'imageInfoList': [{'imgUrl': 'jfs/spu.jpg', 'orderSort': 1}]}, {'uuid': 'color-one', 'imageInfoList': [{'imgUrl': 'jfs/sku.jpg', 'orderSort': 1}]}]}}
        return detail, apply, urls
    def verify(self, detail, apply):
        def get(skill, url, params): return {'data': apply if '/product-apply/' in url else detail}
        with patch.object(pl, 'verify_store'), patch.object(pl.cli, 'get', side_effect=get), patch.object(pl.cli, 'query_sku_stocks', return_value=[{'skuId': 101, 'stockNum': 12}]):
            return pl.verify_package(self.root, attempts=1, interval=0)
    def test_pc_and_mobile_matching_is_verified_with_four_separate_flags(self):
        detail, apply, urls = self.readback(); r = self.verify(detail, apply)
        self.assertEqual('VERIFIED', r['status']); c = read(self.root)['deliveryChecks']
        self.assertTrue(c['platformValidationPassed'] and c['productOnSale'] and c['detailQualityPassed']); self.assertFalse(c['videoBound'])
        self.assertIn('视频尚未绑定', r['statement']); self.assertNotIn('全部完成', r['statement'].replace('不能称全部完成', ''))
    def test_mobile_count_or_order_mismatch_is_not_success(self):
        detail, apply, urls = self.readback(); detail, apply, _ = self.readback(mobile_urls=urls[:-1])
        r = self.verify(detail, apply); self.assertEqual('VERIFICATION_FAILED', r['status'])
        c = read(self.root)['deliveryChecks']; self.assertTrue(c['productOnSale']); self.assertFalse(c['detailQualityPassed'])
        self.assertNotIn('上架完成', r['statement']); self.assertTrue(any('mobile' in p for p in r['problems']))
    def test_pc_order_mismatch_is_not_success(self):
        detail, apply, urls = self.readback(); detail, apply, urls = self.readback(pc_urls=urls[::-1])
        self.assertEqual('VERIFICATION_FAILED', self.verify(detail, apply)['status'])
    def test_local_gate_failure_after_publish_is_also_reported(self):
        detail, apply, urls = self.readback(); m = read(self.root); (self.root / m['detailImages'][0]['localPath']).write_bytes(b'broken')
        r = self.verify(detail, apply); self.assertEqual('VERIFICATION_FAILED', r['status']); self.assertFalse(read(self.root)['deliveryChecks']['detailQualityPassed'])
    def test_manifest_records_detail_plan_for_downstream_skills(self):
        detail, apply, urls = self.readback(); self.verify(detail, apply); m = read(self.root)
        self.assertEqual(len(m['detailImages']), m['detailPlan']['moduleCount']); self.assertEqual(len(m['detailImages']), len(m['detailPlan']['modules']))
        self.assertTrue(all(x['onlineUrl'] for x in m['detailPlan']['modules']))


class LiveStatusTests(unittest.TestCase):
    setUp = PhotoListingTests.setUp
    verify = VerifyDetailTests.verify
    readback = VerifyDetailTests.readback
    def test_live_on_sale_status_103_counts_as_on_sale(self):
        detail, apply, urls = self.readback(); detail['productInfo']['productStatus']['productStatusNew'] = 103
        r = self.verify(detail, apply); self.assertEqual('VERIFIED', r['status']); self.assertTrue(read(self.root)['deliveryChecks']['productOnSale'])
    def test_status_1_is_accepted_and_only_1_and_103(self):
        self.assertEqual((1, 103), pl.ON_SALE_STATUSES)
        detail, apply, urls = self.readback(); detail['productInfo']['productStatus']['productStatusNew'] = 1
        self.assertEqual('VERIFIED', self.verify(detail, apply)['status'])
    def test_104_105_and_other_statuses_are_not_on_sale(self):
        for status in (104, 105, 0, 2, 8, 102, 1030):
            detail, apply, urls = self.readback(); detail['productInfo']['productStatus']['productStatusNew'] = status
            r = self.verify(detail, apply)
            self.assertEqual('VERIFICATION_FAILED', r['status'], status); self.assertIn('product not on sale', r['problems'], status)
            self.assertFalse(read(self.root)['deliveryChecks']['productOnSale'], status); self.assertNotIn('上架完成', r['statement'], status)


class DetailRepairTests(unittest.TestCase):
    setUp = PhotoListingTests.setUp
    def published(self):
        m = read(self.root); m['productId'] = 100; m['status'] = 'VERIFIED'; m['mainImages'][0]['imgUrl'] = 'jfs/spu.jpg'; m['detailImages'] = m['detailImages'][:1]
        atomic_json(self.root / 'manifest.json', m)
    def remote(self):
        return {'applyInfo': {'draftComputeStatus': 2}, 'productInfo': {'categoryDetail': {'lastCategoryId': 7}, 'productDetailDesc': {'desc': "<p><img src='https://img10.360buyimg.com/imgzone/jfs/old.jpg'/></p>", 'mobileDesc': '', 'zhuangBaUseFlag': '0'}}}
    def prepare_ready(self):
        self.published(); r = dr.plan(self.root); self.assertEqual('detail', {t['role'] for t in r['tasks']}.pop())
        gen = self.root / '.generation' / 'generated'; gen.mkdir(parents=True); ledger = {}
        for i, t in enumerate(r['tasks']):
            noise_image(gen / f"{t['id']}.jpg", 70 + i, (1024, 1536)); ledger[t['id']] = {'status': 'SUCCEEDED', 'model': 'GPT-image-2-joybuilder', 'localPath': f"generated/{t['id']}.jpg"}
        pl.materialize_generated(self.root, r['tasks'], {'ledger': ledger, 'seconds': 1, 'modelsUsed': ['x'], 'successCount': len(r['tasks'])})
        return dr.review(self.root)
    def run_apply(self, digest, remote_after=None, patch_effect=None):
        calls = []
        def write(**kw): calls.append(kw); return {'data': {}}
        state = {'desc': self.remote()}
        def get(skill, url, params):
            return {'data': state['desc']}
        urls_holder = {}
        def upload(zone, cat, path): urls_holder.setdefault(sha(path), {'imgUrl': f'jfs/{sha(path)[:8]}.jpg'}); return urls_holder[sha(path)]
        def after_patch(**kw):
            calls.append(kw); body = json.loads(Path(kw['body_path']).read_text(encoding='utf-8')); comp = json.loads(body['patchOperationList'][0]['value'])
            html = {next(p['value'] for p in e['properties'] if p['key'] == 'userAgent'): next(p['value'] for p in e['properties'] if p['key'] == 'desc') for e in comp['value']}
            state['desc'] = {'applyInfo': {'draftComputeStatus': 2}, 'productInfo': {'categoryDetail': {'lastCategoryId': 7}, 'productDetailDesc': {'desc': html['pc'], 'mobileDesc': html['mobile']}}}
            return {'data': {}}
        runner = patch_effect or after_patch
        with patch.object(dr.pl, 'verify_store'), patch.object(dr.pl.cli, 'get', side_effect=get), patch.object(dr.pl.cli, 'upload', side_effect=upload), patch.object(dr.pl.cli, 'run_jdo_large', side_effect=runner):
            try: result = dr.apply(self.root, reference(), confirm='DETAIL_PATCH', digest=digest)
            except Exception as e: result = e
        return result, calls
    def test_repair_plan_is_detail_only_and_requires_published_product(self):
        with self.assertRaises(ValueError): dr.plan(self.root)
        self.published(); r = dr.plan(self.root); self.assertTrue(6 <= len(r['tasks']) <= 8); self.assertTrue(all(t['role'] == 'detail' for t in r['tasks']))
    def test_repair_requires_preview_digest_and_confirm_word(self):
        d = self.prepare_ready()['digest']
        with patch.object(dr.pl.cli, 'run_jdo_large') as w:
            with self.assertRaises(ValueError): dr.apply(self.root, reference(), confirm='', digest=d)
            with self.assertRaises(ValueError): dr.apply(self.root, reference(), confirm='DETAIL_PATCH', digest='stale')
            w.assert_not_called()
    def test_repair_patches_only_wareeditor_and_readback_matches(self):
        d = self.prepare_ready()['digest']; result, calls = self.run_apply(d)
        self.assertEqual(1, len(calls)); self.assertEqual('PATCH', calls[0]['method'].upper()); self.assertEqual('jdo-product-update', calls[0]['skill_id'])
        body = json.loads(Path(calls[0]['body_path']).read_text(encoding='utf-8'))
        self.assertEqual(1, len(body['patchOperationList'])); self.assertEqual('WareEditor', json.loads(body['patchOperationList'][0]['value'])['componentCode'])
        text = json.dumps(body)
        for forbidden in ('WareSkuImg', 'SKUS', 'jdPrice', 'stock', 'video', 'img'+'Url'): self.assertNotIn(forbidden, text)
        self.assertTrue(result['detailQualityPassed']); m = read(self.root); self.assertTrue(m['deliveryChecks']['detailQualityPassed']); self.assertEqual(100, m['productId'])
        self.assertGreaterEqual(len(m['detailImages']), 6)
    def test_repeat_apply_after_success_does_not_patch_again(self):
        d = self.prepare_ready()['digest']; self.run_apply(d)
        with patch.object(dr.pl, 'verify_store'), patch.object(dr.pl.cli, 'get', return_value={'data': self.remote()}), patch.object(dr.pl.cli, 'run_jdo_large') as w, patch.object(dr.pl.cli, 'upload'):
            with self.assertRaises(Exception): dr.apply(self.root, reference(), confirm='DETAIL_PATCH', digest=d)
            w.assert_not_called()
    def test_unknown_result_is_reconciled_by_readback_never_repatched(self):
        d = self.prepare_ready()['digest']
        def boom(**kw): raise RuntimeError('timeout')
        result, calls = self.run_apply(d, patch_effect=boom); self.assertIsInstance(result, ReconcileRequired)
        state = json.loads((self.root / '.detail-repair' / 'state.json').read_text(encoding='utf-8')); self.assertEqual('UNKNOWN', state['status'])
        with patch.object(dr.pl, 'verify_store'), patch.object(dr.pl.cli, 'get', return_value={'data': self.remote()}), patch.object(dr.pl.cli, 'run_jdo_large') as w, patch.object(dr.pl.cli, 'upload'):
            with self.assertRaises(ReconcileRequired): dr.apply(self.root, reference(), confirm='DETAIL_PATCH', digest=d)
            w.assert_not_called()
    def test_unknown_but_remote_already_updated_becomes_success_without_patch(self):
        d = self.prepare_ready()['digest']
        def landed_then_error(**kw):
            self._landed = json.loads(Path(kw['body_path']).read_text(encoding='utf-8')); raise RuntimeError('lost response')
        result, calls = self.run_apply(d, patch_effect=landed_then_error); self.assertIsInstance(result, ReconcileRequired)
        comp = json.loads(self._landed['patchOperationList'][0]['value'])
        html = {next(p['value'] for p in e['properties'] if p['key'] == 'userAgent'): next(p['value'] for p in e['properties'] if p['key'] == 'desc') for e in comp['value']}
        remote = {'applyInfo': {'draftComputeStatus': 2}, 'productInfo': {'categoryDetail': {'lastCategoryId': 7}, 'productDetailDesc': {'desc': html['pc'], 'mobileDesc': html['mobile']}}}
        with patch.object(dr.pl, 'verify_store'), patch.object(dr.pl.cli, 'get', return_value={'data': remote}), patch.object(dr.pl.cli, 'run_jdo_large') as w, patch.object(dr.pl.cli, 'upload'):
            r = dr.apply(self.root, reference(), confirm='DETAIL_PATCH', digest=d); w.assert_not_called()
        self.assertTrue(r['detailQualityPassed'])
    def test_repair_never_touches_price_stock_sku_main_or_video_fields(self):
        before = read(self.root); self.published(); b2 = read(self.root); d = self.prepare_ready()['digest']; self.run_apply(d); after = read(self.root)
        for key in ('variants', 'mainImages', 'video', 'skuIds'): self.assertEqual(b2.get(key), after.get(key), key)
        self.assertEqual(b2['facts']['skus'], after['facts']['skus'])

if __name__ == '__main__': unittest.main()
