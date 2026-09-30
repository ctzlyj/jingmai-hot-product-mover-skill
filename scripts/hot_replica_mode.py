# -*- coding: utf-8 -*-
"""Shared rules for the JD hot-replica mode (2026-09-30 user-confirmed v2).

1. Dynamic image prompt: every image (main / detail / SKU spec / style
   representative) is remade with ONE prompt built from the source link's
   cleaned long title. The trailing SKU-spec segment after the first space is
   stripped (e.g. "...沐浴桶盆 速开【蓝色鹿80直径】加热款" -> "...沐浴桶盆")
   because spec attributes mislead generation. Product consistency and text
   selling points must be preserved; people must be replaced.
2. Brand gate (hard, BEFORE any material collection or image generation):
   the target category's authorized-brand list must contain brandId=-1
   ("无品牌"). If it does not, the platform rejects brandId=-1 and the link is
   recorded as SKIPPED_BRAND_REQUIRED - no draft, no generation, no upload,
   no publish. Never fill a substitute brand to work around it.
"""
from __future__ import annotations

import re


DYNAMIC_PROMPT_TEMPLATE = (
    "你是一个京东电商团队的专业美工，优化这张【{title}】电商图，必须要保证商品一致性，图片完整性。"
    "去除图片上商品以外部分的品牌属性（比如品牌名称，商标，京喜自营等）但是如果有文字卖点，必须保留。"
    "通过原图和标题中的信息把产品的优点凸显。如果有人物，必须把人物换成更漂亮/更帅的人，不要用原人物。"
    "如果这张图片没有商品，就优化这张图片的设计结构，只去除这张图上的品牌属性参数。"
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
