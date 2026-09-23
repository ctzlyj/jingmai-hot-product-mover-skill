import json
import subprocess
import unittest
from pathlib import Path

from test_browser_source_read import taobao_response


ROOT = Path(__file__).resolve().parents[1]


class TaobaoCollectorGuardTests(unittest.TestCase):
    def capture(self, response, price_policy=None, page_url='https://item.taobao.com/item.htm?id=123'):
        collector = (ROOT / 'scripts/browser_collectors/taobao_capture.js').read_text(encoding='utf-8')
        script = '''const vm = require('node:vm');
const context = {URL, location:{href:__PAGE_URL__},
  window:{__ICE_APP_CONTEXT__:{loaderData:{home:{data:{res:__RESPONSE__}}}}},
  document:{querySelectorAll:()=>[]}};
console.log(JSON.stringify(vm.runInNewContext('(' + __COLLECTOR__ + ')(null, ' + JSON.stringify(__PRICE_POLICY__) + ')', context)));
'''.replace('__RESPONSE__', json.dumps(response)).replace('__COLLECTOR__', json.dumps(collector)).replace('__PRICE_POLICY__', json.dumps(price_policy or {})).replace('__PAGE_URL__', json.dumps(page_url))
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, encoding='utf-8', timeout=10, check=True)
        return json.loads(result.stdout)

    def test_description_file_keeps_slash_for_legacy_query_reader(self):
        response = taobao_response()
        response['item']['pcADescUrl'] = 'https://market.m.taobao.com/app/detail-project/desc/index.html?id=123&f=desc/fixture-file&sellerType=C&unneeded=value'
        capture = self.capture(response)
        self.assertEqual(capture['reason'], 'DESCRIPTION_PAGE_REQUIRED')
        self.assertIn('f=desc/fixture-file', capture['descriptionUrl'])
        self.assertNotIn('unneeded', capture['descriptionUrl'])

    def test_price_login_prompt_does_not_prove_account_logged_out(self):
        response = taobao_response()
        response['componentsVO'] = {'priceVO': {'price': {'priceTitle': '优惠前', 'priceActionText': '登录查看更多优惠'}}}
        capture = self.capture(response)
        self.assertEqual(capture, {'ok': False, 'reason': 'EFFECTIVE_PRICE_UNVERIFIED'})

    def test_explicit_returned_price_policy_continues_to_material_validation(self):
        response = taobao_response()
        response['item']['pcADescUrl'] = 'https://market.m.taobao.com/app/detail-project/desc/index.html?id=123&f=desc/fixture-file'
        response['componentsVO'] = {'priceVO': {'price': {'priceTitle': '优惠前', 'priceActionText': '登录查看更多优惠'}}}
        capture = self.capture(response, {'acceptPrePromotionReturnedPrice': True})
        self.assertEqual(capture['reason'], 'DESCRIPTION_PAGE_REQUIRED')

    def test_returned_price_policy_requires_boolean_opt_in(self):
        response = taobao_response()
        response['componentsVO'] = {'priceVO': {'price': {'priceActionText': '登录查看更多优惠'}}}
        capture = self.capture(response, {'acceptPrePromotionReturnedPrice': 'true'})
        self.assertEqual(capture['reason'], 'EFFECTIVE_PRICE_UNVERIFIED')

    def test_returned_price_policy_does_not_accept_login_page(self):
        capture = self.capture(taobao_response(), {'acceptPrePromotionReturnedPrice': True}, 'https://login.taobao.com/member/login.jhtml')
        self.assertEqual(capture['reason'], 'NOT_A_PRODUCT_PAGE')


if __name__ == '__main__':
    unittest.main()
