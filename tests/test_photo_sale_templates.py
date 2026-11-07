import json, sys, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts')); sys.path.insert(0, str(ROOT / 'tests'))
import photo_listing as pl
from photo_generation import atomic_json
from test_photo_listing import PhotoListingTests
from test_photo_contract import reference


def structured_reference():
    r = reference()
    for c in pl.walk_components(r['schema']):
        if c['componentCode'] == 'sale-prop-2':
            c['extra'] = {'isStructure': True, 'dim': 1}
    return r

def read(root): return json.loads((Path(root) / 'manifest.json').read_text(encoding='utf-8'))


class StructuredGateTests(unittest.TestCase):
    setUp = PhotoListingTests.setUp
    def test_structured_attribute_without_template_props_blocks_the_listing(self):
        with self.assertRaisesRegex(ValueError, 'templateSkuProps'):
            pl.build_listing(self.m, self.root, structured_reference())
    def test_error_names_the_attribute_and_says_prevalidation_is_not_proof(self):
        try: pl.build_listing(self.m, self.root, structured_reference())
        except ValueError as e: text = str(e)
        self.assertIn('规格', text); self.assertIn('预校验', text)
    def test_non_structured_template_is_unaffected(self):
        self.assertTrue(pl.build_listing(self.m, self.root, reference())['skus'])
    def test_with_template_props_the_listing_builds_and_submits_them(self):
        self.m['facts']['skus'][0]['styleTemplateSkuProps'] = [{'id': 1, 'value': '白色'}]
        atomic_json(self.root / 'manifest.json', self.m)
        listing = pl.build_listing(read(self.root), self.root, structured_reference())
        self.assertEqual([{'id': 1, 'value': '白色'}], listing['skus'][0]['styleTemplateSkuProps'])

class DiscoverCommandTests(unittest.TestCase):
    setUp = PhotoListingTests.setUp
    def sample(self):
        return {'productInfo': {'productId': 55, 'categoryDetail': {'lastCategoryId': 1}, 'productStatus': {'productStatus': 8}},
                'liveProductStatus': 103,
                'skuList': [{'saleAttrs': [{'attrId': '2', 'attrName': '规格', 'attrValueAlias': 'x', 'templateSkuProps': [{'id': 7, 'value': 'x'}]}]}]}
    def test_discovery_writes_templates_with_provenance_into_the_package(self):
        with patch.object(pl, 'verify_store'), patch.object(pl, '_fetch_official_sale_templates', side_effect=RuntimeError('99904030008')), patch.object(pl, '_fetch_same_shop_samples', return_value=[self.sample()]):
            r = pl.discover_sale_templates(self.root, structured_reference())
        self.assertEqual([], r['missing']); data = json.loads((self.root / 'sale-attr-templates.json').read_text(encoding='utf-8'))
        self.assertEqual(55, data['templates']['2']['source']['productId']); self.assertEqual(1, data['templates']['2']['source']['categoryId'])
        self.assertNotIn('token', json.dumps(data).lower())
        self.assertEqual(55, read(self.root)['saleAttributeTemplates']['2']['productId'])
    def test_unresolvable_template_stops_with_one_question(self):
        with patch.object(pl, 'verify_store'), patch.object(pl, '_fetch_official_sale_templates', side_effect=RuntimeError('denied')), patch.object(pl, '_fetch_same_shop_samples', return_value=[]):
            r = pl.discover_sale_templates(self.root, structured_reference())
        self.assertEqual(1, len(r['missing'])); self.assertIn('请提供', r['ask'])
    def test_apply_templates_fills_skus_from_user_values_only(self):
        tpl = {'templates': {'2': {'attrId': '2', 'inputs': [{'id': 7, 'text': '', 'locked': False, 'options': []}], 'source': {'kind': 'x'}}}, 'missing': []}
        atomic_json(self.root / 'sale-attr-templates.json', tpl)
        f = read(self.root); f['facts']['skus'][0]['templateValues'] = ['白色款']; atomic_json(self.root / 'manifest.json', f)
        out = pl.apply_sale_templates(self.root); m = read(self.root)
        self.assertEqual([{'id': 7, 'value': '白色款'}], m['facts']['skus'][0]['styleTemplateSkuProps']); self.assertEqual('白色款', out['aliases']['one'])
    def test_apply_refuses_when_values_missing(self):
        atomic_json(self.root / 'sale-attr-templates.json', {'templates': {'2': {'attrId': '2', 'inputs': [{'id': 7, 'locked': False, 'options': []}], 'source': {}}}, 'missing': []})
        with self.assertRaises(ValueError): pl.apply_sale_templates(self.root)

if __name__ == '__main__': unittest.main()
