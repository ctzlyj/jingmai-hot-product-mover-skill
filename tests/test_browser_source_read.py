import copy
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jd_product_mover as mover
import taobao_product_mvp as taobao


def taobao_response():
    return {"item": {"itemId": "123", "title": "test", "images": ["https://img.example/main.jpg"]},
            "seller": {"sellerId": "10", "shopId": "20", "shopName": "test"},
            "skuBase": {"props": [
                {"pid": "color", "name": "颜色分类", "values": [{"vid": "red", "name": "red"}, {"vid": "blue", "name": "blue"}]},
                {"pid": "size", "name": "尺寸", "values": [{"vid": "small", "name": "small"}, {"vid": "large", "name": "large"}]}],
                "skus": [{"skuId": "sku-1", "propPath": "color:red;size:small"}]},
            "skuCore": {"sku2info": {"sku-1": {"price": {"priceMoney": "1234"}}}}}


class BrowserCaptureTests(unittest.TestCase):
    def capture(self):
        return {"ok": True, "schemaVersion": "taobao-browser-capture/v1", "pageUrl": "https://item.taobao.com/item.htm?id=123",
                "response": taobao_response(), "skuCount": 1, "detailImageCount": 1,
                "detailDom": {"images": [{"attrs": {"data-name": "singleImage", "data-src": "https://img.example/detail.jpg"}}]}}

    def test_existing_parser_rejects_wrong_product(self):
        with self.assertRaisesRegex(ValueError, "商品|item"):
            taobao.build_source(taobao_response(), {"images": []}, "https://item.taobao.com/item.htm?id=999")

    def test_existing_parser_rejects_empty_real_sku_set(self):
        response = taobao_response()
        response["skuBase"]["skus"] = []
        with self.assertRaises(ValueError):
            taobao.build_source(response, {"images": []}, "https://item.taobao.com/item.htm?id=123")

    def test_capture_preserves_real_skus_not_cartesian_product(self):
        payload = self.capture()
        response, detail = taobao.load_browser_capture(payload, payload["pageUrl"])
        source = taobao.build_source(response, detail, payload["pageUrl"])
        self.assertEqual(len(source["variants"]), 1)
        self.assertEqual(source["variants"][0]["sourcePrice"], "12.34")
        self.assertEqual(source["detailImageUrls"], ["https://img.example/detail.jpg"])

    def test_capture_rejects_other_page_missing_price_or_missing_details(self):
        original = self.capture()
        for case in ("page", "price", "details", "count"):
            with self.subTest(case=case):
                payload = copy.deepcopy(original)
                if case == "page":
                    payload["pageUrl"] = "https://item.taobao.com/item.htm?id=999"
                elif case == "price":
                    payload["response"]["skuCore"]["sku2info"] = {}
                elif case == "details":
                    payload["detailDom"]["images"] = []
                    payload["detailImageCount"] = 0
                else:
                    payload["skuCount"] = 9
                with self.assertRaises(ValueError):
                    taobao.load_browser_capture(payload, original["pageUrl"])

    def test_wrong_capture_stops_before_any_shop_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture.json"
            review = root / "review.json"
            capture.write_text(json.dumps(self.capture()), encoding="utf-8")
            review.write_text('{"decision":"PROCEED"}', encoding="utf-8")
            arguments = ["tool", "--capture", str(capture), "--duplicate-review", str(review),
                         "--source-url", "https://item.taobao.com/item.htm?id=999", "--profile", "test"]
            with patch.object(sys, "argv", arguments), patch.object(taobao.pipeline, "verify_store_identity") as verify:
                with self.assertRaises(ValueError):
                    taobao.main()
                verify.assert_not_called()

    def test_capture_rejects_duplicate_real_sku_ids(self):
        payload = self.capture()
        payload["response"]["skuBase"]["skus"] *= 2
        payload["skuCount"] = 2
        with self.assertRaises(ValueError):
            taobao.load_browser_capture(payload, payload["pageUrl"])


class SourcePermissionTests(unittest.TestCase):
    def test_video_permission_denial_does_not_trigger_browser(self):
        source = {"item": {}, "product": {"popPid": "test", "skuName": "test"}, "price": "1", "mainVideoId": "wanted"}
        client = Mock()
        with patch.object(mover, "parse_mobile_page", return_value=source), patch.object(mover, "parse_detail_urls", return_value=[]), patch.object(mover, "fetch_video_info", side_effect=mover.SourceAccessDenied("permission denied")), patch.object(mover, "fetch_video_info_with_browser") as browser:
            with self.assertRaises(mover.SourceAccessDenied):
                mover.collect_source("https://item.jd.com/12345678.html", client)
            browser.assert_not_called()

    def test_http_denial_is_not_retried_or_converted_to_browser_fallback(self):
        client = mover.HttpClient(retries=4)
        response = mover.requests.Response()
        response.status_code = 403
        with patch.object(client.session, "get", return_value=response) as request, patch.object(mover.time, "sleep"):
            with self.assertRaises(mover.SourceAccessDenied):
                client.get_bytes("https://api.m.jd.com/ware/image/getPlayUrl")
            request.assert_called_once()

    def test_jsonp_authentication_denial_is_distinct_from_missing_video(self):
        with self.assertRaises(mover.SourceAccessDenied):
            mover.parse_video_response('videoUrlCB({"errcode":403,"errmsg":"login required"})')


class OwnedBrowserTests(unittest.TestCase):
    def setUp(self):
        self.browser = importlib.import_module("browser_source_read")
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.router = self.root / "toolbox.py"
        self.router.write_text("", encoding="utf-8")
        self.calls = []

    def run_cli(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if "open" in command:
            config = json.loads(Path(command[command.index("--config") + 1]).read_text(encoding="utf-8"))
            self.assertTrue(config["browser"]["isolated"])
            self.assertTrue(config["browser"]["launchOptions"]["headless"])
            self.assertFalse(config["extension"])
            self.assertNotIn("userDataDir", config["browser"])
            self.assertNotIn("cdpEndpoint", config["browser"])
        result = {"ok": True, "videoId": "wanted", "text": "videoUrlCB({})"}
        return subprocess.CompletedProcess(command, 0, json.dumps(result) if "run-code" in command else "ok", "")

    def test_owned_session_uses_router_native_arguments_and_cleans_up_same_session(self):
        with patch.object(self.browser.subprocess, "run", side_effect=self.run_cli), patch.dict(os.environ, {"PLAYWRIGHT_MCP_CDP_ENDPOINT": "http://working-browser", "NODE_OPTIONS": "inherited"}):
            result = self.browser.run_read_script("async page => ({ok: true})", toolbox=self.router)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.calls), 3)
        sessions = [next(part for part in command if part.startswith("-s=")) for command, _ in self.calls]
        self.assertEqual(len(set(sessions)), 1)
        self.assertIn("close", self.calls[-1][0])
        for command, options in self.calls:
            self.assertIn(str(self.router), command)
            self.assertNotIn("npx", command)
            self.assertFalse(options.get("shell", False))
            self.assertNotIn("PLAYWRIGHT_MCP_CDP_ENDPOINT", options["env"])
            self.assertNotIn("NODE_OPTIONS", options["env"])

    def test_timeout_does_not_replay_or_mask_original_error(self):
        def timeout(command, **kwargs):
            if "run-code" in command:
                self.calls.append((command, kwargs))
                raise subprocess.TimeoutExpired(command, 75)
            return self.run_cli(command, **kwargs)
        with patch.object(self.browser.subprocess, "run", side_effect=timeout):
            with self.assertRaises(self.browser.BrowserReadError):
                self.browser.run_read_script("async page => ({})", toolbox=self.router)
        self.assertEqual(sum("run-code" in command for command, _ in self.calls), 1)
        self.assertIn("close", self.calls[-1][0])

    def test_browser_failure_is_not_reported_as_valid_data(self):
        def failure(command, **kwargs):
            if "run-code" in command:
                return subprocess.CompletedProcess(command, 0, '{"ok":false,"reason":"LOGIN_REQUIRED"}', "")
            return self.run_cli(command, **kwargs)
        with patch.object(self.browser.subprocess, "run", side_effect=failure):
            with self.assertRaises(self.browser.BrowserReadError):
                self.browser.run_read_script("async page => ({})", toolbox=self.router)

    def test_missing_router_does_not_install_or_launch_anything(self):
        with patch.object(self.browser.subprocess, "run") as execute:
            with self.assertRaises(self.browser.BrowserReadError):
                self.browser.run_read_script("async page => ({})", toolbox=self.root / "missing.py")
            execute.assert_not_called()

    def test_unconfirmed_cleanup_does_not_silently_report_success(self):
        def failure(command, **kwargs):
            if "close" in command:
                raise subprocess.TimeoutExpired(command, 30)
            return self.run_cli(command, **kwargs)
        with patch.object(self.browser.subprocess, "run", side_effect=failure):
            with self.assertRaisesRegex(self.browser.BrowserReadError, "cleanup not confirmed"):
                self.browser.run_read_script("async page => ({})", toolbox=self.router)

    def test_video_reader_rejects_wrong_video_and_non_jd_url(self):
        with patch.object(self.browser, "run_read_script", return_value={"ok": True, "videoId": "different", "text": "wrong"}) as read:
            with self.assertRaises(self.browser.BrowserReadError):
                self.browser.read_jd_video("https://item.m.jd.com/product/12345678.html", "wanted")
            read.reset_mock()
            with self.assertRaises(self.browser.BrowserReadError):
                self.browser.read_jd_video("https://unrelated.example/item", "wanted")
            read.assert_not_called()

    def test_mover_uses_browser_reader_without_changing_video_parser(self):
        payload = 'videoUrlCB({"result":{"data":[{"sharpness":"high","main_url":"https://video.example/high.mp4"}]}})'
        with patch.object(self.browser, "read_jd_video", return_value=payload):
            result = mover.fetch_video_info_with_browser("https://item.m.jd.com/product/12345678.html", "wanted")
        self.assertEqual(result["sourceVideoId"], "wanted")
        self.assertEqual(result["downloadUrl"], "https://video.example/high.mp4")
        self.assertEqual(result["resolutionMethod"], "browser-playwright")


if __name__ == "__main__":
    unittest.main()
