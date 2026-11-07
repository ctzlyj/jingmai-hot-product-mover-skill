import json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts')); sys.path.insert(0, str(ROOT / 'tests'))
import photo_listing as pl
import jdo_listing_mvp as jdo
from photo_contract import ReconcileRequired
from photo_generation import atomic_json
from test_photo_listing import PhotoListingTests
from test_photo_contract import reference


def read(root): return json.loads((Path(root) / 'manifest.json').read_text(encoding='utf-8'))
def state(root): return json.loads((Path(root) / 'state.json').read_text(encoding='utf-8'))


class PublishOutcomeTests(unittest.TestCase):
    setUp = PhotoListingTests.setUp
    def prepared(self):
        d = pl.review_package(self.root)['digest']; pl.accept_images(self.root, d, confirm='PHOTOS_REVIEWED')
        with patch.object(pl, 'verify_store'), patch.object(pl.cli, 'upload', return_value={'imgUrl': 'jfs/t.jpg'}), patch.object(pl.cli, 'run_jdo_large', return_value={'data': {}}):
            pl.prepare_package(self.root, reference(), confirm='PREPARE_PHOTO_LISTING', digest=d)
        return d
    def publish(self, d, post, existing=None, verified=None):
        with patch.object(pl, 'verify_store'), patch.object(pl.pipeline, 'find_existing_product', return_value=existing), patch.object(pl.cli, 'run_jdo_large', side_effect=post) as w, patch.object(pl, 'verify_package', return_value=verified or {'status': 'VERIFIED'}):
            try: r = pl.publish_package(self.root, confirm='PUBLISH_TO_JD', digest=d)
            except Exception as e: r = e
        return r, w

    def test_explicit_platform_rejection_is_recorded_with_reason_not_unknown(self):
        d = self.prepared()
        def reject(**kw): raise jdo.JdoError('1010300506 校验异常!销售属性值别名格式错误')
        r, w = self.publish(d, reject)
        self.assertIsInstance(r, ReconcileRequired)
        s = state(self.root); self.assertEqual('REJECTED', s['status']); self.assertIn('1010300506', s['reason']); self.assertEqual(1, w.call_count)
        self.assertTrue((self.root / '.publish' / 'rejection.json').is_file())
    def test_rejected_without_remote_check_cannot_resubmit(self):
        d = self.prepared()
        def reject(**kw): raise jdo.JdoError('1010300506 x')
        self.publish(d, reject)
        with patch.object(pl, 'verify_store'), patch.object(pl.pipeline, 'find_existing_product', side_effect=AssertionError('network not expected')), patch.object(pl.cli, 'run_jdo_large') as w:
            with self.assertRaises(Exception): pl.publish_package(self.root, confirm='PUBLISH_TO_JD', digest=d)
            w.assert_not_called()
    def test_unknown_requires_remote_check_before_any_retry(self):
        d = self.prepared()
        def lost(**kw): raise RuntimeError('timeout')
        r, w = self.publish(d, lost); self.assertEqual('UNKNOWN', state(self.root)['status'])
        with patch.object(pl, 'verify_store'), patch.object(pl.pipeline, 'find_existing_product', return_value=None), patch.object(pl.cli, 'run_jdo_large') as w2:
            with self.assertRaises(ReconcileRequired): pl.publish_package(self.root, confirm='PUBLISH_TO_JD', digest=d)
            w2.assert_not_called()
    def test_unknown_with_remote_product_is_adopted_not_resubmitted(self):
        d = self.prepared()
        def lost(**kw): raise RuntimeError('timeout')
        self.publish(d, lost)
        with patch.object(pl, 'verify_store'), patch.object(pl.pipeline, 'find_existing_product', return_value={'productId': 77}), patch.object(pl.cli, 'run_jdo_large') as w, patch.object(pl, 'verify_package', return_value={'status': 'VERIFIED'}):
            pl.publish_package(self.root, confirm='PUBLISH_TO_JD', digest=d); w.assert_not_called()
        self.assertEqual(77, read(self.root)['productId'])
    def test_confirm_rejected_needs_zero_remote_evidence_then_unlocks_one_resubmit(self):
        d = self.prepared()
        def lost(**kw): raise RuntimeError('timeout')
        self.publish(d, lost)
        with patch.object(pl, 'verify_store'), patch.object(pl.pipeline, 'find_existing_product', return_value={'productId': 5}):
            with self.assertRaises(ReconcileRequired): pl.confirm_not_created(self.root, reason='平台报错')
        self.assertEqual('UNKNOWN', state(self.root)['status'])
        with patch.object(pl, 'verify_store'), patch.object(pl.pipeline, 'find_existing_product', return_value=None), patch.object(pl.pipeline, 'list_products', return_value=[{'productId': 1, 'productName': '其他'}]):
            ev = pl.confirm_not_created(self.root, reason='平台明确拒绝，用户已看到原文')
        self.assertEqual('REJECTED_CONFIRMED', state(self.root)['status']); self.assertEqual(0, len(ev['matches']))
        self.assertTrue((self.root / '.publish' / 'no-product-evidence.json').is_file())
        r, w = self.publish(d, lambda **kw: {'data': {'productId': 9, 'applyId': '9'}})
        self.assertEqual(1, w.call_count); self.assertEqual(9, read(self.root)['productId'])
    def test_confirm_not_created_refuses_when_same_title_exists(self):
        d = self.prepared(); atomic_json(self.root / 'state.json', {'status': 'UNKNOWN', 'itemNumber': 'PN-x'})
        title = read(self.root)['facts']['title']
        with patch.object(pl, 'verify_store'), patch.object(pl.pipeline, 'find_existing_product', return_value=None), patch.object(pl.pipeline, 'list_products', return_value=[{'productId': 3, 'productName': title}]):
            with self.assertRaises(ReconcileRequired): pl.confirm_not_created(self.root, reason='x')
        self.assertEqual('UNKNOWN', state(self.root)['status'])
    def test_confirm_not_created_only_from_unknown_or_rejected(self):
        self.prepared()
        with self.assertRaises(ValueError): pl.confirm_not_created(self.root, reason='x')

if __name__ == '__main__': unittest.main()
