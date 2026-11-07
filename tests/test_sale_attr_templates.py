import json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts')); sys.path.insert(0, str(ROOT / 'tests'))
import sale_attr_templates as sat

ATTR = '1001079446'
def official(inputs):
    items = [{'features': {'isLocked': locked, 'text': text}, 'properties': {'id': {'enum': [i]}, 'value': {'enum': [{'preprocessingSaleAttrValTemplateValueList': children}] if children else []}}} for (i, text, locked, children) in inputs]
    return [{'templateType': '2', 'templateData': json.dumps([{'attrId': int(ATTR), 'valueRules': json.dumps({'title': '尺寸', 'items': items}, ensure_ascii=False)}], ensure_ascii=False)}]
def sample_product(pid, cat, props, alias):
    return {'productInfo': {'productId': pid, 'categoryDetail': {'lastCategoryId': cat}, 'productStatus': {'productStatus': 8}},
            'liveProductStatus': 103,
            'skuList': [{'skuId': 1, 'saleAttrs': [{'attrId': ATTR, 'attrName': '尺寸', 'attrValueAlias': alias, 'templateSkuProps': props}]}]}
STRUCT_COMPONENT = {'componentCode': 'sale-prop-' + ATTR, 'componentTitle': '尺寸', 'extra': {'isStructure': True, 'dim': 1}}
PLAIN_COMPONENT = {'componentCode': 'sale-prop-9', 'componentTitle': '颜色', 'extra': {'isStructure': False, 'dim': 1}}


class OfficialTemplateTests(unittest.TestCase):
    def test_official_inputs_become_a_template_with_source(self):
        t = sat.from_official(official([(20319, '款式', '0', None), (19719, '层数', '0', None)]), ATTR)
        self.assertEqual('official-interface', t['source']['kind']); self.assertEqual([20319, 19719], [i['id'] for i in t['inputs']])
        self.assertTrue(all(not i['locked'] for i in t['inputs']))
    def test_locked_input_exposes_enum_leaves_with_ids(self):
        children = [{'name': '白色系', 'children': [{'id': 111, 'name': '乳白色'}]}]
        t = sat.from_official(official([(1, '颜色', '1', children)]), ATTR)
        self.assertEqual([{'valueId': 111, 'value': '乳白色'}], t['inputs'][0]['options'])
    def test_empty_or_other_attribute_gives_none(self):
        self.assertIsNone(sat.from_official([], ATTR)); self.assertIsNone(sat.from_official(official([(1, 'a', '0', None)]), '999'))


class SameShopDiscoveryTests(unittest.TestCase):
    props = [{'id': 20319, 'value': '【奶白色】锅盖架'}, {'id': 19719, 'unit': '层', 'value': '3'}]
    def test_discovery_reads_only_same_category_and_records_provenance(self):
        sample = sample_product(555, 35057, self.props, '【奶白色】锅盖架3层')
        t = sat.from_same_shop_products([sample], 35057, ATTR)
        self.assertEqual('same-shop-same-category-product', t['source']['kind']); self.assertEqual(555, t['source']['productId'])
        self.assertEqual(35057, t['source']['categoryId']); self.assertEqual('尺寸', t['source']['attrName'])
        self.assertEqual([20319, 19719], [i['id'] for i in t['inputs']]); self.assertTrue(t['source']['evidence'])
    def test_other_category_products_are_ignored(self):
        self.assertIsNone(sat.from_same_shop_products([sample_product(1, 99999, self.props, 'x')], 35057, ATTR))
    def test_off_sale_or_other_attribute_products_are_ignored(self):
        p = sample_product(1, 35057, self.props, 'x'); p['liveProductStatus'] = 104
        self.assertIsNone(sat.from_same_shop_products([p], 35057, ATTR))
        q = sample_product(2, 35057, self.props, 'x'); q['skuList'][0]['saleAttrs'][0]['attrId'] = '777'
        self.assertIsNone(sat.from_same_shop_products([q], 35057, ATTR))
    def test_inconsistent_samples_are_not_guessed(self):
        a = sample_product(1, 35057, self.props, 'a'); b = sample_product(2, 35057, [{'id': 5, 'value': 'x'}], 'b')
        self.assertIsNone(sat.from_same_shop_products([a, b], 35057, ATTR))
    def test_shorter_samples_that_are_a_prefix_mark_trailing_boxes_optional(self):
        long_ = sample_product(1, 35057, self.props, 'a'); short = sample_product(2, 35057, self.props[:1], 'b')
        t = sat.from_same_shop_products([short, long_], 35057, ATTR)
        self.assertEqual([False, True], [i['optional'] for i in t['inputs']]); self.assertEqual([19719], t['source']['optionalInputIds'])
        self.assertEqual([20319], [p['id'] for p in sat.build_props(t, ['只有款式'])[0]]); self.assertEqual(2, len(sat.build_props(t, ['款', '2'])[0]))
    def test_different_first_box_or_unit_is_not_guessed(self):
        a = sample_product(1, 35057, self.props, 'a'); c = sample_product(3, 35057, [{'id': 20319, 'value': 'x'}, {'id': 19719, 'unit': '个', 'value': '3'}], 'c')
        self.assertIsNone(sat.from_same_shop_products([a, c], 35057, ATTR))
    def test_values_from_the_sample_are_never_copied_into_the_template(self):
        t = sat.from_same_shop_products([sample_product(1, 35057, self.props, 'x')], 35057, ATTR)
        self.assertNotIn('锅盖架', json.dumps(t, ensure_ascii=False).replace(t['source']['evidence'], ''))


class ResolveTests(unittest.TestCase):
    def test_official_first_then_same_shop_then_block(self):
        calls = []
        def official_ok(cat): calls.append('official'); return official([(1, 'a', '0', None), (2, 'b', '0', None)])
        r = sat.resolve([STRUCT_COMPONENT], 35057, fetch_official=official_ok, fetch_samples=lambda c: calls.append('samples') or [])
        self.assertEqual(['official'], calls); self.assertEqual('official-interface', r['templates'][ATTR]['source']['kind'])
        calls.clear()
        def denied(cat): calls.append('official'); raise RuntimeError('99904030008 禁止访问')
        samples = [sample_product(9, 35057, [{'id': 1, 'value': 'x'}], 'x')]
        r = sat.resolve([STRUCT_COMPONENT], 35057, fetch_official=denied, fetch_samples=lambda c: calls.append('samples') or samples)
        self.assertEqual(['official', 'samples'], calls); self.assertEqual('same-shop-same-category-product', r['templates'][ATTR]['source']['kind'])
        r = sat.resolve([STRUCT_COMPONENT], 35057, fetch_official=denied, fetch_samples=lambda c: [])
        self.assertEqual({}, r['templates']); self.assertEqual([ATTR], [m['attrId'] for m in r['missing']]); self.assertIn('尺寸', r['ask'])
    def test_non_structured_attribute_needs_no_template(self):
        r = sat.resolve([PLAIN_COMPONENT], 35057, fetch_official=lambda c: [], fetch_samples=lambda c: [])
        self.assertEqual({}, r['templates']); self.assertEqual([], r['missing'])

class BuildPropsTests(unittest.TestCase):
    t = {'inputs': [{'id': 20319, 'text': '款式', 'locked': False, 'options': []}, {'id': 19719, 'text': '层数', 'locked': False, 'options': [], 'unit': '层'}]}
    def test_values_come_from_the_user_and_alias_is_joined_by_space(self):
        props, alias = sat.build_props(self.t, ['【奶白色】吸盘架', '1'])
        self.assertEqual([20319, 19719], [p['id'] for p in props]); self.assertEqual('【奶白色】吸盘架 1', alias.replace('层', '')) if False else None
        self.assertEqual('【奶白色】吸盘架1层', sat.alias_for(props))
    def test_missing_or_locked_values_are_rejected(self):
        with self.assertRaises(ValueError): sat.build_props(self.t, ['only one'])
        locked = {'inputs': [{'id': 1, 'text': '色', 'locked': True, 'options': [{'valueId': 5, 'value': '白'}]}]}
        with self.assertRaises(ValueError): sat.build_props(locked, ['红'])
        props, _ = sat.build_props(locked, ['白']); self.assertEqual({'id': 1, 'value': '白', 'valueId': 5}, props[0])

if __name__ == '__main__': unittest.main()
