import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import image_patch_update as patch


def make_detail():
    return {
        "productInfo": {
            "productId": 123,
            "categoryDetail": {"lastCategoryId": 35409},
        },
        "material": {
            "mainImages": [
                {
                    "uuid": "111",
                    "imageInfoList": [
                        {"imgUrl": "jfs/old-a1.png", "orderSort": 2},
                        {"imgUrl": "jfs/old-a0.png", "orderSort": 1},
                    ],
                },
                {
                    "uuid": "222",
                    "imageInfoList": [
                        {"imgUrl": "jfs/old-b0.png", "orderSort": 1},
                    ],
                },
            ]
        },
        "skuList": [
            {"skuId": 1, "priceInfo": {"jdPrice": 10.0}},
            {"skuId": 2, "priceInfo": {"jdPrice": 20.0}},
        ],
    }


class BuildImagePatchPayloadTest(unittest.TestCase):
    def test_swaps_mapped_urls_and_preserves_groups_and_order(self):
        body, summary = patch.build_image_patch_payload(
            make_detail(),
            {
                "jfs/old-a0.png": "jfs/new-a0.png",
                "jfs/old-a1.png": "jfs/new-a1.png",
                "jfs/old-b0.png": "jfs/new-b0.png",
            },
        )
        self.assertEqual("123", body["applyId"])
        self.assertEqual(3, body["opType"])
        self.assertEqual(35409, body["productInfo"]["categoryDetail"]["lastCategoryId"])
        self.assertEqual(2, len(body["patchOperationList"]))
        first = json.loads(body["patchOperationList"][0]["value"])
        self.assertEqual("WareSkuImg", first["componentCode"])
        urls = []
        for image in first["value"]:
            props = {p["key"]: p["value"] for p in image["properties"]}
            urls.append((props["imgUrl"], props["orderSort"]))
        self.assertEqual([("jfs/new-a0.png", "1"), ("jfs/new-a1.png", "2")], urls)
        for image in first["value"]:
            props = {p["key"]: p["value"] for p in image["properties"]}
            self.assertEqual("111", props["colorId"])
            self.assertEqual("111", props["imgRefId"])
        self.assertEqual(3, summary["replaced"])
        self.assertEqual(
            ["jfs/new-a0.png", "jfs/new-a1.png", "jfs/new-b0.png"],
            sorted(summary["mapping"].values()),
        )

    def test_unmapped_urls_are_kept_and_reported(self):
        body, summary = patch.build_image_patch_payload(make_detail(), {"jfs/old-a0.png": "jfs/new-a0.png"})
        second = json.loads(body["patchOperationList"][1]["value"])
        props = {p["key"]: p["value"] for p in second["value"][0]["properties"]}
        url = props["imgUrl"]
        self.assertEqual("jfs/old-b0.png", url)
        self.assertIn("jfs/old-b0.png", summary["unchangedUrls"])
        self.assertEqual(1, summary["replaced"])


class VerifyPatchTest(unittest.TestCase):
    def test_accepts_expected_swap(self):
        before = make_detail()
        after = make_detail()
        mapping = {"jfs/old-a0.png": "jfs/new-a0.png"}
        for group in after["material"]["mainImages"]:
            for image in group["imageInfoList"]:
                if image["imgUrl"] in mapping:
                    image["imgUrl"] = mapping[image["imgUrl"]]
        self.assertEqual([], patch.verify_patch(before, after, mapping))

    def test_rejects_failed_swap(self):
        before = make_detail()
        after = make_detail()
        mapping = {"jfs/old-a0.png": "jfs/new-a0.png"}
        self.assertEqual(1, len(patch.verify_patch(before, after, mapping)))

    def test_rejects_price_change(self):
        before = make_detail()
        after = make_detail()
        after["skuList"][0]["priceInfo"]["jdPrice"] = 99.0
        problems = patch.verify_patch(before, after, {})
        self.assertTrue(any("price changed" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
