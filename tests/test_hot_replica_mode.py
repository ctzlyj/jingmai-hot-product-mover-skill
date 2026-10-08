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


def test_dynamic_prompt_v3_three_layers():
    # 2026-10-08 agreed three-layer contract (product body / off-product copy /
    # brand removal), replacing the ambiguous "优化+优点凸显" wording.
    prompt = hotmode.build_dynamic_prompt("泡澡桶折叠大人家用成人汗蒸自动加热浴缸泡浴桶全身保温沐浴桶盆")
    assert "泡澡桶折叠大人家用成人汗蒸自动加热浴缸泡浴桶全身保温沐浴桶盆" in prompt
    assert "第一层·商品本体保护" in prompt and "逐像素原样保留" in prompt
    assert "第二层·画面文案保护" in prompt and "逐字原样保留" in prompt
    assert "第三层·品牌清除，仅限非商品区域" in prompt
    # old root-cause wording must stay out
    assert "优化这张" not in prompt and "优点凸显" not in prompt


def test_brand_gate_requires_minus_one():
    assert hotmode.brand_gate({"brands": [{"brandId": -1}]})["ok"] is True
    assert hotmode.brand_gate({"brands": [{"brandId": 5}]})["ok"] is False


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
