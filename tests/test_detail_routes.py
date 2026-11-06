import json, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts')); sys.path.insert(0, str(ROOT / 'tests'))
import detail_pages as dp
import jdo_listing_mvp as cli


class RouteGateTests(unittest.TestCase):
    """Link-move / replica / template routes reach the platform through validate_manifest."""
    def reference(self):
        return {'category': {'id': 1}, 'componentCatalog': [], 'brands': [], 'media': {'categories': [{'categoryId': 1}], 'defaultCategoryId': 1}}
    def manifest(self, n):
        images = [{'imageKey': f'D{i}', 'role': 'detail', 'localPath': f'd{i}.jpg', 'mediaCategoryId': 1} for i in range(n)]
        return {'category': {'id': 1}, 'product': {'productKey': 'T', 'title': '测试', 'brandId': -1, 'sevenDayReturnCode': '1', 'freightTemplateId': '1'},
                'skus': [], 'images': images, 'categoryAttributes': []}
    def test_short_detail_is_a_preflight_error_for_every_validate_manifest_route(self):
        errors, _ = cli.validate_manifest(ROOT / 'listing.json', self.manifest(1), self.reference())
        self.assertTrue(any('详情图只有1张' in e for e in errors))
    def test_six_detail_images_do_not_trigger_the_detail_error(self):
        errors, _ = cli.validate_manifest(ROOT / 'listing.json', self.manifest(6), self.reference())
        self.assertFalse(any('详情图只有' in e for e in errors))
    def test_explicit_exemption_needs_reason(self):
        m = self.manifest(1); m['product'].update(detailPolicy='user-single-image')
        errors, _ = cli.validate_manifest(ROOT / 'listing.json', m, self.reference()); self.assertTrue(any('detailExemptionReason' in e for e in errors))
        m['product']['detailExemptionReason'] = '用户明确只要一张'
        errors, _ = cli.validate_manifest(ROOT / 'listing.json', m, self.reference()); self.assertFalse(any('详情' in e for e in errors))


class SourceDetailGateTests(unittest.TestCase):
    """Matrix route copies the source product's own detail HTML: it must be complete and PC/mobile aligned."""
    u = ['https://img10.360buyimg.com/imgzone/jfs/%d.jpg' % i for i in range(6)]
    def test_complete_source_detail_passes(self):
        self.assertEqual([], dp.source_detail_problems({'desc': dp.detail_html(self.u), 'mobileDesc': dp.detail_html(self.u, mobile=True)}))
    def test_one_image_source_is_blocked(self):
        problems = dp.source_detail_problems({'desc': dp.detail_html(self.u[:1]), 'mobileDesc': dp.detail_html(self.u[:1])})
        self.assertTrue(problems); self.assertTrue(any('detail-repair' in p for p in problems))
    def test_mobile_mismatch_is_blocked(self):
        self.assertTrue(dp.source_detail_problems({'desc': dp.detail_html(self.u), 'mobileDesc': dp.detail_html(self.u[:3])}))
    def test_reason_allows_short_source(self):
        self.assertEqual([], dp.source_detail_problems({'desc': dp.detail_html(self.u[:1])}, exemption_reason='用户明确只要一张'))

if __name__ == '__main__': unittest.main()
