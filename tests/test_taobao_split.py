import sys
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from taobao_product_mvp import split_source_by_style  # noqa: E402
from publish_local_draft import reuse_split_uploaded_images  # noqa: E402


class TaobaoSplitTests(unittest.TestCase):
    def test_balances_complete_style_groups_and_repairs_contact_wording(self):
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

    def test_reuses_uploaded_common_images_by_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "123-P01_A组"
            second = root / "123-P02_B组"
            for folder in (first, second):
                (folder / ".pipeline").mkdir(parents=True)
                (folder / "详情图").mkdir()
                (folder / "详情图" / "详情图001.png").write_bytes(b"same-image")
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


if __name__ == "__main__":
    unittest.main()
