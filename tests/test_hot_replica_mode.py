# -*- coding: utf-8 -*-
import sys
from pathlib import Path

from PIL import Image

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import hot_replica_mode as hotmode  # noqa: E402


def test_dynamic_prompt_strips_spec_tail():
    title = "泡澡桶折叠大人家用成人汗蒸自动加热浴缸泡浴桶全身保温沐浴桶盆 速开【蓝色鹿80直径】加热款"
    assert hotmode.strip_sku_spec(title) == \
        "泡澡桶折叠大人家用成人汗蒸自动加热浴缸泡浴桶全身保温沐浴桶盆"
    assert "速开" not in hotmode.build_dynamic_prompt(title)
    # 2026-10-10 user-confirmed final wording; do not rewrite without user
    # instruction (the 2026-10-08 three-layer variant was reverted).
    assert hotmode.build_dynamic_prompt(title).startswith(
        "你是一个京东电商团队的专业美工，优化这张【泡澡桶折叠大人家用成人汗蒸自动加热浴缸泡浴桶全身保温沐浴桶盆】电商图，"
    )


def test_brand_gate_requires_minus_one():
    assert hotmode.brand_gate({"brands": [{"brandId": -1}]})["ok"] is True
    assert hotmode.brand_gate({"brands": [{"brandId": 5}]})["ok"] is False


def test_style_image_mode_detects_series_and_shared():
    series = hotmode.style_image_mode({"variants": [
        {"sourceSku": "1", "sourceAttributes": {"颜色": "A"},
         "sourceImageUrls": ["a1", "a2", "a3"]},
        {"sourceSku": "2", "sourceAttributes": {"颜色": "B"},
         "sourceImageUrls": ["b1", "b2", "b3"]},
    ]})
    assert series["mode"] == "series"
    assert series["evidence"] == "2 styles / 2 unique scene sequences"

    shared = hotmode.style_image_mode({"variants": [
        {"sourceSku": "1", "sourceAttributes": {"颜色": "A"},
         "sourceImageUrls": ["a1", "same2", "same3"]},
        {"sourceSku": "2", "sourceAttributes": {"颜色": "B"},
         "sourceImageUrls": ["b1", "same2", "same3"]},
    ]})
    assert shared["mode"] == "shared"


def test_square_pad_image(tmp_path):
    # 3:4 image, vertical gradient background: padding must keep every row.
    p = tmp_path / "sku.png"
    Image.new("RGB", (30, 40), (45, 62, 109)).save(p)
    assert hotmode.square_pad_image(p) == (40, 40)
    with Image.open(p) as im:
        assert im.size == (40, 40)
        px = im.convert("RGB").load()
        assert px[0, 20] == (45, 62, 109)
    assert hotmode.square_pad_image(p) == (40, 40)  # idempotent
