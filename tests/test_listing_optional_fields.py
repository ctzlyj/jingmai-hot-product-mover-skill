import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import jdo_listing_mvp as listing


class OptionalListingFieldsTests(unittest.TestCase):
    def manifest(self):
        return {
            "category": {"id": 1},
            "product": {
                "productKey": "TEST", "title": "测试商品", "brandId": -1,
                "sevenDayReturnCode": "1", "freightTemplateId": "12",
            },
            "skus": [], "images": [], "categoryAttributes": [],
        }

    def reference(self, freight_required=None):
        components = [] if freight_required is None else [
            {"path": "root", "componentCode": "WareTransport", "required": freight_required},
        ]
        return {"category": {"id": 1}, "componentCatalog": components,
                "brands": [], "media": {"categories": [], "defaultCategoryId": 1}}

    def test_supplied_logistics_values_reach_publish_schema(self):
        manifest = self.manifest()
        manifest["product"].update(packageLengthMm=240, packageWidthMm=180,
                                   packageHeightMm=60, weightKg="0.400")
        values = {row["componentCode"]: row.get("value") for row in listing.build_publish_schema(manifest)}
        self.assertEqual(values.get("WareLength"), "240")
        self.assertEqual(values.get("WareWide"), "180")
        self.assertEqual(values.get("WareHeight"), "60")
        self.assertEqual(values.get("WareWeight"), "0.400")

    def test_unknown_logistics_values_are_not_invented(self):
        manifest = self.manifest()
        manifest["product"].update(packageLengthMm="", packageWidthMm=None)
        codes = {row["componentCode"] for row in listing.build_publish_schema(manifest)}
        self.assertFalse(codes & {"WareLength", "WareWide", "WareHeight", "WareWeight"})

    def test_absent_freight_is_omitted_not_zero_filled(self):
        manifest = self.manifest()
        manifest["product"].pop("freightTemplateId")
        codes = {row["componentCode"] for row in listing.build_publish_schema(manifest)}
        self.assertNotIn("WareTransport", codes)

    def test_optional_live_freight_does_not_fail_local_preflight(self):
        manifest = self.manifest()
        manifest["product"].pop("freightTemplateId")
        errors, _ = listing.validate_manifest(ROOT / "listing.json", manifest, self.reference(False))
        self.assertFalse(any("freightTemplateId" in error for error in errors), errors)

    def test_required_or_unknown_freight_remains_a_preflight_error(self):
        for required in (True, None):
            with self.subTest(required=required):
                manifest = self.manifest()
                manifest["product"].pop("freightTemplateId")
                errors, _ = listing.validate_manifest(ROOT / "listing.json", manifest, self.reference(required))
                self.assertTrue(any("freightTemplateId" in error for error in errors), errors)

    def test_supplied_freight_is_preserved(self):
        values = {row["componentCode"]: row.get("value") for row in listing.build_publish_schema(self.manifest())}
        self.assertEqual(values["WareTransport"], "12")

    def test_live_schema_numeric_maximum_is_enforced(self):
        manifest = self.manifest()
        manifest["product"].update(packageLengthMm=400, packageWidthMm=200,
                                   packageHeightMm=80, weightKg="0.500")
        reference = self.reference(False)
        reference["componentCatalog"].append({
            "path": "root", "componentCode": "WareLength", "required": True,
            "validateRule": [{"validateType": "maximum", "validateRule": 350}],
        })
        errors, _ = listing.validate_manifest(ROOT / "listing.json", manifest, reference)
        self.assertTrue(any("packageLengthMm" in error and "350" in error for error in errors), errors)

    def test_main_image_uses_real_encoding_not_filename_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "looks-like-jpeg.jpg"
            Image.new("RGB", (800, 800), "white").save(path, format="WEBP")
            errors, _ = listing.validate_image({"role": "spu_main"}, path)
        self.assertTrue(any("主图仅支持JPEG或PNG实际编码" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
