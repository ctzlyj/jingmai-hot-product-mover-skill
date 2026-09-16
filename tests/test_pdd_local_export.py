import sys
import tempfile
import unittest
import hashlib
from pathlib import Path

from openpyxl import Workbook
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from pdd_local_export_mvp import (
    PddExportError,
    harmonize_sku_main_images,
    materialize_local_export,
    parse_export_folder,
)
from jd_product_mover import target_price
from taobao_product_mvp import pricing_audit


def write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 800), "white").save(path, format="JPEG")


def write_workbook(
    path: Path,
    rows: list[list[object]],
    *,
    price_header: str = "价格",
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "类目名"
    sheet.append([
        "上架ID", "标题", "款式", "套餐", price_header, "库存", "商品编码",
        "是否上架", "标价", "优惠券", "实际售价",
    ])
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PddLocalExportTests(unittest.TestCase):
    def test_pdd_price_is_reversed_from_twice_the_source_arrival_price(self):
        pricing = {
            "targetArrivalMultiplier": "2",
            "belowCompetitor": "0",
            "nationalSubsidyRate": "0.15",
            "activityDiscountRate": "0.15",
            "coupon": "0",
        }
        self.assertEqual("7.31", str(target_price("2.64", pricing)))
        audit = pricing_audit({
            "source": {"pricing": pricing},
            "skus": [{
                "sourceSku": "900000000101-SKU001",
                "sourcePrice": 2.64,
                "jdPrice": 7.31,
            }],
        })
        self.assertTrue(audit["ok"])
        self.assertEqual("5.28", audit["rows"][0]["expectedArrivalPrice"])

    def test_colleague_price_header_and_custom_vendor_skus_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "拼多多_商品ID_900000000101"
            folder.mkdir()
            write_workbook(folder / "价格计算模板.xlsx", [
                ["900000000101", "测试励志挂画", "不吃学习的苦", "20x40cm【无痕钉】", 2.64, 1000, "GZH778A"],
                [None, None, "不吃学习的苦", "40x80cm【无痕钉】", 5.52, 1000, "GZH778B"],
            ], price_header="同行售价")
            write_image(folder / "SKU" / "SKU01.jpg")
            write_image(folder / "SKU" / "SKU02.jpg")
            write_image(folder / "主图" / "主图01.jpg")
            write_image(folder / "详情图" / "详情图01.jpg")

            source = parse_export_folder(folder)

            self.assertEqual(
                ["GZH778A", "GZH778B"],
                [variant["vendorSku"] for variant in source["variants"]],
            )

    def test_duplicate_custom_vendor_sku_stops_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "拼多多_商品ID_900000000102"
            folder.mkdir()
            write_workbook(folder / "价格计算模板.xlsx", [
                ["900000000102", "测试励志挂画", "A款", "20x40cm", 2.64, 1000, "CUSTOM-1"],
                [None, None, "B款", "20x40cm", 2.64, 1000, "CUSTOM-1"],
            ])
            write_image(folder / "SKU" / "SKU01.jpg")
            write_image(folder / "SKU" / "SKU02.jpg")
            write_image(folder / "主图" / "主图01.jpg")
            write_image(folder / "详情图" / "详情图01.jpg")

            with self.assertRaisesRegex(PddExportError, "商家SKU重复"):
                parse_export_folder(folder)

    def test_formula_only_rows_are_not_real_skus_and_single_dimension_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "拼多多_商品ID_900000000103"
            folder.mkdir()
            write_workbook(folder / "价格计算模板.xlsx", [
                ["900000000103", "测试挂画", "20*30cm【实木挂轴】", None, 2.83, 1000, None, None, "=(E2-0.1)/0.33", 10, "=I2*0.33-J2"],
                [None, None, "30*45cm【实木挂轴】", None, 5.62, 1000, None, None, "=(E3-0.1)/0.33", 10, "=I3*0.33-J3"],
                [None, None, None, None, None, None, None, None, "=(E4-0.1)/0.33", 10, "=I4*0.33-J4"],
            ])
            write_image(folder / "SKU" / "SKU01_20_30cm.jpg")
            write_image(folder / "SKU" / "SKU02_30_45cm.jpg")
            write_image(folder / "主图" / "主图01.jpg")
            write_image(folder / "详情图" / "详情图01.jpg")

            source = parse_export_folder(folder)

            self.assertEqual("pdd-local-export/v1", source["schemaVersion"])
            self.assertEqual({"pdd-style": "款式"}, source["saleProperties"])
            self.assertEqual(2, len(source["variants"]))
            self.assertEqual("2.83", source["variants"][0]["sourcePrice"])
            self.assertEqual("900000000103-SKU001", source["variants"][0]["sourceSku"])

    def test_mismatched_sku_image_count_stops_before_draft_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "拼多多_商品ID_900000000104"
            folder.mkdir()
            write_workbook(folder / "价格计算模板.xlsx", [
                ["900000000104", "测试挂画", "A款", "30*40cm", 8.9, 1000],
            ])
            write_image(folder / "SKU" / "SKU01_A款.jpg")
            write_image(folder / "SKU" / "SKU02_多余图片.jpg")
            write_image(folder / "主图" / "主图01.jpg")
            write_image(folder / "详情图" / "详情图01.jpg")

            with self.assertRaisesRegex(PddExportError, "SKU行数1与SKU图片数2不一致"):
                parse_export_folder(folder)

    def test_compliance_replacement_archives_original_without_touching_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_folder = root / "拼多多_商品ID_900000000105"
            source_folder.mkdir()
            write_workbook(source_folder / "价格计算模板.xlsx", [
                ["900000000105", "错误的柠檬茶标题", "爱是忍让", None, 8.9, 1000],
            ])
            sku_image = source_folder / "SKU" / "SKU01_爱是忍让.jpg"
            main_image = source_folder / "主图" / "主图01.jpg"
            detail_image = source_folder / "详情图" / "详情图01.jpg"
            write_image(sku_image)
            write_image(main_image)
            write_image(detail_image)
            Image.new("RGB", (800, 800), "red").save(sku_image, format="JPEG")
            Image.new("RGB", (800, 800), "green").save(main_image, format="JPEG")
            source_before = sha256(detail_image)
            replacement = root / "repairs" / "详情图01.jpg"
            replacement.parent.mkdir(parents=True)
            Image.new("RGB", (800, 800), "blue").save(replacement, format="JPEG")
            source = parse_export_folder(source_folder)
            review = {
                "sourceSku": "900000000105",
                "decision": "PASS",
                "reason": "第三方入口页已生成合规副本并原位替换。",
                "titleOverride": "爱是理解和忍让文字挂画客厅卧室实木挂轴装饰画",
                "replacements": [{
                    "role": "详情图",
                    "order": 1,
                    "sourceFilename": detail_image.name,
                    "sourceSha256": source_before,
                    "replacementPath": str(replacement),
                    "replacementSha256": sha256(replacement),
                    "reason": "移除第三方平台入口和福利宣传",
                }],
            }

            output = materialize_local_export(
                source,
                source_folder,
                root / "outputs",
                review,
                root,
            )

            self.assertEqual(source_before, sha256(detail_image))
            self.assertEqual(sha256(replacement), sha256(output / "详情图" / "详情图001.jpg"))
            self.assertEqual(source_before, sha256(output / "原图归档" / "详情图" / "详情图001.jpg"))
            manifest = __import__("json").loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(review["titleOverride"], manifest["title"])
            self.assertEqual("错误的柠檬茶标题", manifest["sourceOriginalTitle"])
            self.assertEqual(1, len(manifest["complianceRepairs"]))

    def test_shared_risky_hash_requires_every_occurrence_to_be_declared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_folder = root / "拼多多_商品ID_100000000001"
            source_folder.mkdir()
            write_workbook(source_folder / "价格计算模板.xlsx", [
                ["100000000001", "测试挂画", "A款", None, 8.9, 1000],
            ])
            shared = source_folder / "SKU" / "SKU01_A款.jpg"
            write_image(shared)
            (source_folder / "主图").mkdir()
            (source_folder / "详情图").mkdir()
            (source_folder / "主图" / "主图01.jpg").write_bytes(shared.read_bytes())
            (source_folder / "详情图" / "详情图01.jpg").write_bytes(shared.read_bytes())
            replacement = root / "repair.jpg"
            Image.new("RGB", (800, 800), "blue").save(replacement, format="JPEG")
            source = parse_export_folder(source_folder)
            review = {
                "sourceSku": "100000000001",
                "decision": "PASS",
                "reason": "测试共享风险图片覆盖。",
                "replacements": [{
                    "role": "SKU",
                    "order": 1,
                    "sourceSha256": sha256(shared),
                    "replacementPath": str(replacement),
                    "reason": "测试",
                }],
            }

            with self.assertRaisesRegex(PddExportError, "相同风险原图仍有未声明位置"):
                materialize_local_export(source, source_folder, root / "outputs", review, root)

    def test_existing_product_id_in_any_matching_folder_blocks_rematerialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_folder = root / "拼多多_商品ID_100000000002"
            source_folder.mkdir()
            write_workbook(source_folder / "价格计算模板.xlsx", [
                ["100000000002", "测试挂画", "A款", None, 8.9, 1000],
            ])
            write_image(source_folder / "SKU" / "SKU01_A款.jpg")
            write_image(source_folder / "主图" / "主图01.jpg")
            write_image(source_folder / "详情图" / "详情图01.jpg")
            source = parse_export_folder(source_folder)
            stale_folder = root / "outputs" / "100000000002_旧标题"
            stale_folder.mkdir(parents=True)
            (stale_folder / "state.json").write_text('{"productId": 123}', encoding="utf-8")

            with self.assertRaisesRegex(PddExportError, "已有productId，禁止重新生成或创建"):
                materialize_local_export(source, source_folder, root / "outputs")

    def test_identical_sku_images_share_one_manifest_path_across_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_folder = root / "拼多多_商品ID_100000000003"
            source_folder.mkdir()
            write_workbook(source_folder / "价格计算模板.xlsx", [
                ["100000000003", "测试挂画", "A款", "30x40cm", 8.9, 1000],
                [None, None, "A款", "40x60cm", 9.9, 1000],
            ])
            first = source_folder / "SKU" / "SKU01_A款_30x40cm.jpg"
            second = source_folder / "SKU" / "SKU02_A款_40x60cm.jpg"
            write_image(first)
            second.write_bytes(first.read_bytes())
            write_image(source_folder / "主图" / "主图01.jpg")
            write_image(source_folder / "详情图" / "详情图01.jpg")
            source = parse_export_folder(source_folder)

            output = materialize_local_export(source, source_folder, root / "outputs")

            manifest = __import__("json").loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["variants"][0]["skuImage"], manifest["variants"][1]["skuImage"])
            self.assertTrue((output / "SKU图" / "SKU图002.jpg").is_file())

    def test_dimension_columns_are_swapped_when_first_column_is_only_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "拼多多_商品ID_100000000004"
            folder.mkdir()
            write_workbook(folder / "价格计算模板.xlsx", [
                ["100000000004", "测试挂画", "宽度30公分*高度40公分", "书法歡囍_实木挂轴", 8.9, 1000],
                [None, None, "宽度40公分*高度60公分", "书法歡囍_实木挂轴", 9.9, 1000],
            ])
            write_image(folder / "SKU" / "SKU01.jpg")
            write_image(folder / "SKU" / "SKU02.jpg")
            write_image(folder / "主图" / "主图01.jpg")
            write_image(folder / "详情图" / "详情图01.jpg")

            source = parse_export_folder(folder)

            self.assertTrue(source["dimensionOrderAdjusted"])
            self.assertEqual("书法歡囍_实木挂轴", source["variants"][0]["sourceAttributes"]["款式"])
            self.assertEqual("宽度30公分*高度40公分", source["variants"][0]["sourceAttributes"]["套餐"])

    def test_final_style_alias_controls_shared_sku_main_image(self):
        listing = {
            "skus": [
                {"skuKey": "S1", "styleAlias": "背靠金山"},
                {"skuKey": "S2", "styleAlias": "背靠金山"},
            ],
            "images": [
                {"skuKey": "S1", "role": "sku_main", "localPath": "SKU图/001.jpg"},
                {"skuKey": "S2", "role": "sku_main", "localPath": "SKU图/002.jpg"},
            ],
        }

        harmonize_sku_main_images(listing)

        self.assertEqual("SKU图/001.jpg", listing["images"][1]["localPath"])


if __name__ == "__main__":
    unittest.main()
