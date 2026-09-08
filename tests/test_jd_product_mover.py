import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from jd_product_mover import (  # noqa: E402
    base_title,
    extract_javascript_object,
    infer_prefix,
    listing_title,
    normalize_size,
    parse_detail_urls,
    parse_video_response,
    source_sku_from_url,
    split_style_and_size,
    target_price,
)
from jdo_listing_mvp import (  # noqa: E402
    multi_set_manifest_errors,
    sale_attribute_components,
    shared_image_audit,
)
from jd_product_pipeline import (  # noqa: E402
    build_video_binding_body,
    effective_rules,
    validate_store_config,
    video_is_ready,
    video_relation_audit,
    video_upload_url_params,
)


class ProductMoverTests(unittest.TestCase):
    def setUp(self):
        rules_path = ROOT / "config" / "modern-decorative-painting.rules.json"
        self.rules = json.loads(rules_path.read_text(encoding="utf-8"))

    def test_extract_javascript_object_handles_strings_and_trailing_commas(self):
        page = 'window._itemOnly = ({"text":"}); still text","rows":[1,2,],}); next();'
        expected = {"text": "}); still text", "rows": [1, 2]}
        self.assertEqual(expected, extract_javascript_object(page, "_itemOnly"))

    def test_source_sku_from_url(self):
        url = "https://item.jd.com/10233795487253.html?x=1&amp;y=2"
        self.assertEqual("10233795487253", source_sku_from_url(url))

    def test_detail_image_order_and_deduplication(self):
        payload = 'showdesc({"content":"<img data-lazyload=\\"//img/a.jpg\\"><img src=\\"https://img/b.jpg\\"><img src=\\"//img/a.jpg\\">"})'
        self.assertEqual(["https://img/a.jpg", "https://img/b.jpg"], parse_detail_urls(payload))

    def test_title_and_size_normalization(self):
        full = "山水挂画 50*100cm（适合客厅）"
        self.assertEqual("山水挂画", base_title(full, ["50*100cm（适合客厅）"]))
        self.assertEqual("50×100cm", normalize_size("宽50厘米x高100厘米"))
        self.assertEqual("8寸", normalize_size("8寸【十条家训】胡桃色相框"))
        self.assertEqual(
            ("【十条家训】胡桃色相框", "8寸"),
            split_style_and_size("8寸【十条家训】胡桃色相框", "默认款式"),
        )

    def test_listing_title_removes_source_brand_and_respects_jd_limit(self):
        source = "艺诚轩家规家训挂图家风挂画新中式书法装饰客厅书房背景墙卷轴壁画装饰"
        self.assertEqual(
            "家规家训挂图家风挂画新中式书法装饰客厅书房背景墙卷轴壁画装饰",
            listing_title(source, "艺诚轩"),
        )

    def test_video_response_prefers_high_quality(self):
        payload = 'videoUrlCB({"errcode":0,"result":{"data":[{"sharpness":"low","main_url":"low.mp4"},{"sharpness":"high","main_url":"high.mp4"},{"sharpness":"yuanhua","main_url":"source.mp4"}],"imageUrls":["cover.jpg"],"videoDuration":53,"videoSize":82}});'
        video = parse_video_response(payload)
        self.assertEqual("high.mp4", video["downloadUrl"])
        self.assertEqual("cover.jpg", video["coverUrl"])
        self.assertEqual(53, video["durationSeconds"])

    def test_prefix_uses_one_shared_category_configuration(self):
        self.assertEqual("ZTH", infer_prefix("客厅自粘墙贴画", self.rules))
        self.assertEqual("GZH", infer_prefix("中式卷轴挂画", self.rules))

    def test_target_price_matches_business_formula(self):
        self.assertEqual("0.10", self.rules["pricing"]["belowCompetitor"])
        self.assertEqual("0.15", self.rules["pricing"]["nationalSubsidyRate"])
        self.assertEqual("0.15", self.rules["pricing"]["activityDiscountRate"])
        self.assertEqual("0", self.rules["pricing"]["coupon"])
        self.assertEqual(
            "merchant_batch_enrollment_after_publish",
            self.rules["pricing"]["discountExecution"],
        )
        jd_price = target_price("59.00", self.rules["pricing"])
        arrival = (jd_price * Decimal("0.85") * Decimal("0.85")).quantize(Decimal("0.01"))
        self.assertEqual(Decimal("81.52"), jd_price)
        self.assertEqual(Decimal("58.90"), arrival)
        self.assertEqual(Decimal("59.00") - Decimal("0.10"), arrival)

    def test_modern_decorative_painting_requires_multi_set_main_images(self):
        manifest = {
            "product": {"mainImageMode": "single_set"},
            "skus": [],
            "images": [],
        }
        self.assertIn("必须使用多套主图模式", multi_set_manifest_errors(manifest)[0])

    def test_sizes_in_same_style_must_share_first_image(self):
        manifest = {
            "product": {"mainImageMode": "multi_set"},
            "skus": [
                {"skuKey": "SKU-A", "styleAlias": "喜居宝地"},
                {"skuKey": "SKU-B", "styleAlias": "喜居宝地"},
            ],
            "images": [
                {"role": "sku_main", "skuKey": "SKU-A", "localPath": "a.png"},
                {"role": "sku_main", "skuKey": "SKU-B", "localPath": "b.png"},
            ],
        }
        errors = multi_set_manifest_errors(manifest)
        self.assertEqual(1, len(errors))
        self.assertIn("同一款式的不同尺寸必须共用同一套主图", errors[0])

    def test_online_audit_requires_style_image_group(self):
        manifest = {
            "skus": [{"styleAlias": "喜居宝地"}],
            "images": [
                {"role": "spu_main", "order": 1, "upload": {"imgUrl": "spu-first.png"}},
                {"role": "spu_main", "order": 2, "upload": {"imgUrl": "shared.png"}},
            ],
        }
        detail = {
            "skuList": [{
                "saleAttrs": [{
                    "attrId": 1001080021,
                    "attrValueAlias": "喜居宝地",
                    "attrValues": "color-1",
                }],
            }],
            "material": {
                "mainImages": [{
                    "uuid": "color-1",
                    "imageInfoList": [
                        {"orderSort": 1, "imgUrl": "style-first.png"},
                        {"orderSort": 2, "imgUrl": "shared.png"},
                    ],
                }],
            },
        }
        audit = shared_image_audit(manifest, detail)
        self.assertTrue(audit["ok"])
        self.assertEqual("multi_set", audit["mode"])

    def test_single_source_dimension_does_not_publish_size_attribute(self):
        sku = {
            "styleAlias": "传承家风【家规】50*100cm 送挂钩",
            "styleMappingId": "color-1",
            "styleOrder": 1,
            "sizeAlias": "50×100cm",
            "sizeMappingId": "size-1",
            "sizeOrder": 1,
        }
        components = sale_attribute_components(sku, include_size=False)
        self.assertEqual(["sale-prop-1001080021"], [item["componentCode"] for item in components])

    def test_two_source_dimensions_keep_size_attribute(self):
        sku = {
            "styleAlias": "家规",
            "styleMappingId": "color-1",
            "styleOrder": 1,
            "sizeAlias": "50×100cm",
            "sizeMappingId": "size-1",
            "sizeOrder": 1,
        }
        components = sale_attribute_components(sku, include_size=True)
        self.assertEqual(
            ["sale-prop-1001080021", "sale-prop-1001080022"],
            [item["componentCode"] for item in components],
        )

    def test_video_upload_params_omit_false_breakpoint_flag(self):
        params = video_upload_url_params(Path("main.mp4"), 33203604)
        self.assertEqual(1, params["videoType"])
        self.assertNotIn("breakPointFlag", params)
        self.assertNotIn("jdVideoId", params)

    def test_video_binding_body_contains_one_sku(self):
        body = build_video_binding_body(
            product_id=1001,
            sku_id=2001,
            video={
                "videoId": 3001,
                "jdVideoId": 4001,
                "videoName": "商品展示视频",
                "duration": 53,
                "indexImage": "https://img.example/cover.jpg",
            },
            tag={"tagId": 1383, "tagName": "产品展示", "sort": 1},
        )
        self.assertIsInstance(body, dict)
        self.assertEqual(1, len(body["skuMaterials"]))
        row = body["skuMaterials"][0]
        self.assertEqual(2001, row["skuId"])
        self.assertEqual(42, row["materials"][0]["materialType"])

    def test_video_relation_audit_requires_every_sku_approved(self):
        relations = [
            {"skuId": 1, "videoId": 9, "relationStatus": 4},
            {"skuId": 2, "videoId": 9, "relationStatus": 3},
        ]
        audit = video_relation_audit(relations, [1, 2], 9)
        self.assertFalse(audit["allApproved"])
        self.assertEqual([2], audit["pendingSkuIds"])

    def test_approved_video_is_reusable_after_transcode(self):
        self.assertTrue(video_is_ready(2))
        self.assertTrue(video_is_ready(4))
        self.assertFalse(video_is_ready(1))
        self.assertFalse(video_is_ready(5))

    def test_store_config_controls_identity_and_freight_template(self):
        store_config = {
            "store": {"venderId": 123, "shopName": "测试店铺"},
            "business": {"freightTemplateId": 456, "freightTemplateLabel": "测试模板"},
        }
        validate_store_config(store_config)
        rules = effective_rules(self.rules, store_config)
        self.assertEqual(456, rules["productDefaults"]["freightTemplateId"])
        self.assertEqual("测试模板", rules["productDefaults"]["freightTemplateLabel"])

    def test_store_config_rejects_unconfigured_placeholders(self):
        with self.assertRaisesRegex(ValueError, "venderId"):
            validate_store_config({
                "store": {"venderId": 0, "shopName": "YOUR_SHOP_NAME"},
                "business": {"freightTemplateId": 0, "freightTemplateLabel": ""},
            })


if __name__ == "__main__":
    unittest.main()
