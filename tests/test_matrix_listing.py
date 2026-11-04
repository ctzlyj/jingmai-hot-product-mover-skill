import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import hot_replica_mode as hrm  # noqa: E402
import matrix_listing as mx  # noqa: E402


def img(url, order):
    return {"imgUrl": url, "orderSort": order}


def source_apply():
    return {
        "applyInfo": {"applyId": "900", "draftComputeStatus": 2},
        "material": {
            "mainImages": [
                {"uuid": "0000000000", "imageInfoList": [img("jfs/t1/a/shared-2.jpg", 2), img("jfs/t1/a/shared-1.jpg", 1), img("jfs/t1/a/cert.jpg", 3)]},
                {"uuid": "111", "imageInfoList": [img("jfs/t1/a/red-1.jpg", 1), img("jfs/t1/a/shared-1.jpg", 2)]},
                {"uuid": "222", "imageInfoList": [img("jfs/t1/a/blue-1.jpg", 1)]},
            ],
            "whiteBackGroundImages": [
                {"uuid": "5001", "whiteBackGroundImage": {"imageUrl": "jfs/t1/a/white-red.jpg", "status": 4}},
                {"uuid": "5002", "whiteBackGroundImage": {"imageUrl": "jfs/t1/a/white-blue.jpg", "status": 4}},
            ],
        },
        "productInfo": {
            "productId": 900,
            "productName": "示例洗澡椅老人防滑 加厚款",
            "brandInfo": {"brandId": -1},
            "categoryDetail": {"lastCategoryId": 53207},
            "productDetailDesc": {"desc": "<p>detail</p>", "zhuangBaUseFlag": 0},
            "length": 50, "width": 40, "height": 30, "weight": 4.5,
            "features": [{"key": "is7ToReturn", "value": "1"}, {"key": "isPayFirst", "value": "1"}],
            "goodsAttrInfos": [{"attrId": 11, "values": [{"attrValue": "101"}, {"attrValue": "102"}]}],
            "logisticsInfo": {"transportId": 7788},
        },
        "skuList": [
            {
                "skuId": 5002, "outerId": "SKU-B", "stockNum": 8,
                "priceInfo": {"jdPrice": 99.5}, "skuEnableStatus": 1,
                "features": [{"key": "shortTitle", "value": "蓝色款"}],
                "goodsAttrInfos": [],
                "saleAttrs": [{"attrId": "1001", "attrName": "颜色", "attrValueAlias": "蓝色", "attrValues": "222", "attrValuesSeqNo": 2}],
            },
            {
                "skuId": 5001, "outerId": "SKU-A", "stockNum": 5,
                "priceInfo": {"jdPrice": 88.0}, "skuEnableStatus": 1,
                "features": [],
                "goodsAttrInfos": [{"attrId": 12, "values": [{"attrValue": "201"}]}],
                "saleAttrs": [{"attrId": "1001", "attrName": "颜色", "attrValueAlias": "红色", "attrValues": "111", "attrValuesSeqNo": 1}],
            },
        ],
    }


UPLOADS = {
    "shared-1": "jfs/t1/new/shared-1.jpg",
    "shared-2": "jfs/t1/new/shared-2.jpg",
    "red-1": "jfs/t1/new/red-1.jpg",
    "blue-1": "jfs/t1/new/blue-1.jpg",
}


def component(components, code):
    return next(item for item in components if item["componentCode"] == code)


def sku_component(sku, code):
    return next((item for item in sku["value"] if item["componentCode"] == code), None)


class SourceImageTests(unittest.TestCase):
    def test_unique_urls_in_group_order(self):
        self.assertEqual(
            ["jfs/t1/a/shared-1.jpg", "jfs/t1/a/shared-2.jpg", "jfs/t1/a/cert.jpg", "jfs/t1/a/red-1.jpg", "jfs/t1/a/blue-1.jpg"],
            mx.source_image_urls(source_apply()),
        )

    def test_original_download_uses_imgzone_before_image_and_never_n0(self):
        urls = mx.original_image_urls("jfs/t1/a/x.jpg")
        self.assertEqual("https://img10.360buyimg.com/imgzone/jfs/t1/a/x.jpg", urls[0])
        self.assertTrue(all("/n0/" not in url for url in urls))


class PromptTests(unittest.TestCase):
    def test_prompt_keeps_finalized_dynamic_prompt_verbatim_as_prefix(self):
        variant = {"suffix": "风格：原木暖光。"}
        prompt = mx.build_variant_prompt("示例洗澡椅老人防滑 加厚款", variant, "red-1")
        self.assertTrue(prompt.startswith(hrm.build_dynamic_prompt("示例洗澡椅老人防滑 加厚款") + "风格：原木暖光。"))
        self.assertIn(mx.COMMON_HARD_RULES, prompt)
        self.assertIn("局部特写", mx.COMMON_HARD_RULES)

    def test_suffix_with_own_hard_rules_is_not_duplicated(self):
        prompt = mx.build_variant_prompt("t", {"suffix": "风格。通用硬性要求：自带。"}, "x")
        self.assertEqual(1, prompt.count("通用硬性要求"))

    def test_example_variants_file_is_valid(self):
        root = Path(__file__).resolve().parents[1]
        variants, keep = mx.load_variants(root / "references" / "matrix-variants.example.json")
        self.assertGreaterEqual(len(variants), 5)
        self.assertEqual(set(), keep)

    def test_per_image_text_lock_is_appended_only_to_that_image(self):
        variant = {"suffix": "S。", "textLocks": {"red-1": "「一档高度」「五档高度」"}}
        locked = mx.build_variant_prompt("t", variant, "red-1")
        other = mx.build_variant_prompt("t", variant, "blue-1")
        self.assertIn("画面中只允许出现以下文字", locked)
        self.assertIn("「一档高度」「五档高度」", locked)
        self.assertNotIn("画面中只允许出现以下文字", other)

    def test_only_two_gpt_image_25_models_alternate(self):
        self.assertEqual(("GPT-Image-2.5-Flare-joybuilder", "GPT-Image-2.5-Sunburst-joybuilder"), mx.MODELS)
        self.assertEqual([mx.MODELS[0], mx.MODELS[1], mx.MODELS[0]], [mx.model_order(i)[0] for i in range(3)])


class ListingTests(unittest.TestCase):
    def build(self, **kwargs):
        return mx.build_matrix_listing(source_apply(), "m2", UPLOADS, keep_stems={"cert"}, **kwargs)

    def test_item_number_and_mapping_ids_are_stable_and_new(self):
        self.assertEqual("MXM2-900", mx.matrix_item_number("m2", 900))
        first = mx.matrix_mapping_id(900, "m2", "111")
        self.assertEqual(first, mx.matrix_mapping_id(900, "m2", "111"))
        self.assertNotEqual(first, mx.matrix_mapping_id(900, "m3", "111"))
        self.assertNotEqual("111", first)
        self.assertEqual(32, len(first))

    def test_schema_copies_business_fields_and_replaces_only_images(self):
        components, plan = self.build()
        self.assertEqual("示例洗澡椅老人防滑 加厚款", component(components, "WareTitle")["value"])
        self.assertEqual("MXM2-900", component(components, "WareItemnum")["value"])
        self.assertEqual("7788", component(components, "WareTransport")["value"])
        self.assertEqual("4.5", component(components, "WareWeight")["value"])
        shared = [p["properties"][3]["value"] for p in component(components, "WareSkuImg")["value"]]
        self.assertEqual(["jfs/t1/new/shared-1.jpg", "jfs/t1/new/shared-2.jpg", "jfs/t1/a/cert.jpg"], shared)
        skus = component(components, "SKUS")["value"]
        self.assertEqual(["SKU-A", "SKU-B"], [sku_component(s, "SkuVendorSku")["value"] for s in skus])
        self.assertEqual(["88.0", "99.5"], [sku_component(s, "SkuJdprice")["value"] for s in skus])
        self.assertEqual([5, 8], [sku_component(s, "SkuStockNum")["value"] for s in skus])
        sale = sku_component(skus[0], "VenderCategoryValue")["value"][0]["properties"]
        mapping = next(p["value"] for p in sale if p["key"] == "mappingId")
        self.assertEqual(mx.matrix_mapping_id(900, "m2", "111"), mapping)
        self.assertEqual(["jfs/t1/new/red-1.jpg", "jfs/t1/new/shared-1.jpg"], plan["skus"][0]["mainImages"])
        self.assertEqual("[201]", sku_component(skus[0], "SkuAttr")["value"][0]["properties"][0]["value"])
        self.assertEqual("蓝色款", sku_component(skus[1], "SkuShortTitle")["value"])

    def test_white_background_copied_per_sku_by_default(self):
        components, plan = self.build()
        skus = component(components, "SKUS")["value"]
        white = sku_component(skus[0], "SkuWhiteBackground")["value"][0]["properties"]
        self.assertIn({"key": "imgUrl", "value": "jfs/t1/a/white-red.jpg"}, white)
        self.assertEqual("jfs/t1/a/white-blue.jpg", plan["skus"][1]["whiteBackground"])
        components, _ = self.build(white_background="skip")
        skus = component(components, "SKUS")["value"]
        self.assertIsNone(sku_component(skus[0], "SkuWhiteBackground"))

    def test_missing_upload_for_regenerated_image_stops_build(self):
        uploads = dict(UPLOADS)
        uploads.pop("blue-1")
        with self.assertRaises(mx.MatrixError):
            mx.build_matrix_listing(source_apply(), "m2", uploads, keep_stems={"cert"})


class LedgerTests(unittest.TestCase):
    def test_atomic_save_retries_windows_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "ledger.json"
            real_replace = mx.os.replace
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError(13, "locked")
                return real_replace(src, dst)

            with patch.object(mx.os, "replace", side_effect=flaky):
                mx.save_json_atomic(target, {"a": 1}, sleep=lambda _: None)
            self.assertEqual({"a": 1}, json.loads(target.read_text(encoding="utf-8")))
            self.assertEqual(3, calls["n"])

    def test_rebuild_ledger_recovers_images_already_on_disk(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            Image.new("RGB", (8, 8)).save(out / "red-1.jpg", "JPEG")
            (out / "blue-1.jpg").write_bytes(b"broken")
            ledger, recovered = mx.rebuild_ledger({}, out, ["red-1", "blue-1", "shared-1"])
            self.assertEqual(["red-1"], recovered)
            self.assertEqual("ok", ledger["red-1"]["status"])
            self.assertNotIn("blue-1", ledger)


class PublishGuardTests(unittest.TestCase):
    def test_submitting_or_published_state_blocks_resubmit(self):
        for state in ({"stage": "SUBMITTING"}, {"stage": "PUBLISHED", "productId": 1}):
            with self.assertRaises(mx.MatrixError):
                mx.publish_guard(state)
        mx.publish_guard({})
        mx.publish_guard({"stage": "PUBLISH_REJECTED"})

    def test_publish_requires_confirmation_before_any_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(mx, "verify_store") as verify:
                with self.assertRaises(SystemExit):
                    mx.main(["publish", "--run", directory, "--variant", "m1", "--profile", "pop-x"])
                verify.assert_not_called()
            self.assertFalse((Path(directory) / "publish").exists())


class ReadbackTests(unittest.TestCase):
    def published(self):
        _, plan = mx.build_matrix_listing(source_apply(), "m2", UPLOADS, keep_stems={"cert"})
        after = source_apply()
        after["applyInfo"]["draftComputeStatus"] = 2
        groups = {"0000000000": plan["shared"]}
        for sku, entry in zip(sorted(after["skuList"], key=lambda s: s["outerId"]), plan["skus"]):
            sku["saleAttrs"][0]["attrValues"] = "new-" + entry["outerId"]
            groups["new-" + entry["outerId"]] = entry["mainImages"]
        after["material"]["mainImages"] = [
            {"uuid": key, "imageInfoList": [img(u, i) for i, u in enumerate(urls, 1)]} for key, urls in groups.items()
        ]
        return plan, after

    def test_clean_readback_has_no_problems(self):
        plan, after = self.published()
        self.assertEqual([], mx.readback_problems(plan, after, source_apply()))

    def test_price_and_image_drift_are_reported(self):
        plan, after = self.published()
        after["skuList"][0]["priceInfo"]["jdPrice"] = 1
        after["material"]["mainImages"][1]["imageInfoList"][0]["imgUrl"] = "jfs/old.jpg"
        problems = mx.readback_problems(plan, after, source_apply())
        self.assertTrue(any(p.startswith("price") for p in problems))
        self.assertTrue(any(p.startswith("images") for p in problems))

    def test_apply_not_ready_while_draft_computing(self):
        self.assertFalse(mx.apply_ready({"applyInfo": {"draftComputeStatus": 1}, "skuList": [{}]}))
        self.assertFalse(mx.apply_ready({"applyInfo": {"draftComputeStatus": 2}, "skuList": []}))
        self.assertTrue(mx.apply_ready({"applyInfo": {"draftComputeStatus": 2}, "skuList": [{}]}))

    def test_source_unchanged_checks_images_and_prices(self):
        before = source_apply()
        after = source_apply()
        self.assertEqual([], mx.source_unchanged(before, after))
        after["skuList"][0]["priceInfo"]["jdPrice"] = 1
        self.assertTrue(mx.source_unchanged(before, after))


class VideoTests(unittest.TestCase):
    def test_reuses_single_approved_source_video_and_its_tag(self):
        material = {"status": 4, "videoMaterial": {"videoId": 77, "tagId": 1383, "tagName": "产品展示", "tagOrder": 1}}
        picked = mx.pick_source_video({1: [material], 2: [material]})
        self.assertEqual({"videoId": 77, "tagId": 1383, "tagName": "产品展示", "sort": 1}, picked)

    def test_mixed_or_unapproved_source_videos_are_not_reused(self):
        a = {"status": 4, "videoMaterial": {"videoId": 77, "tagId": 1, "tagName": "x", "tagOrder": 1}}
        b = {"status": 4, "videoMaterial": {"videoId": 78, "tagId": 1, "tagName": "x", "tagOrder": 1}}
        pending = {"status": 3, "videoMaterial": {"videoId": 77, "tagId": 1, "tagName": "x", "tagOrder": 1}}
        self.assertIsNone(mx.pick_source_video({1: [a], 2: [b]}))
        self.assertIsNone(mx.pick_source_video({1: [pending]}))
        self.assertIsNone(mx.pick_source_video({}))


if __name__ == "__main__":
    unittest.main()
