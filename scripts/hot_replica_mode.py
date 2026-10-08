# -*- coding: utf-8 -*-
"""Shared rules for the JD hot-replica mode (2026-10-08 user-confirmed v3).

1. Dynamic image prompt: every image (main / detail / SKU spec / style
   representative) is remade with ONE prompt built from the source link's
   cleaned long title. The trailing SKU-spec segment after the first space is
   stripped (e.g. "...沐浴桶盆 速开【蓝色鹿80直径】加热款" -> "...沐浴桶盆")
   because spec attributes mislead generation. v3 (2026-10-08) rewrites the
   prompt into an explicit three-layer contract agreed with the user after
   "商品上的文字被修改" feedback: (1) product body incl. its printed text is
   pixel-identical, never redrawn; (2) off-product selling copy preserved
   verbatim, layout-only adjustments; (3) brand attributes removed only from
   non-product areas. People must still be replaced.
2. Brand gate (hard, BEFORE any material collection or image generation):
   the target category's authorized-brand list must contain brandId=-1
   ("无品牌"). If it does not, the platform rejects brandId=-1 and the link is
   recorded as SKIPPED_BRAND_REQUIRED - no draft, no generation, no upload,
   no publish. Never fill a substitute brand to work around it.
"""
from __future__ import annotations

import re
from pathlib import Path


DYNAMIC_PROMPT_TEMPLATE = (
    "你是京东电商团队的资深修图师，正在对这张【{title}】电商图做合规清理与整体提质，禁止重新设计。"
    "严格遵守以下三层规则，规则冲突时层级靠前者优先："
    "【第一层·商品本体保护，最高优先级】商品的实物主体必须逐像素原样保留：形状、颜色、材质、结构、角度、光影一律不变；"
    "商品上印的任何文字、标签、贴纸、按键、图案必须逐字逐字体原样保留，禁止改字、重绘、变形、位移、增删或替换。"
    "【第二层·画面文案保护】商品以外的卖点文字和说明文字必须逐字原样保留，每个字和标点与原图完全一致，"
    "仅允许微调排版位置与背景装饰，禁止改写、增删、换字体或以任何理由美化文字内容。"
    "【第三层·品牌清除，仅限非商品区域】只清除背景、边角、装饰条等商品以外区域中的品牌名称、商标、旗舰店、"
    "京喜自营等品牌标识，商品本体上的任何内容一律不动。"
    "如果有人物，把人物换成更漂亮/更帅的模特，姿势与构图尽量保持原样，不得遮挡商品本体。"
    "如果这张图没有商品，只做去品牌清理和版式微调，不重绘画面内容。"
)

# A space-separated tail that looks like SKU spec attributes (colour/diameter/
# size/count variants) and therefore must NOT enter the generation prompt.
_SPEC_TAIL_RE = re.compile(
    r"【|】|\d+\s*(?:cm|CM|厘米|mm|毫米|g|克|kg|千克|L|升|ml|毫升|片|个|只|条|支|装|层|号)|"
    r"直径|加厚|速开|加大|豪华|升级|款|色"
)


def strip_sku_spec(title: str) -> str:
    """Drop the trailing SKU-spec segment after the first space.

    Example:
      "泡澡桶折叠大人家用成人汗蒸自动加热浴缸泡浴桶全身保温沐浴桶盆 速开【蓝色鹿80直径】加热款"
      -> "泡澡桶折叠大人家用成人汗蒸自动加热浴缸泡浴桶全身保温沐浴桶盆"

    A space-separated tail is only dropped when it looks like spec attributes;
    a plain tail is kept so legitimate multi-word titles survive.
    """
    text = str(title or "").strip()
    match = re.match(r"^(\S+)\s+(.+)$", text)
    if match and _SPEC_TAIL_RE.search(match.group(2)):
        return match.group(1)
    return text


def build_dynamic_prompt(title: str) -> str:
    """Build the one dynamic remake prompt from the cleaned long title."""
    return DYNAMIC_PROMPT_TEMPLATE.format(title=strip_sku_spec(title))


def no_brand_allowed(reference: dict) -> bool:
    """True when the target category accepts brandId=-1 (无品牌)."""
    brands = reference.get("brands") or []
    return any(int(b.get("brandId") or 0) == -1 for b in brands)


def brand_gate(reference: dict, category_label: str = "") -> dict:
    """Hard pre-generation gate. Returns {'ok': bool, ...} evidence dict."""
    if no_brand_allowed(reference):
        return {
            "ok": True,
            "decision": "PROCEED",
            "evidence": "类目授权品牌列表包含 brandId=-1（无品牌），可无品牌上架",
            "category": category_label,
        }
    brand_names = [str(b.get("brandName") or "") for b in reference.get("brands") or []]
    return {
        "ok": False,
        "decision": "SKIPPED_BRAND_REQUIRED",
        "evidence": "类目授权品牌列表不含 brandId=-1（无品牌），平台将拒绝无品牌商品",
        "category": category_label,
        "authorizedBrandCount": len(brand_names),
    }


def square_pad_image(path: Path) -> tuple[int, int]:
    """Pad a generated image to exactly 1:1 (JD spu_main/sku_main rule).

    Non-square generated SKU/main images are rejected by the local preflight
    ("sku_main 必须为1:1"). Regenerating wastes quota, so pad losslessly with
    row-wise edge extension (keeps vertical gradients natural, no stretching,
    no cropping). The file is modified in place; returns the final size.
    2026-09-30 verified on 10201583840642 SKU图1.png (1086x1448 -> 1448x1448,
    published and platform-verified afterwards).
    """
    from PIL import Image

    with Image.open(path) as im:
        rgb = im.convert("RGB")
        width, height = rgb.size
        if width == height:
            return width, height
    import numpy as np

    arr = np.asarray(rgb)
    if width < height:
        pad = height - width
        left = np.repeat(arr[:, :1, :], pad // 2, axis=1)
        right = np.repeat(arr[:, -1:, :], pad - pad // 2, axis=1)
        arr = np.concatenate([left, arr, right], axis=1)
    else:
        pad = width - height
        top = np.repeat(arr[:1, :, :], pad // 2, axis=0)
        bottom = np.repeat(arr[-1:, :, :], pad - pad // 2, axis=0)
        arr = np.concatenate([top, arr, bottom], axis=0)
    out = Image.fromarray(arr)
    out.save(path, "PNG")
    return out.size
