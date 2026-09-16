import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jdo_listing_mvp as jingmai


class ImageUploadNameTests(unittest.TestCase):
    def test_long_ascii_and_chinese_names_are_bounded(self):
        for name in ("a" * 64 + ".jpg", "图片" * 40 + ".png"):
            result = jingmai.safe_upload_image_name(name)
            self.assertLessEqual(jingmai.weighted_text_length(result), 64)
            self.assertEqual(Path(result).suffix, Path(name).suffix)
            self.assertEqual(result, jingmai.safe_upload_image_name(name))

    def test_short_names_remain_unchanged(self):
        self.assertEqual(jingmai.safe_upload_image_name("SKU001.jpg"), "SKU001.jpg")

    def test_upload_changes_only_remote_name_not_local_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / ("a" * 64 + ".jpg")
            source.write_bytes(b"unchanged-image")
            captured = {}
            def invoke(**kwargs):
                captured.update(json.loads(kwargs["body_path"].read_text(encoding="utf-8")))
                return {"data": {"imgUrl": "verified-placeholder"}}
            with patch.object(jingmai, "JDO_RUNTIME", Path(temporary) / "runtime.pyc"), patch.object(jingmai, "run_jdo_large", side_effect=invoke):
                jingmai.upload("zone", 1, source)
            self.assertLessEqual(len(captured["zoneImageDTO"]["imgName"]), 64)
            self.assertEqual(source.read_bytes(), b"unchanged-image")


if __name__ == "__main__":
    unittest.main()
