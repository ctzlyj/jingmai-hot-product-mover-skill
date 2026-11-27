# -*- coding: utf-8 -*-
"""TOP销量SKU聚合草稿模式的假数据回归：不访问网络、不写店铺。"""
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import top_sku_aggregation as tsa  # noqa: E402


def make_source(pid, *, brand=123, category=53181, attrs=None, price="19.90",
                color="黑色", sku_attrs=None, images=6, detail="<p>detail</p>",
                mobile=None, dims=(10, 20, 30, 0.5), transport=7788, promise=99,
                sale_attrs=None):
    attrs = attrs if attrs is not None else [{"attrId": 11, "values": [{"attrValue": "101"}]}]
    sku_attrs = sku_attrs if sku_attrs is not None else [{"attrId": 12, "values": [{"attrValue": "201"}]}]
    main = [{"imgUrl": f"jfs/t1/{pid}/img{i}.jpg", "orderSort": i} for i in range(1, images + 1)]
    if sale_attrs is None:
        sale_attrs = [{"attrId": "1001", "attrName": "颜色", "attrValueAlias": color, "attrValues": "111"}]
    return {
        "productInfo": {
            "productId": int(pid),
            "productName": f"商品{pid}",
            "brandInfo": {"brandId": brand},
            "categoryDetail": {"lastCategoryId": category},
            "productDetailDesc": {"desc": detail, "mobileDesc": mobile or detail},
            "length": dims[0], "width": dims[1], "height": dims[2], "weight": dims[3],
            "features": [{"key": "is7ToReturn", "value": "1"}],
            "goodsAttrInfos": attrs,
            "logisticsInfo": {"transportId": transport, "promiseId": promise},
        },
        "material": {"mainImages": [
            {"uuid": "0000000000", "imageInfoList": main},
        ]},
        "skuList": [{
            "skuId": int(pid) * 10 + 1,
            "priceInfo": {"jdPrice": float(price)},
            "logo": f"jfs/t1/{pid}/logo.jpg",
            "goodsAttrInfos": sku_attrs,
            "saleAttrs": sale_attrs,
        }],
    }


def write_workspace(tmp, sources, *, top_n=2, stock=100, title="聚合测试链接",
                    specs=None, prices=None, extra=None):
    root = Path(tmp)
    pids = list(sources.keys())
    if specs is None:
        specs = [f"规格{i}" for i in range(1, top_n + 1)]
    if prices is None:
        prices = [f"{sources[pid]['skuList'][0]['priceInfo']['jdPrice']:.2f}" for pid in pids[:top_n]]
    top_skus = []
    for index, pid in enumerate(pids[:top_n], 1):
        top_skus.append({
            "n": index, "pid": str(pid),
            "skuId": str(sources[pid]["skuList"][0]["skuId"]),
            "price": prices[index - 1], "spec": specs[index - 1],
        })
    cfg = {
        "venderId": 12345678, "shopName": "测试店", "jdoProfile": "pop-test",
        "categoryId": 53181, "topN": top_n, "title": title, "stock": stock,
        "draftOnly": True, "topSkus": top_skus,
    }
    if extra:
        cfg.update(extra)
    (root / "task_config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "source_applies.json").write_text(
        json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    return root, cfg


def base_config():
    return {
        "venderId": 1, "shopName": "店", "jdoProfile": "pop-test",
        "categoryId": 53181, "topN": 2, "title": "标题", "stock": 10,
        "draftOnly": True,
        "topSkus": [
            {"n": 1, "pid": "1", "skuId": "11", "price": "10.00", "spec": "A"},
            {"n": 2, "pid": "2", "skuId": "22", "price": "20.00", "spec": "B"},
        ],
    }


class FakeJm:
    def __init__(self, *, vender=12345678, shop="测试店", validation=None,
                 create=None, apply_detail=None, products=None, source_applies=None):
        self.vender = vender
        self.shop = shop
        self.validation = validation if validation is not None else {"success": True, "data": {"passFlag": True}}
        self.create = create if create is not None else {"success": True, "data": {"applyId": "9001"}}
        self.apply_detail = apply_detail or {}
        self.products = products or []
        self.source_applies = source_applies if source_applies is not None else {
            "101": make_source(101),
            "102": make_source(102, price="29.90"),
        }
        self.product_query_ids = []
        self.calls = []
        self.profile = None
        self.verified = None
        self.state_at_create = None
        self.state_path = None

    def configure_jdo_profile(self, profile):
        self.profile = profile
        return profile

    def mark_jdo_store_verified(self, profile, vender_id, shop_name):
        self.verified = (profile, vender_id, shop_name)

    def get(self, skill, url, params=None):
        self.calls.append(("get", url, params))
        if "venderinfo" in url:
            return {"data": {"venderId": self.vender, "shopBaseInfo": {"shopName": self.shop}}}
        if "product-apply" in url:
            apply_id = str((params or {}).get("applyId") or "")
            if apply_id in self.source_applies:
                return {"data": self.source_applies[apply_id]}
            return {"data": self.apply_detail}
        if "products" in url:
            if params and "productIdList" in params:
                self.product_query_ids.append(params["productIdList"])
            return {"data": self.products,
                    "paginationData": {"totalItems": len(self.products), "currentPage": 1}}
        return {"data": {}}

    def run_jdo_large(self, *, url, method, body_path, skill_id, params=None, yes=False):
        self.calls.append(("post", url, str(body_path)))
        if "validations" in url:
            return self.validation
        if "product-applies" in url:
            if self.state_path and Path(self.state_path).is_file():
                self.state_at_create = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
            if isinstance(self.create, Exception):
                raise self.create
            return self.create
        return {"success": True, "data": {}}


class ConfigTests(unittest.TestCase):
    def test_requires_required_fields(self):
        with self.assertRaises(tsa.ConfigError):
            tsa.validate_config({"venderId": 1})

    def test_rejects_default_profile(self):
        cfg = base_config()
        cfg["jdoProfile"] = "default"
        with self.assertRaises(tsa.ConfigError):
            tsa.validate_config(cfg)

    def test_rejects_top_n_mismatch(self):
        cfg = base_config()
        cfg["topN"] = 3
        with self.assertRaises(tsa.ConfigError):
            tsa.validate_config(cfg)

    def test_rejects_duplicate_spec(self):
        cfg = base_config()
        cfg["topSkus"][1]["spec"] = "A"
        with self.assertRaises(tsa.ConfigError):
            tsa.validate_config(cfg)

    def test_rejects_duplicate_sku(self):
        cfg = base_config()
        cfg["topSkus"][1]["skuId"] = "11"
        with self.assertRaises(tsa.ConfigError):
            tsa.validate_config(cfg)

    def test_rejects_draft_only_false(self):
        cfg = base_config()
        cfg["draftOnly"] = False
        with self.assertRaises(tsa.ConfigError):
            tsa.validate_config(cfg)

    def test_rejects_spec_with_brackets(self):
        cfg = base_config()
        cfg["topSkus"][0]["spec"] = "A[1]"
        with self.assertRaises(tsa.ConfigError):
            tsa.validate_config(cfg)

    def test_accepts_valid_config(self):
        result = tsa.validate_config(base_config())
        self.assertEqual("pop-test", result["jdoProfile"])
        self.assertEqual("10.00", result["topSkus"][0]["price"])

    def test_collect_mode_allows_empty_top_skus(self):
        cfg = base_config()
        cfg["topSkus"] = []
        result = tsa.validate_config(cfg, require_top_skus=False)
        self.assertEqual([], result["topSkus"])

    def test_collect_mode_allows_empty_title(self):
        cfg = base_config()
        cfg["topSkus"] = []
        cfg["title"] = ""
        result = tsa.validate_config(cfg, require_top_skus=False)
        self.assertEqual("", result["title"])


class RankingTests(unittest.TestCase):
    def test_top_n_uses_order_qty_not_sales_volume(self):
        ranked = [
            {"productId": "1", "skuId": 11, "orderQty30d": 10, "productSales30d": 1000},
            {"productId": "2", "skuId": 21, "orderQty30d": 100, "productSales30d": 10},
        ]
        result = tsa.rank_spus(ranked)
        self.assertEqual(["2", "1"], [item["pid"] for item in result])
        self.assertEqual(100, result[0]["orderQty30d"])

    def test_spu_rank_sums_all_sku_order_qty(self):
        ranked = [
            {"productId": "1", "skuId": 11, "orderQty30d": 60, "productSales30d": 1},
            {"productId": "1", "skuId": 12, "orderQty30d": 60, "productSales30d": 1},
            {"productId": "2", "skuId": 21, "orderQty30d": 100, "productSales30d": 1},
        ]
        result = tsa.rank_spus(ranked)
        self.assertEqual("1", result[0]["pid"])
        self.assertEqual(120, result[0]["orderQty30d"])
        self.assertEqual(11, int(result[0]["best"]["skuId"]))


class BuildTests(unittest.TestCase):
    def test_price_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {"101": make_source(101), "102": make_source(102, price="29.90")}
            root, _ = write_workspace(tmp, sources, prices=["19.90", "19.90"])
            with self.assertRaises(tsa.BuildError):
                tsa.build(root / "task_config.json")

    def test_missing_color_dimension_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {
                "101": make_source(101),
                "102": make_source(102, price="29.90", sale_attrs=[
                    {"attrId": "1002", "attrName": "尺码", "attrValueAlias": "M"}]),
            }
            root, _ = write_workspace(tmp, sources, prices=["19.90", "29.90"])
            with self.assertRaises(tsa.BuildError):
                tsa.build(root / "task_config.json")

    def test_images_below_five_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {"101": make_source(101), "102": make_source(102, price="29.90", images=3)}
            root, _ = write_workspace(tmp, sources, prices=["19.90", "29.90"])
            with self.assertRaises(tsa.BuildError):
                tsa.build(root / "task_config.json")

    def test_brand_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {"101": make_source(101), "102": make_source(102, price="29.90", brand=456)}
            root, _ = write_workspace(tmp, sources, prices=["19.90", "29.90"])
            with self.assertRaises(tsa.ConsistencyError):
                tsa.build(root / "task_config.json")

    def test_category_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {"101": make_source(101), "102": make_source(102, price="29.90", category=53182)}
            root, _ = write_workspace(tmp, sources, prices=["19.90", "29.90"])
            with self.assertRaises(tsa.ConsistencyError):
                tsa.build(root / "task_config.json")

    def test_category_attr_mismatch_requires_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {
                "101": make_source(101),
                "102": make_source(102, price="29.90", attrs=[
                    {"attrId": 11, "values": [{"attrValue": "999"}]}]),
            }
            root, _ = write_workspace(tmp, sources, prices=["19.90", "29.90"])
            with self.assertRaises(tsa.ConsistencyError):
                tsa.build(root / "task_config.json")
            cfg = json.loads((root / "task_config.json").read_text(encoding="utf-8"))
            cfg["consistencyOverride"] = {"confirmed": True, "reason": "用户确认"}
            (root / "task_config.json").write_text(
                json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            result = tsa.build(root / "task_config.json")
            self.assertEqual("BUILT", result["status"])

    def test_build_single_color_dimension(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {"101": make_source(101), "102": make_source(102, price="29.90")}
            root, _ = write_workspace(tmp, sources, prices=["19.90", "29.90"])
            tsa.build(root / "task_config.json")
            body = json.loads((root / "publish_body.json").read_text(encoding="utf-8"))
            components = json.loads(body["schema"])
            skus = next(item for item in components if item["componentCode"] == "SKUS")["value"]
            self.assertEqual(2, len(skus))
            for sku in skus:
                values = next(item for item in sku["value"]
                              if item["componentCode"] == "VenderCategoryValue")["value"]
                self.assertEqual(1, len(values))
                self.assertTrue(values[0]["componentCode"].startswith("sale-prop-"))
                props = {item["key"]: item["value"] for item in values[0]["properties"]}
                self.assertEqual("颜色", props["attrName"])

    def test_build_writes_detail_mapping_and_manual_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {"101": make_source(101), "102": make_source(102, price="29.90")}
            root, _ = write_workspace(tmp, sources, prices=["19.90", "29.90"])
            tsa.build(root / "task_config.json")
            manifest = json.loads((root / "aggregate_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("SPU", manifest["detailMode"])
            self.assertTrue(manifest["skuDetailManualPending"])
            self.assertEqual(["规格1", "规格2"], manifest["skuDetailPendingKeys"])
            self.assertEqual(0, manifest["saveActionType"])
            self.assertTrue(manifest["draftOnly"])
            for sku in manifest["skus"]:
                self.assertTrue((root / sku["detailFile"]).is_file())
                self.assertTrue((root / sku["detailMobileFile"]).is_file())
            mapping = json.loads((root / "sku_detail_mapping.json").read_text(encoding="utf-8"))
            self.assertEqual(2, len(mapping["items"]))
            manual = (root / "sku-detail-manual.md").read_text(encoding="utf-8")
            self.assertIn("京麦客户端", manual)
            self.assertIn("skuList[].productDetailDesc.desc", manual)

    def test_build_checklist_hash_changes_with_stock(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {"101": make_source(101), "102": make_source(102, price="29.90")}
            root, _ = write_workspace(tmp, sources, prices=["19.90", "29.90"])
            first = tsa.build(root / "task_config.json")["checklistHash"]
            cfg = json.loads((root / "task_config.json").read_text(encoding="utf-8"))
            cfg["stock"] = 7
            (root / "task_config.json").write_text(
                json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            second = tsa.build(root / "task_config.json")["checklistHash"]
            self.assertNotEqual(first, second)

    def test_build_does_not_touch_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {"101": make_source(101), "102": make_source(102, price="29.90")}
            root, _ = write_workspace(tmp, sources, prices=["19.90", "29.90"])
            with patch.object(tsa, "load_mover", side_effect=AssertionError("network")):
                result = tsa.build(root / "task_config.json")
            self.assertEqual("BUILT", result["status"])

    def test_build_csv_has_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {"101": make_source(101), "102": make_source(102, price="29.90")}
            root, _ = write_workspace(tmp, sources, prices=["19.90", "29.90"])
            tsa.build(root / "task_config.json")
            data = (root / "新链接对照表.csv").read_bytes()
            self.assertEqual(b"\xef\xbb\xbf", data[:3])

    def test_build_writes_checklist_with_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {"101": make_source(101), "102": make_source(102, price="29.90")}
            root, _ = write_workspace(tmp, sources, prices=["19.90", "29.90"])
            result = tsa.build(root / "task_config.json")
            checklist = (root / "确认清单.md").read_text(encoding="utf-8")
            self.assertIn(result["checklistHash"], checklist)
            self.assertIn("只保存草稿", checklist)
            self.assertIn("TOP1 继承项", checklist)
            self.assertIn("运费模板", checklist)

class SubmitTests(unittest.TestCase):
    def _built(self, tmp, **kwargs):
        sources = {"101": make_source(101), "102": make_source(102, price="29.90")}
        root, cfg = write_workspace(tmp, sources, prices=["19.90", "29.90"], **kwargs)
        result = tsa.build(root / "task_config.json")
        return root, cfg, result

    def test_requires_confirm_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, result = self._built(tmp)
            with patch.object(tsa, "load_mover", return_value=FakeJm()):
                with self.assertRaises(tsa.SubmitBlocked):
                    tsa.submit(root / "task_config.json", "deadbeef")

    def test_blocks_when_apply_id_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, result = self._built(tmp)
            (root / "submit_state.json").write_text(
                json.dumps({"state": "CREATED", "applyId": "9001"}), encoding="utf-8")
            with patch.object(tsa, "load_mover", return_value=FakeJm()):
                with self.assertRaises(tsa.SubmitBlocked):
                    tsa.submit(root / "task_config.json", result["checklistHash"][:8])

    def test_blocks_when_submitting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, result = self._built(tmp)
            (root / "submit_state.json").write_text(
                json.dumps({"state": "SUBMITTING"}), encoding="utf-8")
            with patch.object(tsa, "load_mover", return_value=FakeJm()):
                with self.assertRaises(tsa.SubmitBlocked):
                    tsa.submit(root / "task_config.json", result["checklistHash"][:8])

    def test_blocks_non_draft_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, result = self._built(tmp)
            body = json.loads((root / "publish_body.json").read_text(encoding="utf-8"))
            body["productApplyDTO"]["saveActionType"] = 1
            (root / "publish_body.json").write_text(
                json.dumps(body, ensure_ascii=False), encoding="utf-8")
            with patch.object(tsa, "load_mover", return_value=FakeJm()):
                with self.assertRaises(tsa.SubmitBlocked):
                    tsa.submit(root / "task_config.json", result["checklistHash"][:8])

    def test_validation_failure_leaves_prepared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, result = self._built(tmp)
            fake = FakeJm(validation={"success": False, "data": {"passFlag": False}})
            with patch.object(tsa, "load_mover", return_value=fake):
                out = tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            self.assertEqual("VALIDATION_FAILED", out["status"])
            state = json.loads((root / "submit_state.json").read_text(encoding="utf-8"))
            self.assertEqual("PREPARED", state["state"])
            self.assertFalse(any("product-applies" in call[1] for call in fake.calls))

    def test_submitting_written_before_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, result = self._built(tmp)
            fake = FakeJm()
            fake.state_path = root / "submit_state.json"
            with patch.object(tsa, "load_mover", return_value=fake):
                out = tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            self.assertEqual("CREATED", out["status"])
            self.assertEqual("SUBMITTING", fake.state_at_create["state"])

    def test_created_persists_apply_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, result = self._built(tmp)
            with patch.object(tsa, "load_mover", return_value=FakeJm()):
                out = tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            self.assertEqual("CREATED", out["status"])
            manifest = json.loads((root / "aggregate_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("9001", manifest["applyId"])
            state = json.loads((root / "submit_state.json").read_text(encoding="utf-8"))
            self.assertEqual("CREATED", state["state"])

    def test_unknown_then_reconcile_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, cfg, result = self._built(tmp)
            fake = FakeJm(create=RuntimeError("timeout"))
            with patch.object(tsa, "load_mover", return_value=fake):
                out = tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            self.assertEqual("UNKNOWN", out["status"])
            fake.products = [{
                "productId": 9001, "productName": cfg["title"],
                "productStatus": 105, "created": int(time.time() * 1000),
            }]
            with patch.object(tsa, "load_mover", return_value=fake):
                with self.assertRaises(tsa.SubmitBlocked):
                    tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            state = json.loads((root / "submit_state.json").read_text(encoding="utf-8"))
            self.assertEqual("CREATED", state["state"])
            self.assertEqual("9001", state["applyId"])

    def test_unknown_title_hit_without_time_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, cfg, result = self._built(tmp)
            fake = FakeJm(create=RuntimeError("timeout"))
            with patch.object(tsa, "load_mover", return_value=fake):
                out = tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            self.assertEqual("UNKNOWN", out["status"])
            fake.products = [{
                "productId": 9001, "productName": cfg["title"],
                "productStatus": 105, "created": "not-a-date",
            }]
            with patch.object(tsa, "load_mover", return_value=fake):
                with self.assertRaises(tsa.SubmitBlocked) as ctx:
                    tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            self.assertIn("无法确认", str(ctx.exception))
            state = json.loads((root / "submit_state.json").read_text(encoding="utf-8"))
            self.assertEqual("UNKNOWN", state["state"])

    def test_unknown_not_found_requires_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, result = self._built(tmp)
            fake = FakeJm(create=RuntimeError("timeout"))
            with patch.object(tsa, "load_mover", return_value=fake):
                out = tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            self.assertEqual("UNKNOWN", out["status"])
            fake.create = {"success": True, "data": {"applyId": "9002"}}
            with patch.object(tsa, "load_mover", return_value=fake):
                with self.assertRaises(tsa.SubmitBlocked):
                    tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            with patch.object(tsa, "load_mover", return_value=fake):
                out = tsa.submit(root / "task_config.json", result["checklistHash"][:8], retry=True)
            self.assertEqual("CREATED", out["status"])
            self.assertEqual("9002", out["applyId"])

    def test_submit_blocks_when_source_price_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, result = self._built(tmp)
            fake = FakeJm(source_applies={
                "101": make_source(101, price="21.90"),
                "102": make_source(102, price="29.90"),
            })
            with patch.object(tsa, "load_mover", return_value=fake):
                with self.assertRaises(tsa.SubmitBlocked) as ctx:
                    tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            self.assertIn("价格已变化", str(ctx.exception))
            self.assertFalse(any("product-applies" in call[1] for call in fake.calls))

    def test_submit_proceeds_when_source_price_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, result = self._built(tmp)
            fake = FakeJm()
            with patch.object(tsa, "load_mover", return_value=fake):
                out = tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            self.assertEqual("CREATED", out["status"])

    def test_submitting_reconcile_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, cfg, result = self._built(tmp)
            (root / "submit_state.json").write_text(json.dumps({
                "state": "SUBMITTING", "title": cfg["title"],
                "attemptStartedAtMs": int(time.time() * 1000),
            }), encoding="utf-8")
            fake = FakeJm(products=[{
                "productId": 9001, "productName": cfg["title"],
                "productStatus": 105, "created": int(time.time() * 1000),
            }])
            with patch.object(tsa, "load_mover", return_value=fake):
                with self.assertRaises(tsa.SubmitBlocked) as ctx:
                    tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            self.assertIn("已创建", str(ctx.exception))
            state = json.loads((root / "submit_state.json").read_text(encoding="utf-8"))
            self.assertEqual("CREATED", state["state"])
            self.assertEqual("9001", state["applyId"])
            self.assertFalse(any("product-applies" in call[1] for call in fake.calls))

    def test_submitting_reconcile_not_found_requires_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, cfg, result = self._built(tmp)
            (root / "submit_state.json").write_text(json.dumps({
                "state": "SUBMITTING", "title": cfg["title"],
                "attemptStartedAtMs": int(time.time() * 1000),
            }), encoding="utf-8")
            fake = FakeJm(products=[])
            with patch.object(tsa, "load_mover", return_value=fake):
                with self.assertRaises(tsa.SubmitBlocked):
                    tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            with patch.object(tsa, "load_mover", return_value=fake):
                out = tsa.submit(root / "task_config.json", result["checklistHash"][:8], retry=True)
            self.assertEqual("CREATED", out["status"])

    def test_submitting_reconcile_time_unverified_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, cfg, result = self._built(tmp)
            (root / "submit_state.json").write_text(json.dumps({
                "state": "SUBMITTING", "title": cfg["title"],
                "attemptStartedAtMs": int(time.time() * 1000),
            }), encoding="utf-8")
            fake = FakeJm(products=[{
                "productId": 9001, "productName": cfg["title"],
                "productStatus": 105, "created": "not-a-date",
            }])
            with patch.object(tsa, "load_mover", return_value=fake):
                with self.assertRaises(tsa.SubmitBlocked) as ctx:
                    tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            self.assertIn("无法确认", str(ctx.exception))
            state = json.loads((root / "submit_state.json").read_text(encoding="utf-8"))
            self.assertEqual("SUBMITTING", state["state"])

    def test_reconcile_command_creates_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, cfg, result = self._built(tmp)
            (root / "submit_state.json").write_text(json.dumps({
                "state": "SUBMITTING", "title": cfg["title"],
                "attemptStartedAtMs": int(time.time() * 1000),
            }), encoding="utf-8")
            fake = FakeJm(products=[{
                "productId": 9001, "productName": cfg["title"],
                "productStatus": 105, "created": int(time.time() * 1000),
            }])
            with patch.object(tsa, "load_mover", return_value=fake):
                out = tsa.reconcile_command(root / "task_config.json")
            self.assertEqual("CREATED", out["status"])
            state = json.loads((root / "submit_state.json").read_text(encoding="utf-8"))
            self.assertEqual("CREATED", state["state"])

    def test_reconcile_command_not_found_keeps_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, cfg, result = self._built(tmp)
            (root / "submit_state.json").write_text(json.dumps({
                "state": "SUBMITTING", "title": cfg["title"],
                "attemptStartedAtMs": int(time.time() * 1000),
            }), encoding="utf-8")
            fake = FakeJm(products=[])
            with patch.object(tsa, "load_mover", return_value=fake):
                out = tsa.reconcile_command(root / "task_config.json")
            self.assertEqual("NOT_FOUND", out["status"])
            state = json.loads((root / "submit_state.json").read_text(encoding="utf-8"))
            self.assertEqual("SUBMITTING", state["state"])

    def test_detail_marker_blocks_submit_until_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {"101": make_source(101, detail="<p>淘宝同款</p>"),
                       "102": make_source(102, price="29.90")}
            root, _ = write_workspace(tmp, sources, prices=["19.90", "29.90"])
            result = tsa.build(root / "task_config.json")
            manifest = json.loads((root / "aggregate_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["detailReview"]["markers"])
            fake = FakeJm()
            with patch.object(tsa, "load_mover", return_value=fake):
                with self.assertRaises(tsa.SubmitBlocked):
                    tsa.submit(root / "task_config.json", result["checklistHash"][:8])
            cfg = json.loads((root / "task_config.json").read_text(encoding="utf-8"))
            cfg["review"] = {"detailAccepted": True}
            (root / "task_config.json").write_text(
                json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            result2 = tsa.build(root / "task_config.json")
            with patch.object(tsa, "load_mover", return_value=fake):
                out = tsa.submit(root / "task_config.json", result2["checklistHash"][:8])
            self.assertEqual("CREATED", out["status"])


class VerifyTests(unittest.TestCase):
    def _built_with_apply(self, tmp, *, detail="<p>detail</p>", status=105,
                          skus=None, product_id=9001, apply_id="9001", products=None):
        sources = {"101": make_source(101), "102": make_source(102, price="29.90")}
        root, cfg = write_workspace(tmp, sources, prices=["19.90", "29.90"])
        tsa.build(root / "task_config.json")
        manifest_path = root / "aggregate_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["applyId"] = apply_id
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        if skus is None:
            skus = [
                {"skuId": 1, "saleAttrs": [{"attrName": "颜色", "attrValueAlias": "规格1"}],
                 "priceInfo": {"jdPrice": 19.9}, "productDetailDesc": {"desc": detail}},
                {"skuId": 2, "saleAttrs": [{"attrName": "颜色", "attrValueAlias": "规格2"}],
                 "priceInfo": {"jdPrice": 29.9}, "productDetailDesc": {"desc": detail}},
            ]
        apply_detail = {
            "productInfo": {"productId": product_id, "productStatus": status, "wareStatus": 2},
            "skuList": skus,
            "material": {"mainImages": [
                {"uuid": "0000000000", "imageInfoList": [{"imgUrl": f"x{i}"} for i in range(6)]},
            ]},
        }
        if products is None:
            products = [{"productId": product_id, "productStatus": status}]
        fake = FakeJm(apply_detail=apply_detail, products=products)
        return root, cfg, fake

    def test_verify_reports_pending_sku_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, fake = self._built_with_apply(tmp, detail="")
            with patch.object(tsa, "load_mover", return_value=fake):
                report = tsa.verify(root / "task_config.json")
            self.assertTrue(report["skuDetailManualPending"])
            self.assertEqual(["规格1", "规格2"], sorted(report["skuDetailPendingKeys"]))
            self.assertTrue(report["checks"]["priced"])
            self.assertTrue(report["checks"]["singleColorDimension"])
            self.assertTrue(report["checks"]["multiImage"])
            self.assertFalse(report["checks"]["skuDetailPerSku"])
            self.assertTrue(report["checks"]["draftNotOnSale"])

    def test_verify_all_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, fake = self._built_with_apply(tmp)
            with patch.object(tsa, "load_mover", return_value=fake):
                report = tsa.verify(root / "task_config.json")
            self.assertFalse(report["skuDetailManualPending"])
            self.assertTrue(all(report["checks"].values()))
            self.assertEqual([], report["problems"])

    def test_verify_flags_on_sale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, fake = self._built_with_apply(tmp, status=103)
            with patch.object(tsa, "load_mover", return_value=fake):
                report = tsa.verify(root / "task_config.json")
            self.assertFalse(report["checks"]["draftNotOnSale"])
            self.assertTrue(any("103" in problem for problem in report["problems"]))

    def test_verify_queries_product_id_not_apply_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, fake = self._built_with_apply(tmp, product_id=8001, apply_id="9001")
            with patch.object(tsa, "load_mover", return_value=fake):
                report = tsa.verify(root / "task_config.json")
            self.assertTrue(report["checks"]["draftNotOnSale"])
            self.assertEqual([[8001]], fake.product_query_ids)
            self.assertEqual(8001, report["productId"])

    def test_verify_missing_product_row_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, fake = self._built_with_apply(tmp, products=[])
            with patch.object(tsa, "load_mover", return_value=fake):
                report = tsa.verify(root / "task_config.json")
            self.assertFalse(report["checks"]["draftNotOnSale"])
            self.assertTrue(any("未查到商品状态" in problem for problem in report["problems"]))

    def test_verify_missing_sku_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            skus = [
                {"skuId": 1, "saleAttrs": [{"attrName": "颜色", "attrValueAlias": "规格1"}],
                 "priceInfo": {"jdPrice": 19.9}, "productDetailDesc": {"desc": "<p>detail</p>"}},
            ]
            root, _, fake = self._built_with_apply(tmp, skus=skus)
            with patch.object(tsa, "load_mover", return_value=fake):
                report = tsa.verify(root / "task_config.json")
            self.assertFalse(report["checks"]["skuCountMatches"])
            self.assertTrue(any("SKU 数量不一致" in problem for problem in report["problems"]))
            self.assertTrue(any("缺少规格" in problem for problem in report["problems"]))
            with patch.object(tsa, "load_mover", return_value=fake), redirect_stdout(io.StringIO()):
                code = tsa.main(["verify", "--config", str(root / "task_config.json")])
            self.assertEqual(2, code)

    def test_verify_extra_sku_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            skus = [
                {"skuId": 1, "saleAttrs": [{"attrName": "颜色", "attrValueAlias": "规格1"}],
                 "priceInfo": {"jdPrice": 19.9}, "productDetailDesc": {"desc": "<p>detail</p>"}},
                {"skuId": 2, "saleAttrs": [{"attrName": "颜色", "attrValueAlias": "规格2"}],
                 "priceInfo": {"jdPrice": 29.9}, "productDetailDesc": {"desc": "<p>detail</p>"}},
                {"skuId": 3, "saleAttrs": [{"attrName": "颜色", "attrValueAlias": "规格3"}],
                 "priceInfo": {"jdPrice": 39.9}, "productDetailDesc": {"desc": "<p>detail</p>"}},
            ]
            root, _, fake = self._built_with_apply(tmp, skus=skus)
            with patch.object(tsa, "load_mover", return_value=fake):
                report = tsa.verify(root / "task_config.json")
            self.assertFalse(report["checks"]["skuCountMatches"])
            self.assertTrue(any("多出规格" in problem for problem in report["problems"]))
            with patch.object(tsa, "load_mover", return_value=fake), redirect_stdout(io.StringIO()):
                code = tsa.main(["verify", "--config", str(root / "task_config.json")])
            self.assertEqual(2, code)

    def test_verify_main_exit_code_three_when_only_sku_detail_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, fake = self._built_with_apply(tmp, detail="")
            with patch.object(tsa, "load_mover", return_value=fake), redirect_stdout(io.StringIO()):
                code = tsa.main(["verify", "--config", str(root / "task_config.json")])
            self.assertEqual(3, code)


class ProfileLockTests(unittest.TestCase):
    def test_detects_singleton_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "SingletonLock").write_text("x", encoding="utf-8")
            result = tsa.profile_lock_status(tmp, process_lister=lambda _: [])
            self.assertTrue(result["locked"])
            self.assertIn("关闭", result["advice"])

    def test_missing_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = tsa.profile_lock_status(Path(tmp) / "nope", process_lister=lambda _: [])
            self.assertFalse(result["exists"])
            self.assertFalse(result["locked"])

    def test_process_hit_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = tsa.profile_lock_status(
                tmp, process_lister=lambda _: [{"pid": 1, "commandLineHasProfile": True}])
            self.assertTrue(result["locked"])


class HelperTests(unittest.TestCase):
    def test_parse_created_ms(self):
        self.assertEqual(1_700_000_000_000, tsa.parse_created_ms(1_700_000_000))
        self.assertEqual(1_700_000_000_000, tsa.parse_created_ms("1700000000"))
        self.assertIsNone(tsa.parse_created_ms("not-a-date"))

    def test_confirm_matches_prefix(self):
        self.assertTrue(tsa.confirm_matches("abcdef12", "abcdef123456"))
        self.assertFalse(tsa.confirm_matches("deadbeef", "abcdef123456"))

    def test_title_matches_brand_prefix(self):
        self.assertTrue(tsa.title_matches("品牌A聚合测试链接", "聚合测试链接"))
        self.assertTrue(tsa.title_matches("聚合测试链接", "聚合测试链接"))
        self.assertFalse(tsa.title_matches("其他商品", "聚合测试链接"))


class DocumentationTests(unittest.TestCase):
    def test_skill_documents_mode_and_triggers(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("## TOP销量SKU聚合草稿", text)
        for word in ("聚合上新", "整合链接", "类目TOP SKU聚合", "组合链接草稿"):
            self.assertIn(word, text)
        self.assertIn("orderQty30d", text)
        self.assertIn("skuDetailManualPending", text)

    def test_reference_covers_conflicts(self):
        text = (ROOT / "references" / "top-sku-aggregation.md").read_text(encoding="utf-8")
        for phrase in ("orderQty30d", "submit_state.json", "skuDetailManualPending",
                       "Profile 锁", "saveActionType=0", "productStatus=105",
                       "consistencyOverride", "review.detailAccepted"):
            self.assertIn(phrase, text)

    def test_no_kill_chrome_advice(self):
        text = (ROOT / "references" / "top-sku-aggregation.md").read_text(encoding="utf-8")
        self.assertIn("不按 `*jd-material-pop*` 过滤并结束 chrome 进程", text)
        self.assertIn("不按 PID 强杀", text)
        self.assertNotIn("Stop-Process", text)
        self.assertNotIn("taskkill", text)

    def test_version_bumped(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("version: 1.3.0", text)


class FakeCollectJm(FakeJm):
    def __init__(self, *, product_rows, details, orders):
        super().__init__()
        self.product_rows = product_rows
        self.details = details
        self.orders = orders

    def get(self, skill, url, params=None):
        self.calls.append(("get", url, params))
        if "venderinfo" in url:
            return {"data": {"venderId": self.vender, "shopBaseInfo": {"shopName": self.shop}}}
        if "products/{productId}" in url:
            return {"data": self.details[str(params["productId"])]}
        if "/products" in url:
            return {"data": self.product_rows,
                    "paginationData": {"totalItems": len(self.product_rows), "currentPage": 1}}
        if "/orders" in url:
            if params.get("orderType") == 22:
                return {"data": self.orders,
                        "paginationData": {"totalItems": len(self.orders), "currentPage": 1}}
            return {"data": [], "paginationData": {"totalItems": 0, "currentPage": 1}}
        if "product-apply" in url:
            return {"data": self.details[str(params["applyId"])]}
        return {"data": {}}


class CollectTests(unittest.TestCase):
    def test_collect_ranks_by_order_qty_not_sales_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {
                "venderId": 12345678, "shopName": "测试店", "jdoProfile": "pop-test",
                "categoryId": 53181, "topN": 2, "title": "", "stock": 100,
                "draftOnly": True, "topSkus": [],
            }
            (root / "task_config.json").write_text(
                json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            details = {"101": make_source(101), "102": make_source(102, price="29.90")}
            fake = FakeCollectJm(
                product_rows=[
                    {"productId": 101, "productName": "A", "productStatus": 103,
                     "categoryId": 53181, "salesVolume": 1000},
                    {"productId": 102, "productName": "B", "productStatus": 103,
                     "categoryId": 53181, "salesVolume": 10},
                ],
                details=details,
                orders=[
                    {"orderStatus": "FINISHED", "skuItemInfoList": [{"skuId": 1011, "itemNum": 1}]},
                    {"orderStatus": "FINISHED", "skuItemInfoList": [{"skuId": 1021, "itemNum": 5}]},
                ],
            )
            with patch.object(tsa, "load_mover", return_value=fake), \
                 patch.object(tsa.time, "sleep", lambda _: None):
                result = tsa.collect(root / "task_config.json")
            self.assertEqual("orderQty30d", result["rankingBasis"])
            top = json.loads((root / "top_skus.json").read_text(encoding="utf-8"))
            self.assertEqual(["102", "101"], [item["pid"] for item in top])
            self.assertEqual(5, top[0]["spuOrderQty30d"])
            self.assertEqual(1, top[1]["spuOrderQty30d"])
            self.assertTrue((root / "source_applies.json").is_file())


if __name__ == "__main__":
    unittest.main()