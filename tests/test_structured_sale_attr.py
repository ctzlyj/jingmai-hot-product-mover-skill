import json, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import jdo_listing_mvp as cli


def sku(**extra):
    d = {'skuKey': 'k', 'styleAlias': '【奶白色】吸盘架1层', 'styleMappingId': 'm', 'styleOrder': 1, 'sizeAlias': 'x', 'sizeMappingId': 'n', 'sizeOrder': 1}
    d.update(extra)
    return d


def props(component):
    return {p['key']: p['value'] for p in component['properties']}


class StructuredSaleAttributeTests(unittest.TestCase):
    defs = {'style': {'componentCode': 'sale-prop-1', 'attrName': '尺寸'}}
    def test_without_template_props_output_is_unchanged(self):
        comps = cli.sale_attribute_components(sku(), False, self.defs)
        self.assertNotIn('templateSkuProps', props(comps[0]))
    def test_structured_attribute_submits_template_sku_props_as_json_string(self):
        tsp = [{'id': 20319, 'value': '【奶白色】吸盘架'}, {'id': 19719, 'unit': '层', 'value': '1'}]
        comps = cli.sale_attribute_components(sku(styleTemplateSkuProps=tsp), False, self.defs)
        p = props(comps[0])
        self.assertEqual(tsp, json.loads(p['templateSkuProps']))
        self.assertEqual('[【奶白色】吸盘架1层]', p['attrValueAlias'])
    def test_size_dimension_also_supported(self):
        tsp = [{'id': 5, 'value': 'a'}]
        comps = cli.sale_attribute_components(sku(sizeTemplateSkuProps=tsp), True, {'style': {'componentCode': 's', 'attrName': 'a'}, 'size': {'componentCode': 'z', 'attrName': 'b'}})
        self.assertEqual(tsp, json.loads(props(comps[1])['templateSkuProps']))
        self.assertNotIn('templateSkuProps', props(comps[0]))

if __name__ == '__main__': unittest.main()
