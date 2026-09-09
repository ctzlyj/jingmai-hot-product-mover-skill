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
    multi_set_manifest_errors,
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
    product_apply_rejection,
    query_video_relations,
    record_batch_preflight_skip,
    record_pipeline_exception,
    save_state,
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
