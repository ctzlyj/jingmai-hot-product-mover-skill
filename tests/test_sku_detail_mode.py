import json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

class SkuDetailModeBoundaryTests(unittest.TestCase):
    def test_skill_top_states_the_hard_boundary(self):
        text = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('## 详情模式边界', text)
        for phrase in (
            '默认 SPU',
            '仅京麦客户端人工设置',
            '禁止探测 descModel',
            '禁止网页自动化',
            '禁止客户端 CDP',
            'productInfo.productDetailDesc',
            'skuList[].productDetailDesc.desc',
        ):
            self.assertIn(phrase, text)

    def test_reference_records_three_channels_false_success_and_ten_sku_mapping(self):
        path = ROOT / 'references' / 'jd-sku-detail-mode.md'
        self.assertTrue(path.is_file(), 'The SKU-detail mode reference is missing')
        text = path.read_text(encoding='utf-8')
        for phrase in (
            'REST',
            '京麦网页',
            '京麦客户端',
            'descModel=2',
            'success',
            'productInfo.productDetailDesc',
            'skuList[].productDetailDesc.desc',
            '10 个 SKU',
        ):
            self.assertIn(phrase, text)

    def test_browser_source_routing_does_not_route_sku_detail_to_browser(self):
        text = (ROOT / 'references' / 'browser-source-routing.md').read_text(encoding='utf-8')
        self.assertIn('SKU 维度详情', text)
        self.assertIn('不走网页自动化', text)


class ListingPackageBoundaryTests(unittest.TestCase):
    def test_initial_manifest_exports_the_manual_boundary(self):
        from photo_contract import initial_manifest
        manifest = initial_manifest(
            {'profile': 'pop-test', 'venderId': 1, 'shopName': '测试店'},
            ['photo.png'],
            '测试商品',
        )
        self.assertEqual('SPU', manifest['detailMode'])
        self.assertTrue(manifest['skuDetailManualPending'])
        self.assertEqual([], manifest['skuDetailPendingKeys'])
        self.assertTrue(any('京麦客户端' in step for step in manifest['skuDetailManualSteps']))
        self.assertNotIn('descModel', manifest)

    def test_schema_declares_the_boundary_fields(self):
        schema = json.loads((ROOT / 'references' / 'listing-package.schema.json').read_text(encoding='utf-8'))
        properties = schema['properties']
        self.assertEqual(['SPU'], properties['detailMode']['enum'])
        self.assertEqual('boolean', properties['skuDetailManualPending']['type'])
        self.assertEqual('array', properties['skuDetailManualSteps']['type'])
        checks = properties['deliveryChecks']['properties']
        self.assertEqual(['SPU'], checks['detailMode']['enum'])
        self.assertEqual('boolean', checks['skuDetailManualPending']['type'])


class ReviewChecklistBoundaryTests(unittest.TestCase):
    def test_checklist_shows_detail_mode_pending_skus_and_client_steps(self):
        import photo_listing as pl
        from photo_contract import initial_manifest
        from photo_generation import atomic_json
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'listing-package'
            root.mkdir()
            for name in ('main.jpg', 'sku.jpg', 'detail.jpg'):
                Image.new('RGB', (32, 32), 'white').save(root / name)
            manifest = initial_manifest(
                {'profile': 'pop-test', 'venderId': 1, 'shopName': '测试店'},
                ['photo.png'],
                '测试商品',
            )
            manifest['facts'] = {
                'skus': [
                    {'key': 'sku-1', 'spec': '单层', 'jdPrice': '10.50', 'stock': 10, 'skuAttributes': []},
                    {'key': 'sku-2', 'spec': '双层', 'jdPrice': '12.50', 'stock': 10, 'skuAttributes': []},
                ]
            }
            manifest['mainImages'] = [{'localPath': 'main.jpg'}]
            manifest['variants'] = [{'key': 'sku-1', 'skuImage': {'localPath': 'sku.jpg'}}]
            manifest['detailImages'] = [{'localPath': 'detail.jpg', 'module': 'hero', 'skuKeys': ['sku-1', 'sku-2']}]
            atomic_json(root / 'manifest.json', manifest)
            with patch.object(pl.dp, 'check_detail_set', return_value={'ok': True, 'problems': [], 'exemption': None}), \
                 patch.object(pl.dp, 'overview_image', return_value=root / '详情图' / '详情页总览.jpg'):
                pl.review_package(root)
            text = (root / '确认清单.md').read_text(encoding='utf-8')
            self.assertIn('详情模式：SPU', text)
            self.assertIn('SKU 详情待手动设置：是', text)
            self.assertIn('待设置 SKU：sku-1、sku-2', text)
            self.assertIn('京麦客户端', text)


if __name__ == '__main__':
    unittest.main()


class ReadbackRuleTests(unittest.TestCase):
    def test_sku_detail_pending_requires_per_sku_desc_readback(self):
        from photo_listing import sku_detail_pending
        expected = {f'PN-{i:03d}': {'skuKey': f'sku-{i:03d}'} for i in range(1, 11)}
        remote = {'skuList': [
            {'outerId': f'PN-{i:03d}', 'productDetailDesc': {'desc': '' if i <= 9 else '<p>ok</p>'}}
            for i in range(1, 11)
        ]}
        pending = sku_detail_pending(remote, expected)
        self.assertEqual([f'sku-{i:03d}' for i in range(1, 10)], pending)
        for item in remote['skuList']: item['productDetailDesc']['desc'] = '<p>ok</p>'
        self.assertEqual([], sku_detail_pending(remote, expected))

    def test_delivery_checks_and_statement_expose_manual_pending(self):
        from detail_pages import completion_statement, delivery_checks
        manifest = {'deliveryChecks': {'platformValidationPassed': True, 'productOnSale': True, 'detailQualityPassed': True}, 'detailMode': 'SPU', 'skuDetailManualPending': True}
        checks = delivery_checks(manifest)
        self.assertEqual('SPU', checks['detailMode'])
        self.assertTrue(checks['skuDetailManualPending'])
        self.assertIn('SKU 详情仍需京麦客户端人工设置', completion_statement(checks))
