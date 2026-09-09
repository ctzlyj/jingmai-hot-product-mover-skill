import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from jd_product_mover import (  # noqa: E402
    base_title,
    build_listing,
    extract_javascript_object,
    infer_prefix,
    expected_source_category_id,
    listing_title,
    normalize_dimension_alias,
    normalize_size,
    ordered_size_aliases,
    parse_detail_urls,
    parse_video_response,
    required_sku_attribute_values,
    safe_sale_alias,
    source_sku_from_url,
    split_style_and_size,
    target_price,
    variant_style_and_size,
)
from jdo_listing_mvp import (  # noqa: E402
    JdoError,
    configure_jdo_profile,
    decode_process_output,
    mark_jdo_store_verified,
    multi_set_manifest_errors,
    run_jdo,
    run_jdo_large,
    sale_attribute_components,
    shared_image_audit,
    validate_manifest,
)
from jd_product_pipeline import (  # noqa: E402
    build_video_binding_body,
    effective_rules,
    existing_video_materials,
    load_batch_preflight_decisions,
    PipelineError,
    record_batch_preflight_skip,
    record_pipeline_exception,
    product_apply_rejection,
    query_video_relations,
    save_state,
    validate_store_config,
    video_is_ready,
    video_relation_audit,
    video_upload_url_params,
)
from taobao_product_mvp import (  # noqa: E402
    build_source,
    detail_image_urls,
    split_source_by_style,
    update_local_video_metadata,
)
from publish_local_draft import (  # noqa: E402
    existing_product_id,
    require_publish_confirmation,
    reuse_split_uploaded_images,
)


class ProductMoverTests(unittest.TestCase):
    def setUp(self):
        rules_path = ROOT / "config" / "modern-decorative-painting.rules.json"
        self.rules = json.loads(rules_path.read_text(encoding="utf-8"))
        configure_jdo_profile("test-profile")

    def test_jdo_output_decoder_preserves_gb18030_chinese(self):
        payload = '{"success":true,"data":{"shopName":"示例店铺"}}'
        self.assertEqual(payload, decode_process_output(payload.encode("gb18030")))

    def test_jdo_subprocess_enables_python_utf8_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "jdo-cli.exe"
            executable.write_bytes(b"")
            completed = type("Completed", (), {
                "stdout": b'{"success":true}',
                "stderr": b"",
            })()
            with patch.dict("os.environ", {"JDO_CLI_PATH": str(executable)}), patch(
                "jdo_listing_mvp.subprocess.run", return_value=completed,
            ) as mocked_run:
                run_jdo(["status"])
        environment = mocked_run.call_args.kwargs["env"]
        self.assertEqual(
            [str(executable), "status", "--profile", "test-profile"],
            mocked_run.call_args.args[0],
        )
        self.assertEqual("1", environment["PYTHONUTF8"])
        self.assertEqual("utf-8", environment["PYTHONIOENCODING"])

    def test_jdo_rejects_missing_or_default_profile(self):
        with self.assertRaisesRegex(JdoError, "明确指定"):
            configure_jdo_profile("")
        with self.assertRaisesRegex(JdoError, "default"):
            configure_jdo_profile("default")

    def test_jdo_write_requires_verified_store_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            body_path = Path(temp_dir) / "body.json"
            body_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(JdoError, "店铺身份"):
                run_jdo_large(
                    url="/rest/example",
                    method="POST",
                    body_path=body_path,
                    skill_id="test-skill",
                    yes=True,
                )

    def test_jdo_write_request_binds_verified_profile(self):
        mark_jdo_store_verified("test-profile", 123, "测试店铺")
        captured_request = {}
        with tempfile.TemporaryDirectory() as temp_dir:
            body_path = Path(temp_dir) / "body.json"
            body_path.write_text("{}", encoding="utf-8")
            completed = type("Completed", (), {
                "stdout": '{"success":true}',
                "stderr": "",
            })()

            def run_bridge(command, **kwargs):
                request_path = Path(command[command.index("--request") + 1])
                captured_request.update(json.loads(request_path.read_text(encoding="utf-8")))
                return completed

            with patch("jdo_listing_mvp.PYTHON312_CANDIDATES", (Path(sys.executable),)), patch(
                "jdo_listing_mvp.ensure_jdo_runtime", return_value=Path(temp_dir) / "runtime.pyc",
            ), patch(
                "jdo_listing_mvp.recheck_verified_jdo_store",
                return_value={"profile": "test-profile", "venderId": 123, "shopName": "测试店铺"},
            ), patch(
                "jdo_listing_mvp.uuid.uuid4",
                return_value=type("Uuid", (), {"hex": "test"})(),
            ), patch(
                "jdo_listing_mvp.JDO_RUNTIME", Path(temp_dir) / "runtime.pyc",
            ), patch(
                "jdo_listing_mvp.subprocess.run", side_effect=run_bridge,
            ) as mocked_run:
                run_jdo_large(
                    url="/rest/example",
                    method="POST",
                    body_path=body_path,
                    skill_id="test-skill",
                    yes=True,
                )
        self.assertEqual("test-profile", captured_request["profile"])
        self.assertTrue(mocked_run.called)

    def test_taobao_source_keeps_only_variable_sales_dimensions_and_real_prices(self):
        response = {
            "item": {"itemId": "1", "title": "自粘贴画", "images": ["//img/main.jpg"], "videos": []},
            "seller": {"sellerId": "2", "shopId": "3", "shopName": "测试店"},
            "skuBase": {
                "props": [
                    {"pid": "color", "name": "颜色分类", "values": [
                        {"vid": "a", "name": "山前有路", "image": "//img/a.jpg"},
                        {"vid": "b", "name": "曙光初现", "image": "//img/b.jpg"},
                    ]},
                    {"pid": "mount", "name": "组合形式", "values": [{"vid": "m", "name": "自带背胶"}]},
                    {"pid": "size", "name": "尺寸", "values": [
                        {"vid": "s", "name": "宽40cm*高60cm"},
                        {"vid": "l", "name": "宽60cmx高90cm"},
                    ]},
                    {"pid": "frame", "name": "外框类型", "values": [{"vid": "n", "name": "无框"}]},
                ],
                "skus": [
                    {"skuId": "11", "propPath": "color:a;mount:m;size:s;frame:n"},
                    {"skuId": "12", "propPath": "color:b;mount:m;size:l;frame:n"},
                ],
            },
            "skuCore": {"sku2info": {
                "11": {"price": {"priceMoney": "780"}, "subPrice": {"priceMoney": "686"}},
                "12": {"price": {"priceMoney": "1500"}, "subPrice": {"priceMoney": "1320"}},
            }},
        }
        source = build_source(response, {"images": []}, "https://item.taobao.com/item.htm?id=1&skuId=11")
        self.assertEqual({"color": "颜色", "size": "尺寸"}, source["saleProperties"])
        self.assertEqual(["6.86", "13.20"], [row["sourcePrice"] for row in source["variants"]])
        self.assertEqual({"组合形式": ["自带背胶"], "外框类型": ["无框"]}, source["sourceFixedSkuProperties"])

    def test_taobao_detail_images_preserve_dom_order(self):
        payload = {"images": [
            {"attrs": {"data-name": "singleImage", "data-src": "//img/1.jpg"}},
            {"attrs": {"data-name": "singleImage", "data-src": "https://img/2.jpg"}},
            {"attrs": {"src": "chrome-extension://ignored/icon.png"}},
        ]}
        self.assertEqual(["https://img/1.jpg", "https://img/2.jpg"], detail_image_urls(payload))

    def test_taobao_source_splits_balanced_complete_style_groups(self):
        source = {
            "sourceSku": "771157084239",
            "sourceSpu": "771157084239",
            "title": "玄关装饰画",
            "fullTitle": "玄关装饰画",
            "saleProperties": {"color": "颜色", "size": "尺寸"},
            "variants": [
                {
                    "sourceSku": f"{style:02d}-{size:02d}",
                    "sourceAttributes": {
                        "颜色": f"款式{style:02d}",
                        "尺寸": "【定制尺寸】联系客服下单" if size == 10 else f"尺寸{size:02d}",
                    },
                }
                for style in range(1, 51)
                for size in range(1, 11)
            ],
        }

        parts = split_source_by_style(source, 299)

        self.assertEqual([250, 250], [len(part["variants"]) for part in parts])
        self.assertEqual([25, 25], [part["splitInfo"]["styleCount"] for part in parts])
        self.assertEqual(["771157084239-P01", "771157084239-P02"], [part["sourceSpu"] for part in parts])
        self.assertEqual(500, len({row["sourceSku"] for part in parts for row in part["variants"]}))
        self.assertTrue(all(
            row["sourceAttributes"]["尺寸"] != "【定制尺寸】联系客服下单"
            for part in parts for row in part["variants"]
        ))
        self.assertEqual(
            {f"款式{style:02d}" for style in range(1, 26)},
            {row["sourceAttributes"]["颜色"] for row in parts[0]["variants"]},
        )

    def test_taobao_split_without_video_skips_video_metadata(self):
        source = {"video": None}
        self.assertIs(source, update_local_video_metadata(source, Path(".")))

    def test_split_draft_reuses_uploaded_common_images_by_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "123-P01_A组"
            second = root / "123-P02_B组"
            for folder in (first, second):
                (folder / ".pipeline").mkdir(parents=True)
                (folder / "详情图").mkdir()
                (folder / "详情图" / "详情图001.png").write_bytes(b"same-image")
                (folder / "manifest.json").write_text(json.dumps({
                    "sourceParentSku": "123",
                }), encoding="utf-8")
            (first / ".pipeline" / "listing.uploaded.json").write_text(json.dumps({
                "images": [{
                    "localPath": "详情图/详情图001.png",
                    "upload": {"status": "uploaded", "imgId": "1", "imgUrl": "https://img/1"},
                }],
            }), encoding="utf-8")
            listing = {"images": [{
                "localPath": "详情图/详情图001.png",
                "upload": {"status": "pending", "imgId": "", "imgUrl": ""},
            }]}

            reused = reuse_split_uploaded_images(second, {"sourceParentSku": "123"}, listing)

            self.assertEqual(1, reused)
            self.assertEqual("uploaded", listing["images"][0]["upload"]["status"])
            self.assertEqual("1", listing["images"][0]["upload"]["imgId"])

    def test_local_draft_resume_never_searches_or_creates_when_product_id_exists(self):
        with patch("publish_local_draft.pipeline.find_existing_product") as find_existing:
            product_id, existing = existing_product_id({"productId": 10001234567890}, "961562277885")
        self.assertEqual(10001234567890, product_id)
        self.assertIsNone(existing)
        find_existing.assert_not_called()

    def test_local_draft_requires_publish_confirmation_before_any_write(self):
        with self.assertRaisesRegex(PipelineError, "PUBLISH_TO_JD"):
            require_publish_confirmation("")
        require_publish_confirmation("PUBLISH_TO_JD")

    def test_product_apply_rejection_detects_keeper_rejection(self):
        with patch("jd_product_pipeline.jingmai.get") as get:
            get.side_effect = [
                {"data": {"applyInfo": {"draftComputeStatus": 3, "bossAuditStatus": -1}}},
                {"data": [{
                    "productId": 10036061623956,
                    "status": 300,
                    "keeperStatus": 309,
                    "pushStatus": 100,
                }]},
            ]
            rejection = product_apply_rejection(10036061623956)
        self.assertEqual(3, rejection["draftComputeStatus"])
        self.assertEqual(300, rejection["status"])
        self.assertEqual(309, rejection["keeperStatus"])
        self.assertEqual(100, rejection["pushStatus"])

    def test_product_apply_rejection_ignores_non_rejected_apply(self):
        with patch("jd_product_pipeline.jingmai.get") as get:
            get.return_value = {
                "data": {"applyInfo": {"draftComputeStatus": 2, "bossAuditStatus": -1}},
            }
            self.assertIsNone(product_apply_rejection(10036061623956))
        self.assertEqual(1, get.call_count)

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
        self.assertEqual("60×40cm", normalize_size("宽60厘米-高40厘米【送安装工具刮板】"))
        self.assertEqual("60×30cm", normalize_size("宽60厘米---高30厘米"))
        self.assertEqual("8寸", normalize_size("8寸【十条家训】胡桃色相框"))
        self.assertEqual(
            ("【十条家训】胡桃色相框", "8寸"),
            split_style_and_size("8寸【十条家训】胡桃色相框", "默认款式"),
        )

    def test_single_variant_without_size_uses_internal_single_spec(self):
        self.assertEqual(
            ("励志装饰画家规家训", "单规格"),
            variant_style_and_size("励志装饰画家规家训", "默认款式", allow_single_spec=True),
        )
        with self.assertRaisesRegex(Exception, "无法从销售属性拆分款式和尺寸"):
            variant_style_and_size("励志装饰画家规家训", "默认款式", allow_single_spec=False)

    def test_one_dimension_multiple_variants_without_size_keep_each_style(self):
        self.assertEqual(
            ("百福葫芦装饰贴*2张", "单规格"),
            variant_style_and_size("百福葫芦装饰贴*2张", "默认款式", allow_single_spec=True),
        )
        self.assertEqual(
            ("百福葫芦装饰贴*6张", "单规格"),
            variant_style_and_size("百福葫芦装饰贴*6张", "默认款式", allow_single_spec=True),
        )

    def test_non_physical_second_dimension_keeps_source_alias(self):
        self.assertEqual("【懂的都懂】已走流程", normalize_dimension_alias("【懂的都懂】已走流程"))
        self.assertEqual(
            ["第二档", "第一档"],
            ordered_size_aliases(["第二档", "第一档", "第二档"]),
        )

    def test_preflight_accepts_non_physical_size_alias_order(self):
        manifest = {
            "category": {"id": 35395},
            "product": {
                "productKey": "P1",
                "title": "测试商品",
                "brandId": -1,
                "sevenDayReturnCode": "1",
                "freightTemplateId": 1,
                "vendorSkuPrefix": "ZTH",
                "vendorStyleStart": 10000,
            },
            "skus": [{
                "skuKey": "S1",
                "jdPrice": 10,
                "stock": 1,
                "mountingValueId": "1",
                "subjectValueId": "1",
                "styleAlias": "款式一",
                "styleMappingId": "style-1",
                "styleOrder": 1,
                "vendorStyleOrder": 1,
                "sizeAlias": "【懂的都懂】已走流程",
                "sizeMappingId": "size-1",
                "sizeOrder": 1,
                "vendorSizeOrder": 1,
            }],
            "images": [],
        }
        reference = {"category": {"id": 35395}, "brands": [{"brandId": -1}], "componentCatalog": []}
        errors, _ = validate_manifest(Path("listing.json"), manifest, reference)
        self.assertFalse(any("无法识别尺寸" in error for error in errors))

    def test_required_sku_attribute_defaults_are_rule_driven(self):
        components = [{
            "path": "root/SKUS/SkuAttr",
            "required": True,
            "componentCode": "prop-sku-243066",
            "title": "画框材质",
        }]
        rules = {
            "skuAttributes": {
                "requiredDefaults": {
                    "prop-sku-243066": {"valueId": "2195810", "label": "绫布"},
                },
            },
        }
        self.assertEqual(
            [{"componentCode": "prop-sku-243066", "valueId": "2195810", "label": "绫布"}],
            required_sku_attribute_values(components, rules),
        )

    def test_fourth_level_category_can_declare_source_parent_id(self):
        rules = {"category": {"id": 53194, "sourceId": 53154}}
        self.assertEqual(53154, expected_source_category_id(rules))

    def test_sale_alias_preserves_separators_and_reserves_wrapper_length(self):
        value = "【5张装】体验款 衣柜、橱柜、鞋柜、墙面、家电装饰贴"
        alias = safe_sale_alias(value)
        self.assertIn("/", safe_sale_alias("衣/橱柜"))
        self.assertLessEqual(sum(1 if character.isascii() else 2 for character in alias), 48)

    def test_listing_title_removes_source_brand_and_respects_jd_limit(self):
        source = "艺诚轩家规家训挂图家风挂画新中式书法装饰客厅书房背景墙卷轴壁画装饰"
        self.assertEqual(
            "家规家训挂图家风挂画新中式书法装饰客厅书房背景墙卷轴壁画装饰",
            listing_title(source, "艺诚轩"),
        )

    def test_listing_uses_source_platform_product_key_prefix(self):
        source = {
            "sourceUrl": "https://item.taobao.com/item.htm?id=771157084239",
            "sourceSku": "771157084239",
            "sourceSpu": "771157084239",
            "collectedAt": "2026-09-10T00:00:00+08:00",
            "productKeyPrefix": "TB",
            "title": "现代简约装饰画",
            "sourceBrandName": "",
            "categoryIds": [35381],
            "saleProperties": {"color": "颜色"},
            "variants": [{
                "sourceSku": "1",
                "sourcePrice": "10.00",
                "sourceAttributes": {"颜色": "款式一"},
                "skuImage": "主图.jpg",
            }],
            "mainImages": [],
            "detailImages": [],
        }
        reference = {
            "componentCatalog": [{
                "path": "root/SKUS/VenderCategoryValue",
                "componentCode": "sale-prop-1001080021",
                "title": "颜色",
            }],
            "media": {"defaultCategoryId": 1},
        }
        listing = build_listing(source, ROOT, self.rules, reference, 10000)
        self.assertEqual("TB-771157084239", listing["product"]["productKey"])

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

    def test_new_category_online_audit_uses_configured_style_attribute(self):
        manifest = {
            "product": {
                "saleAttributeComponents": {
                    "style": {"componentCode": "sale-prop-1001079909"},
                },
            },
            "skus": [{"styleAlias": "百福葫芦贴6张"}],
            "images": [
                {"role": "spu_main", "order": 1, "upload": {"imgUrl": "spu-first.png"}},
                {"role": "spu_main", "order": 2, "upload": {"imgUrl": "shared.png"}},
            ],
        }
        detail = {
            "skuList": [{
                "saleAttrs": [{
                    "attrId": 1001079909,
                    "attrValueAlias": "百福葫芦贴6张",
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
        for component in components:
            properties = {item["key"]: item["value"] for item in component["properties"]}
            self.assertNotIn("saleAttrValues", properties)

    def test_standard_sale_attribute_includes_configured_value_id(self):
        sku = {
            "styleAlias": "金箔款",
            "styleMappingId": "color-1",
            "styleOrder": 1,
            "sizeAlias": "8cm",
            "sizeMappingId": "size-1",
            "sizeOrder": 1,
        }
        components = sale_attribute_components(
            sku,
            include_size=False,
            definitions={
                "style": {
                    "componentCode": "sale-prop-1001079849",
                    "attrName": "颜色",
                    "valueId": "99986949726",
                },
            },
        )
        properties = {item["key"]: item["value"] for item in components[0]["properties"]}
        self.assertEqual("[99986949726]", properties["saleAttrValues"])

    def test_custom_sale_attribute_can_use_alias_as_value(self):
        sku = {
            "styleAlias": "五张装",
            "styleMappingId": "color-1",
            "styleOrder": 1,
            "sizeAlias": "8cm",
            "sizeMappingId": "size-1",
            "sizeOrder": 1,
        }
        components = sale_attribute_components(
            sku,
            include_size=False,
            definitions={
                "style": {
                    "componentCode": "sale-prop-1001079849",
                    "attrName": "颜色",
                    "useAliasAsValue": True,
                },
            },
        )
        properties = {item["key"]: item["value"] for item in components[0]["properties"]}
        self.assertEqual("[五张装]", properties["saleAttrValues"])

    def test_new_category_uses_reference_sale_codes_and_omits_sku_attr(self):
        sku = {
            "styleAlias": "百福葫芦贴6张",
            "styleMappingId": "color-1",
            "styleOrder": 1,
            "sizeAlias": "单规格",
            "sizeMappingId": "size-1",
            "sizeOrder": 1,
        }
        components = sale_attribute_components(
            sku,
            include_size=False,
            definitions={
                "style": {"componentCode": "sale-prop-1001079909", "attrName": "颜色"},
                "size": {"componentCode": "sale-prop-1001079910", "attrName": "尺码"},
            },
        )
        self.assertEqual(["sale-prop-1001079909"], [item["componentCode"] for item in components])

    def test_new_category_preflight_uses_matching_reference(self):
        manifest = {"category": {"id": 35395}, "product": {"mainImageMode": "multi_set"}}
        matching_errors, _ = validate_manifest(
            Path("listing.json"), manifest, {"category": {"id": 35395}, "brands": [], "componentCatalog": []}
        )
        mismatched_errors, _ = validate_manifest(
            Path("listing.json"), manifest, {"category": {"id": 35381}, "brands": [], "componentCatalog": []}
        )
        self.assertFalse(any("类目与实时Schema不一致" in error for error in matching_errors))
        self.assertTrue(any("类目与实时Schema不一致" in error for error in mismatched_errors))

    def test_no_brand_can_reach_platform_validation_when_reference_page_omits_it(self):
        manifest = {"category": {"id": 35395}, "product": {"brandId": -1}}
        errors, _ = validate_manifest(
            Path("listing.json"), manifest, {"category": {"id": 35395}, "brands": [], "componentCatalog": []},
        )
        self.assertNotIn("品牌ID不在当前店铺可选品牌列表中", errors)

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

    def test_existing_video_materials_queries_at_most_fifty_skus_per_request(self):
        sku_ids = list(range(1, 163))

        def response_for_batch(_skill_id, _path, params):
            return {
                "data": [
                    {
                        "skuId": sku_id,
                        "materials": [
                            {"materialType": 42, "videoMaterial": {"videoId": sku_id}},
                            {"materialType": 1},
                        ],
                    }
                    for sku_id in params["skuIds"]
                ],
            }

        with patch("jd_product_pipeline.jingmai.get", side_effect=response_for_batch) as get:
            materials = existing_video_materials(sku_ids)

        batch_sizes = [len(call.args[2]["skuIds"]) for call in get.call_args_list]
        queried_skus = [sku_id for call in get.call_args_list for sku_id in call.args[2]["skuIds"]]
        self.assertEqual([50, 50, 50, 12], batch_sizes)
        self.assertEqual(sku_ids, queried_skus)
        self.assertEqual({"materialType": 42, "videoMaterial": {"videoId": 162}}, materials[162][0])

    def test_query_video_relations_reads_every_page(self):
        def response_for_page(_skill_id, _path, params):
            start = (params["page"] - 1) * params["pageSize"]
            rows = [{"skuId": sku_id, "videoId": 9, "relationStatus": 4} for sku_id in range(start + 1, min(start + 51, 163))]
            return {
                "data": rows,
                "paginationData": {"currentPage": params["page"], "pageSize": 50, "totalItems": 162},
            }

        with patch("jd_product_pipeline.jingmai.get", side_effect=response_for_page) as get:
            relations = query_video_relations("video_1", 9)

        self.assertEqual(162, len(relations))
        self.assertEqual([1, 2, 3, 4], [call.args[2]["page"] for call in get.call_args_list])

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
            "store": {"profile": "test-profile", "venderId": 123, "shopName": "测试店铺"},
            "business": {"freightTemplateId": 456, "freightTemplateLabel": "测试模板"},
        }
        validate_store_config(store_config, "test-profile")
        rules = effective_rules(self.rules, store_config)
        self.assertEqual(456, rules["productDefaults"]["freightTemplateId"])
        self.assertEqual("测试模板", rules["productDefaults"]["freightTemplateLabel"])

    def test_store_config_rejects_profile_mismatch(self):
        store_config = {
            "store": {"profile": "store-alpha", "venderId": 123, "shopName": "测试店铺"},
            "business": {"freightTemplateId": 456, "freightTemplateLabel": "测试模板"},
        }
        with self.assertRaisesRegex(ValueError, "Profile"):
            validate_store_config(store_config, "store-beta")

    def test_store_config_rejects_unconfigured_placeholders(self):
        with self.assertRaisesRegex(ValueError, "venderId"):
            validate_store_config({
                "store": {"venderId": 0, "shopName": "YOUR_SHOP_NAME"},
                "business": {"freightTemplateId": 0, "freightTemplateLabel": ""},
            })

    def test_successful_state_clears_stale_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                '{"stage":"FAILED_LOCAL_VALIDATION","error":"old failure",'
                '"errors":["old validation failure"]}',
                encoding="utf-8",
            )
            state = save_state(path, "VERIFIED")
            self.assertNotIn("error", state)
            self.assertNotIn("errors", state)

    def test_resuming_a_skipped_item_clears_stale_skip_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({
                "stage": "SKIPPED_OUT_OF_SCOPE_CATEGORY",
                "categoryDecision": {"reason": "old category decision"},
                "preflightDecision": {"decision": "SKIPPED_OUT_OF_SCOPE_CATEGORY"},
                "duplicateReview": {"decision": "SKIPPED_AI_DUPLICATE"},
            }), encoding="utf-8")
            state = save_state(path, "RECEIVED", sourceSku="10233624202855")
            self.assertNotIn("categoryDecision", state)
            self.assertNotIn("preflightDecision", state)
            self.assertNotIn("duplicateReview", state)

    def test_propagation_pending_state_survives_outer_exception_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(
                path,
                "PRODUCT_PROPAGATION_PENDING",
                productId=10036059566777,
                verification={"ok": False, "errors": ["300 未查询到商品信息"]},
            )
            state = record_pipeline_exception(path, RuntimeError("300 未查询到商品信息"))
            self.assertEqual("PRODUCT_PROPAGATION_PENDING", state["stage"])
            self.assertEqual(10036059566777, state["productId"])
            self.assertNotIn("error", state)

    def test_product_apply_rejected_state_survives_outer_exception_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(
                path,
                "PRODUCT_APPLY_REJECTED",
                productId=10036061623956,
                productApply={"draftComputeStatus": 3, "keeperStatus": 309},
            )
            state = record_pipeline_exception(path, RuntimeError("商品申请已驳回"))
            self.assertEqual("PRODUCT_APPLY_REJECTED", state["stage"])
            self.assertEqual(10036061623956, state["productId"])
            self.assertNotIn("error", state)

    def test_batch_preflight_requires_a_decision_for_every_link(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.json"
            path.write_text(json.dumps({"decisions": [{
                "sourceSku": "10233624202855",
                "decision": "PROCEED",
                "reason": "AI审阅后未发现重复",
            }]}, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(PipelineError, "缺少 AI/类目预检决策"):
                load_batch_preflight_decisions(path, [
                    "https://item.jd.com/10233624202855.html",
                    "https://item.jd.com/10229820086994.html",
                ])

    def test_batch_preflight_accepts_category_and_ai_skip_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.json"
            path.write_text(json.dumps({
                "categorySkips": [{
                    "sourceSku": "10233624202855",
                    "stage": "SKIPPED_OUT_OF_SCOPE_CATEGORY",
                    "reason": "不是目标类目",
                }],
                "aiReviews": [{
                    "sourceSku": "10163162235672",
                    "decision": "SKIPPED_AI_DUPLICATE",
                    "reason": "款式和主图均一致",
                    "matchedProductId": 10035761384498,
                }],
            }, ensure_ascii=False), encoding="utf-8")
            decisions = load_batch_preflight_decisions(path, [
                "https://item.jd.com/10233624202855.html",
                "https://item.jd.com/10163162235672.html",
            ])
            self.assertEqual("SKIPPED_OUT_OF_SCOPE_CATEGORY", decisions["10233624202855"]["decision"])
            self.assertEqual("SKIPPED_AI_DUPLICATE", decisions["10163162235672"]["decision"])

    def test_batch_preflight_skip_persists_state_without_product_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            result = record_batch_preflight_skip(
                url="https://item.jd.com/10233624202855.html",
                output_root=Path(directory),
                decision={
                    "decision": "SKIPPED_OUT_OF_SCOPE_CATEGORY",
                    "reason": "不是目标类目",
                },
            )
            state = json.loads(Path(result["state"]).read_text(encoding="utf-8"))
            self.assertTrue(result["skipped"])
            self.assertEqual("SKIPPED_OUT_OF_SCOPE_CATEGORY", state["stage"])
            self.assertNotIn("productId", state)

    def test_ai_duplicate_review_has_structured_decision_contract(self):
        schema_path = ROOT / "references" / "ai-duplicate-review.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            ["PROCEED", "SKIPPED_AI_DUPLICATE"],
            schema["properties"]["decision"]["enum"],
        )
        self.assertTrue({
            "sourceSku",
            "candidateTitle",
            "candidateCategory",
            "decision",
            "matchType",
            "reason",
            "confidence",
            "evidence",
        }.issubset(schema["required"]))
        self.assertTrue({
            "coreMeaning",
            "patternAndStyle",
            "formAndMaterial",
            "useScenario",
        }.issubset(schema["properties"]["evidence"]["required"]))


if __name__ == "__main__":
    unittest.main()
