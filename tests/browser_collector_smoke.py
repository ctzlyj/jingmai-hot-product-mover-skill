import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from browser_source_read import run_read_script
from jd_product_mover import parse_video_response
from taobao_product_mvp import load_browser_capture, build_source
from test_browser_source_read import taobao_response


class BrowserCollectorSmokeTests(unittest.TestCase):
    def test_linked_taobao_description_waits_for_hydration(self):
        response = taobao_response()
        response["item"]["pcADescUrl"] = "https://market.m.taobao.com/app/detail-project/desc/index.html?id=123&descVersion=7"
        response["skuBase"]["props"] = response["skuBase"]["props"][:1]
        response["skuBase"]["props"][0]["name"] = "颜色"
        response["skuBase"]["skus"][0]["propPath"] = "color:red"
        context = {"loaderData": {"home": {"data": {"res": response}}}}
        html = '<html><body><script>window.__ICE_APP_CONTEXT__=' + json.dumps(context) + ';</script></body></html>'
        module = (ROOT / "scripts/browser_collectors/taobao_read.mjs").as_uri()
        script = r'''async page => {
          const {readTaobaoProduct} = await import(__MODULE__);
          await page.context().route('**/*', async route => {
            const url = new URL(route.request().url());
            if (url.hostname === 'item.taobao.com') {
              await route.fulfill({status:200,contentType:'text/html; charset=utf-8',body:__HTML__});
            } else if (url.hostname === 'market.m.taobao.com') {
              await route.fulfill({status:200,contentType:'text/html',body:'<html><body><script>setTimeout(()=>{const image=document.createElement("img");image.setAttribute("data-name","singleImage");image.setAttribute("data-src","https://img.example/detail.jpg");document.body.appendChild(image);},100);</script></body></html>'});
            } else await route.abort();
          });
          const capture = await readTaobaoProduct(page, 'https://item.taobao.com/item.htm?id=123', {timeout:5000});
          return {...capture, remainingPages:page.context().pages().length};
        }'''.replace("__MODULE__", json.dumps(module)).replace("__HTML__", json.dumps(html))
        provider = Path.home() / ".joycode/skills/jd-taobao-hot-sales"
        if not (provider / "package.json").is_file():
            self.skipTest("Installed Playwright provider required for isolated browser smoke")
        harness = '''import {createRequire} from 'node:module';
          const {chromium} = createRequire(__PACKAGE__)('playwright-core');
          const {resolveChromeExecutable} = await import(__CONFIG__);
          const browser = await chromium.launch({executablePath:resolveChromeExecutable(),headless:true});
          try {
            const context = await browser.newContext();
            const page = await context.newPage();
            console.log(JSON.stringify(await (__SCRIPT__)(page)));
          } finally { await browser.close(); }
        '''.replace("__PACKAGE__", json.dumps((provider / "package.json").as_uri())).replace(
            "__CONFIG__", json.dumps((provider / "lib/config.mjs").as_uri())).replace("__SCRIPT__", script)
        result = subprocess.run(["node", "--input-type=module"], input=harness, text=True, encoding="utf-8",
                                capture_output=True, timeout=45, check=True)
        capture = json.loads(result.stdout)
        response, detail = load_browser_capture(capture, capture["pageUrl"])
        self.assertEqual(capture["detailScope"], "linked-description-page")
        self.assertEqual(capture["remainingPages"], 1)
        self.assertEqual(len(detail["images"]), 1)
        self.assertEqual(build_source(response, detail, capture["pageUrl"])["saleProperties"], {"color": "颜色"})

    def test_jd_video_ignores_earlier_unrelated_response(self):
        requested = {"url": "https://item.m.jd.com/product/12345678.html", "videoId": "wanted"}
        collector = (ROOT / "scripts/browser_collectors/jd_video.js").read_text(encoding="utf-8")
        collector = collector.replace("__MOVER_REQUEST__", json.dumps(requested))
        script = r'''async page => {
          const seen = [];
          await page.route('**/*', async route => {
            const url = route.request().url();
            if (url.startsWith('https://item.m.jd.com/')) {
              const make = id => 'https://api.m.jd.com/ware/image/getPlayUrl?body=' + encodeURIComponent(JSON.stringify({vid:id}));
              const html = '<html><body>fixture<script>fetch(' + JSON.stringify(make('other')) + ');setTimeout(()=>fetch(' + JSON.stringify(make('wanted')) + '),40);</script></body></html>';
              await route.fulfill({status:200,contentType:'text/html',body:html});
            } else if (url.startsWith('https://api.m.jd.com/')) {
              const id = JSON.parse(decodeURIComponent(url.split('body=')[1])).vid;
              seen.push(id);
              await route.fulfill({status:200,contentType:'text/plain',headers:{'access-control-allow-origin':'*'},body:'videoUrlCB(' + JSON.stringify({result:{data:[{sharpness:'high',main_url:'https://video.example/' + id + '.mp4'}]}}) + ')'});
            } else await route.abort();
          });
          const collect = (__COLLECTOR__);
          const result = await collect(page);
          return {...result, fixtureRequests:seen};
        }'''.replace("__COLLECTOR__", collector)
        result = run_read_script(script)
        self.assertEqual(result["fixtureRequests"], ["other", "wanted"])
        self.assertEqual(parse_video_response(result["text"])["downloadUrl"], "https://video.example/wanted.mp4")

    def test_taobao_dom_capture_roundtrip_excludes_unneeded_private_fields(self):
        response = taobao_response()
        response["privateToken"] = "synthetic-do-not-export"
        response["seller"]["privateSession"] = "synthetic-do-not-export"
        response["skuCore"]["sku2info"]["sku-1"]["debugSession"] = "synthetic-do-not-export"
        context = {"loaderData": {"home": {"data": {"res": response}}}}
        html = '<html><body><script>window.__ICE_APP_CONTEXT__=' + json.dumps(context, ensure_ascii=False) + ';</script><img data-name="singleImage" data-src="https://img.example/detail.jpg"></body></html>'
        collector = (ROOT / "scripts/browser_collectors/taobao_capture.js").read_text(encoding="utf-8")
        script = '''async page => {
          await page.route('**/*', route => route.fulfill({status:200,contentType:'text/html; charset=utf-8',body:__HTML__}));
          await page.goto('https://item.taobao.com/item.htm?id=123');
          return await page.evaluate((__COLLECTOR__));
        }'''.replace("__HTML__", json.dumps(html, ensure_ascii=False)).replace("__COLLECTOR__", collector)
        capture = run_read_script(script)
        self.assertNotIn("synthetic-do-not-export", json.dumps(capture))
        parsed, detail = load_browser_capture(capture, "https://item.taobao.com/item.htm?id=123")
        source = build_source(parsed, detail, capture["pageUrl"])
        self.assertEqual(len(source["variants"]), 1)
        self.assertEqual(source["variants"][0]["sourcePrice"], "12.34")
        self.assertEqual(len(source["detailImageUrls"]), 1)


if __name__ == "__main__":
    unittest.main()
