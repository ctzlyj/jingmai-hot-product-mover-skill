#!/usr/bin/env python3
"""京麦自动上架 MVP：预检、上传图片、发布多 SKU 商品并回查。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from collections import Counter
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "config" / "modern-decorative-painting.reference.json"
CATEGORY_ID = 35381
CATEGORY_NAME = "现代装饰画"
JDO_CLI = Path.home() / ".jdo-cli" / "bin" / "jdo-cli.exe"
JDO_BRIDGE = ROOT / "scripts" / "jdo_large_body_bridge.py"
JDO_RUNTIME = ROOT / "outputs" / ".runtime" / "jdo-cli.pyc"
PYTHON312_CANDIDATES = tuple(
    Path(value)
    for value in (
        os.environ.get("JDO_PYTHON312"),
        str(Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe"),
        r"C:\Program Files\LibreOffice\program\python.exe",
    )
    if value
)
MAX_IMAGE_BYTES = 3 * 1024 * 1024
IMAGE_FORMATS = {"JPEG", "PNG", "GIF", "WEBP"}


class JdoError(RuntimeError):
    pass


def parse_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("HTTP ") and ":" in cleaned:
        cleaned = cleaned.split(":", 1)[1].strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise JdoError(f"jdo-cli 返回非 JSON 内容：{cleaned[:300]}") from exc
    if payload.get("success") is False:
        errors = payload.get("errorList") or []
        message = "; ".join(
            f"{item.get('code', 'UNKNOWN')} {item.get('message', '')}" for item in errors
        )
        raise JdoError(message or "jdo-cli 调用失败")
    return payload


def run_jdo(arguments: list[str]) -> dict[str, Any]:
    executable = Path(os.environ.get("JDO_CLI_PATH", str(JDO_CLI)))
    if not executable.exists():
        raise JdoError(f"找不到 jdo-cli：{executable}")
    environment = os.environ.copy()
    environment["JDO_ENV"] = "prod"
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [str(executable), *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return parse_response(output)


def ensure_jdo_runtime() -> Path:
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:
        raise JdoError("当前Python缺少PyInstaller，无法读取jdo-cli运行时代码") from exc
    executable = Path(os.environ.get("JDO_CLI_PATH", str(JDO_CLI)))
    if not executable.exists():
        raise JdoError(f"找不到 jdo-cli：{executable}")
    runtime = CArchiveReader(str(executable)).extract("jdo-cli")
    JDO_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    if not JDO_RUNTIME.exists() or JDO_RUNTIME.read_bytes() != runtime:
        JDO_RUNTIME.write_bytes(runtime)
    return JDO_RUNTIME


def run_jdo_large(
    *, url: str, method: str, body_path: Path, skill_id: str,
    params: dict[str, Any] | None = None, yes: bool = False,
) -> dict[str, Any]:
    python312 = next((path for path in PYTHON312_CANDIDATES if path.is_file()), None)
    if not python312:
        raise JdoError("找不到兼容 jdo-cli 的 Python 3.12；请设置 JDO_PYTHON312")
    runtime = ensure_jdo_runtime()
    request_path = JDO_RUNTIME.parent / f"request-{uuid.uuid4().hex}.json"
    request_path.write_text(json.dumps({
        "url": url,
        "method": method,
        "bodyFile": str(body_path.resolve()),
        "params": params,
        "skillId": skill_id,
        "requestIdentity": "vender",
        "timeout": 180,
        "yes": yes,
    }, ensure_ascii=False), encoding="utf-8")
    environment = os.environ.copy()
    environment["JDO_ENV"] = "prod"
    try:
        completed = subprocess.run(
            [str(python312), str(JDO_BRIDGE), "--runtime", str(runtime), "--request", str(request_path)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        request_path.unlink(missing_ok=True)
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return parse_response(output)


def get(skill: str, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = ["invoke", "--skill-id", skill, "--url", url, "--method", "GET"]
    if params:
        arguments += ["--params", json.dumps(params, ensure_ascii=False, separators=(",", ":"))]
    arguments += ["--request-identity", "vender"]
    return run_jdo(arguments)


def required(component: dict[str, Any]) -> bool:
    for rule in component.get("validateRule") or []:
        if rule.get("validateType") != "required":
            continue
        value = rule.get("validateRule")
        return value is True or (isinstance(value, str) and value.lower() == "true")
    return False


def flatten(components: list[dict[str, Any]], path: str = "root") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in components or []:
        code = item.get("componentCode")
        if not code:
            continue
        options = [
            {"label": option.get("label"), "value": option.get("value")}
            for option in item.get("optionsValueList") or []
            if option.get("label") is not None or option.get("value") is not None
        ]
        rows.append(
            {
                "path": path,
                "componentCode": code,
                "title": item.get("componentTitle"),
                "inputType": item.get("inputType"),
                "required": required(item),
                "options": options,
                "devTips": item.get("devTips"),
            }
        )
        rows += flatten(item.get("childrenComponents") or [], f"{path}/{code}")
    return rows


def refresh_reference(
    output: Path, category_id: int = CATEGORY_ID, category_name: str = CATEGORY_NAME,
    brand_category_id: int | None = None,
) -> dict[str, Any]:
    brands = get(
        "jdo-product-publish",
        "/rest/sp-seller/v0/vender-brands",
        {"queryType": 2, "categoryId": brand_category_id or category_id},
    ).get("data") or []
    schema_response = get(
        "jdo-product-publish",
        "/rest/sp-product/v0/product-schemas",
        {"lastCategoryId": str(category_id), "scene": "pop"},
    )
    if not schema_response.get("data"):
        raise JdoError("发品 Schema 返回为空")
    schema = json.loads(schema_response["data"][0])

    zones = get("jdo-product-material", "/rest/sp-product/v0/media-zones").get("data") or []
    image_zone = next((item for item in zones if item.get("zoneType") == 1), None)
    if not image_zone:
        raise JdoError("当前店铺没有图片空间")
    media_categories = get(
        "jdo-product-material",
        "/rest/sp-product/v0/media-zones/{zoneId}/categories",
        {"zoneId": image_zone["zoneId"], "page": 1, "pageSize": 100},
    ).get("data") or []
    default_category = next(
        (item for item in media_categories if item.get("categoryName") == "我的本地文件"),
        next((item for item in media_categories if item.get("status") == 1), None),
    )
    if not default_category:
        raise JdoError("图片空间没有可用分类")

    warnings: list[str] = []
    sale_attributes: Any = None
    try:
        sale_attributes = get(
            "jdo-product-publish",
            "/rest/sp-product/v0/categories/{categoryId}/sale-attr-templates",
            {"categoryId": category_id},
        ).get("data")
    except JdoError as exc:
        warnings.append(f"销售属性模板暂不可用：{exc}")

    result = {
        "schemaVersion": 1,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scene": "pop",
        "category": {"id": category_id, "name": category_name},
        "brands": brands,
        "schema": schema,
        "componentCatalog": flatten(schema.get("componentList") or []),
        "media": {
            "zoneId": image_zone["zoneId"],
            "freeSize": image_zone.get("freeSize"),
            "categories": media_categories,
            "defaultCategoryId": default_category["categoryId"],
            "defaultCategoryName": default_category.get("categoryName"),
        },
        "saleAttributeTemplates": sale_attributes,
        "warnings": warnings,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def present(value: Any) -> bool:
    return value is not None and str(value).strip() not in {"", "待填写", "TODO"}


def weighted_text_length(value: str) -> int:
    return sum(1 if ord(character) < 128 else 2 for character in value)


def size_suffix(order: int) -> str:
    if order < 1:
        raise ValueError("尺寸顺序必须为正整数")
    result = ""
    while order > 0:
        order -= 1
        result = chr(65 + order % 26) + result
        order //= 26
    return result


def resolved_vendor_sku(manifest: dict[str, Any], sku: dict[str, Any]) -> str:
    product = manifest.get("product") or {}
    prefix = str(product.get("vendorSkuPrefix") or "GZH").strip()
    style_start = int(product.get("vendorStyleStart") or 10000)
    style_order = int(sku.get("vendorStyleOrder") or sku.get("styleOrder") or 0)
    size_order = int(sku.get("vendorSizeOrder") or sku.get("sizeOrder") or 0)
    generated = f"{prefix}{style_start + style_order - 1:05d}{size_suffix(size_order)}"
    return str(sku.get("vendorSku") or generated).strip()


def size_dimensions(value: str) -> tuple[Decimal, Decimal, str]:
    if value == "单规格":
        return Decimal("0"), Decimal("0"), value
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*cm\s*", value)
    if match:
        width = Decimal(match.group(1))
        height = Decimal(match.group(2))
        return max(width, height), min(width, height), value
    single = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(cm|寸)\s*", value, re.I)
    if not single:
        raise ValueError(f"无法识别尺寸：{value}")
    length = Decimal(single.group(1))
    if single.group(2) == "寸":
        length *= Decimal("2.54")
    return length, Decimal("0"), value


def resolve_path(manifest_path: Path, local_path: str) -> Path:
    candidate = Path(local_path)
    if not candidate.is_absolute():
        candidate = manifest_path.parent / candidate
    return candidate.resolve()


def validate_image(item: dict[str, Any], path: Path) -> tuple[list[str], list[str]]:
    if not path.exists():
        return [f"图片不存在：{path}"], []
    errors: list[str] = []
    warnings: list[str] = []
    if path.stat().st_size >= MAX_IMAGE_BYTES:
        errors.append(f"图片必须小于3MB：{path.name}")
    try:
        with Image.open(path) as image:
            width, height = image.size
            image_format = (image.format or "").upper()
    except Exception as exc:
        return [f"无法读取图片 {path.name}：{exc}"], []
    if image_format not in IMAGE_FORMATS:
        errors.append(f"图片格式不支持：{path.name} ({image_format})")
    role = item.get("role")
    if role in {"spu_main", "sku_main"}:
        if width != height:
            errors.append(f"{role} 必须为1:1：{path.name} 当前{width}x{height}")
        if min(width, height) <= 480:
            errors.append(f"{role} 宽高必须大于480px：{path.name}")
    if role in {"search_main", "long"}:
        if width * 4 != height * 3:
            errors.append(f"{role} 必须为3:4：{path.name} 当前{width}x{height}")
        if min(width, height) <= 480:
            errors.append(f"{role} 宽高必须大于480px：{path.name}")
    if role == "detail":
        if not 750 <= width <= 1500:
            warnings.append(f"detail 宽度建议为750-1500px：{path.name} 当前{width}px；仍保留该图片")
        if height > 1500:
            warnings.append(f"detail 高度超过1500px：{path.name} 当前{height}px；仍保留该图片")
        if width * 2 < height:
            warnings.append(f"detail 宽高比小于1:2：{path.name} 当前{width}x{height}；仍保留该图片")
    return errors, warnings


def multi_set_manifest_errors(manifest: dict[str, Any]) -> list[str]:
    product = manifest.get("product") or {}
    errors: list[str] = []
    if int((manifest.get("category") or {}).get("id") or 35381) == 35381 and product.get("mainImageMode") != "multi_set":
        errors.append("现代装饰画必须使用多套主图模式（mainImageMode=multi_set）")

    style_by_sku = {
        str(sku.get("skuKey") or ""): str(sku.get("styleAlias") or "")
        for sku in manifest.get("skus") or []
    }
    first_images_by_style: dict[str, set[str]] = {}
    for image in manifest.get("images") or []:
        if image.get("role") != "sku_main":
            continue
        sku_key = str(image.get("skuKey") or "")
        style_alias = style_by_sku.get(sku_key, "")
        upload = image.get("upload") or {}
        identity = str(upload.get("imgUrl") or image.get("localPath") or "").strip()
        if style_alias and identity:
            first_images_by_style.setdefault(style_alias, set()).add(identity)
    for style_alias, identities in first_images_by_style.items():
        if len(identities) > 1:
            errors.append(
                f"同一款式的不同尺寸必须共用同一套主图：{style_alias} 检测到{len(identities)}张不同首图"
            )
    return errors


def validate_manifest(path: Path, manifest: dict[str, Any], reference: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    manifest_category_id = int((manifest.get("category") or {}).get("id") or 0)
    reference_category_id = int((reference.get("category") or {}).get("id") or 0)
    if not manifest_category_id or manifest_category_id != reference_category_id:
        errors.append(
            f"商品类目与实时Schema不一致：商品{manifest_category_id}，参考{reference_category_id}"
        )
    product = manifest.get("product") or {}
    required_fields = {
        "productKey": "商品内部编号",
        "title": "商品标题",
        "brandId": "品牌ID",
        "sevenDayReturnCode": "无理由退货代码",
        "freightTemplateId": "运费模板ID",
    }
    for field, label in required_fields.items():
        if not present(product.get(field)):
            errors.append(f"商品信息缺少：{label} ({field})")
    title = str(product.get("title") or "")
    title_units = sum(1 if character.isascii() else 2 for character in title)
    if title_units > 60:
        errors.append(f"商品标题超过京东60字符单位限制：当前{title_units}（汉字按2计算）")
    errors.extend(multi_set_manifest_errors(manifest))
    valid_brand_ids = {str(item.get("brandId")) for item in reference.get("brands", [])}
    if (
        present(product.get("brandId"))
        and str(product.get("brandId")) != "-1"
        and str(product.get("brandId")) not in valid_brand_ids
    ):
        errors.append("品牌ID不在当前店铺可选品牌列表中")
    vendor_sku_prefix = str(product.get("vendorSkuPrefix") or "GZH").strip()
    if not vendor_sku_prefix.isascii() or not vendor_sku_prefix.isalpha() or not vendor_sku_prefix.isupper():
        errors.append("商家编码前缀必须为大写英文字母")
    try:
        vendor_style_start = int(product.get("vendorStyleStart") or 10000)
        if not 0 <= vendor_style_start <= 99999:
            errors.append("商家编码款式起始号必须为0至99999的整数")
    except (TypeError, ValueError):
        errors.append("商家编码款式起始号必须为整数")

    attributes = {item.get("componentCode"): item for item in manifest.get("categoryAttributes") or []}
    required_attributes = [
        item for item in reference.get("componentCatalog") or []
        if item.get("path") == "root/CategoryGroupList" and item.get("required")
    ]
    for component in required_attributes:
        code = str(component.get("componentCode") or "")
        label = str(component.get("title") or code)
        row = attributes.get(code) or {}
        if not row.get("valueIds") and not row.get("valueLabels"):
            errors.append(f"类目属性缺少：{label} ({code})")
            continue
        valid_values = {str(option.get("value")) for option in component.get("options") or []}
        invalid_values = [str(value) for value in row.get("valueIds") or [] if valid_values and str(value) not in valid_values]
        if invalid_values:
            errors.append(f"类目属性值不在实时Schema中：{label} {invalid_values}")
    scene_values = (attributes.get("prop-product-243294") or {}).get("valueIds") or []
    if len(scene_values) > 3:
        errors.append(f"适用场景最多3项，当前{len(scene_values)}项")

    skus = manifest.get("skus") or []
    sku_limit = int((reference.get("schema") or {}).get("extendMap", {}).get("skuBindThresholdNum") or 299)
    if not skus:
        errors.append("至少填写1个SKU")
    if len(skus) > sku_limit:
        errors.append(f"SKU数量超过类目上限：当前{len(skus)}，上限{sku_limit}")
    sku_keys: set[str] = set()
    vendor_skus: set[str] = set()
    combinations: set[tuple[str, str]] = set()
    style_mapping_ids: dict[str, str] = {}
    size_mapping_ids: dict[str, str] = {}
    style_orders: dict[str, int] = {}
    size_orders: dict[str, int] = {}
    for index, sku in enumerate(skus, start=1):
        required_sku_fields = {
            "skuKey": "SKU内部编号",
            "jdPrice": "京东价",
            "stock": "库存",
            "styleAlias": "款式名称",
            "styleMappingId": "款式mappingId",
            "sizeAlias": "规格尺寸",
            "sizeMappingId": "尺寸mappingId",
        }
        if sku.get("skuAttributes") is None:
            required_sku_fields.update({
                "mountingValueId": "装裱属性ID",
                "subjectValueId": "题材属性ID",
            })
        for field, label in required_sku_fields.items():
            if not present(sku.get(field)):
                errors.append(f"SKU第{index}行缺少：{label} ({field})")
        sku_key = str(sku.get("skuKey") or "")
        if sku_key in sku_keys:
            errors.append(f"SKU内部编号重复：{sku_key}")
        sku_keys.add(sku_key)
        try:
            expected_vendor_sku = (
                f"{str(product.get('vendorSkuPrefix') or 'GZH')}"
                f"{int(product.get('vendorStyleStart') or 10000) + int(sku.get('vendorStyleOrder') or sku.get('styleOrder') or 0) - 1:05d}"
                f"{size_suffix(int(sku.get('vendorSizeOrder') or sku.get('sizeOrder') or 0))}"
            )
            vendor_sku = resolved_vendor_sku(manifest, sku)
            if vendor_sku != expected_vendor_sku:
                errors.append(f"SKU第{index}行商家编码不符合规则：应为{expected_vendor_sku}，当前{vendor_sku}")
            if vendor_sku in vendor_skus:
                errors.append(f"商家编码重复：{vendor_sku}")
            vendor_skus.add(vendor_sku)
        except (TypeError, ValueError):
            errors.append(f"SKU第{index}行无法生成商家编码")
        style_alias = str(sku.get("styleAlias") or "")
        size_alias = str(sku.get("sizeAlias") or "")
        try:
            style_orders[style_alias] = int(sku.get("styleOrder") or 0)
            size_orders[size_alias] = int(sku.get("sizeOrder") or 0)
        except (TypeError, ValueError):
            errors.append(f"SKU第{index}行款式或尺寸顺序不是整数")
        combination = (style_alias, size_alias)
        if combination in combinations:
            errors.append(f"销售属性组合重复：{style_alias} / {size_alias}")
        combinations.add(combination)
        if weighted_text_length(style_alias) > 50 or weighted_text_length(size_alias) > 50:
            errors.append(f"SKU第{index}行销售属性别名超过50字符")
        for alias, mapping_id, known, label in (
            (style_alias, str(sku.get("styleMappingId") or ""), style_mapping_ids, "款式"),
            (size_alias, str(sku.get("sizeMappingId") or ""), size_mapping_ids, "尺寸"),
        ):
            if alias in known and known[alias] != mapping_id:
                errors.append(f"同一{label}使用了不同mappingId：{alias}")
            known[alias] = mapping_id
        try:
            if float(sku.get("jdPrice")) <= 0:
                errors.append(f"SKU第{index}行京东价必须大于0")
            stock = float(sku.get("stock"))
            if stock < 0 or not stock.is_integer():
                errors.append(f"SKU第{index}行库存必须为非负整数")
        except (TypeError, ValueError):
            errors.append(f"SKU第{index}行价格或库存不是有效数字")
        if manifest.get("source"):
            try:
                pricing = (manifest.get("source") or {}).get("pricing") or {}
                below = Decimal(str(pricing.get("belowCompetitor", "0.10")))
                national_rate = Decimal(str(pricing.get("nationalSubsidyRate", "0.15")))
                activity_rate = Decimal(str(pricing.get("activityDiscountRate", "0.15")))
                coupon = Decimal(str(pricing.get("coupon", "0")))
                source_price = Decimal(str(sku["sourcePrice"]))
                actual_jd_price = Decimal(str(sku["jdPrice"]))
                expected_jd_price = (
                    (source_price - below + coupon)
                    / ((Decimal("1") - national_rate) * (Decimal("1") - activity_rate))
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                expected_arrival = (
                    actual_jd_price
                    * (Decimal("1") - national_rate)
                    * (Decimal("1") - activity_rate)
                    - coupon
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                target_arrival = (source_price - below).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                if actual_jd_price != expected_jd_price:
                    errors.append(
                        f"SKU第{index}行京东价计算不一致：应为{expected_jd_price}，当前{actual_jd_price}"
                    )
                if expected_arrival != target_arrival:
                    errors.append(
                        f"SKU第{index}行预计到手价不等于同行价减0.10："
                        f"预计{expected_arrival}，目标{target_arrival}"
                    )
            except (KeyError, InvalidOperation, TypeError, ValueError):
                errors.append(f"SKU第{index}行缺少有效同行售价，禁止自动上架")
    if product.get("vendorSkuPrefix") or any(str(sku.get("vendorSku") or "").strip() for sku in skus):
        expected_style_orders = set(range(1, len(style_orders) + 1))
        if set(style_orders.values()) != expected_style_orders:
            errors.append("款式顺序必须从1开始连续递增，才能生成10000、10001……商家编码")
        try:
            sorted_sizes = sorted(size_orders, key=size_dimensions)
            expected_size_orders = {alias: index for index, alias in enumerate(sorted_sizes, start=1)}
            for alias, expected_order in expected_size_orders.items():
                if size_orders.get(alias) != expected_order:
                    errors.append(f"尺寸顺序不符合从小到大规则：{alias}应为{expected_order}")
        except ValueError:
            expected_orders = set(range(1, len(size_orders) + 1))
            if set(size_orders.values()) != expected_orders:
                errors.append("非物理尺寸规格顺序必须从1开始连续递增")
    images = manifest.get("images") or []
    roles = {item.get("role") for item in images}
    for role in ("spu_main", "sku_main"):
        if role not in roles:
            errors.append(f"缺少 role={role} 的图片")
    if sum(item.get("role") == "spu_main" for item in images) > 10:
        errors.append("SPU主图最多10张")
    if sum(item.get("role") == "detail" for item in images) > 50:
        errors.append("详情图最多50张")
    sku_image_keys = {str(item.get("skuKey") or "") for item in images if item.get("role") == "sku_main"}
    shared_sku_image_count = sum(
        item.get("role") == "spu_main" and int(item.get("order") or 0) > 1
        for item in images
    )
    for sku_key in sku_keys:
        if sku_key not in sku_image_keys:
            errors.append(f"SKU缺少方图：{sku_key}")
        sku_specific_count = sum(
            item.get("role") == "sku_main" and str(item.get("skuKey") or "") == sku_key
            for item in images
        )
        if sku_specific_count != 1:
            errors.append(f"每个SKU必须恰好有1张专属首图：{sku_key} 当前{sku_specific_count}张")
        if sku_specific_count + shared_sku_image_count > 10:
            errors.append(
                f"SKU方图超过10张：{sku_key} 专属图{sku_specific_count}张，公共图{shared_sku_image_count}张"
            )
    image_keys: set[str] = set()
    valid_media_ids = {
        str(item.get("categoryId")) for item in reference.get("media", {}).get("categories", [])
    }
    for index, item in enumerate(images, start=1):
        image_key = str(item.get("imageKey") or "")
        if image_key in image_keys:
            errors.append(f"图片编号重复：{image_key}")
        image_keys.add(image_key)
        local_path = str(item.get("localPath") or "").strip()
        if not local_path:
            errors.append(f"图片第{index}行缺少localPath")
            continue
        category_id = item.get("mediaCategoryId") or reference["media"]["defaultCategoryId"]
        if str(category_id) not in valid_media_ids:
            errors.append(f"图片第{index}行素材分类ID无效：{category_id}")
        image_errors, image_warnings = validate_image(item, resolve_path(path, local_path))
        errors += image_errors
        warnings += image_warnings
    video = manifest.get("video") or {}
    video_local_path = str(video.get("localPath") or "").strip()
    if video_local_path:
        resolved_video = resolve_path(path, video_local_path)
        if not resolved_video.is_file():
            errors.append(f"源商品存在视频，但本地视频文件缺失：{resolved_video}")
        else:
            warnings.append(
                f"主图视频已下载归档；商品发布后由素材接口自动上传、转码并逐SKU绑定：{resolved_video}"
            )
    if any(item.get("role") == "detail" for item in images):
        warnings.append("详情图按输入清单全部保留；脚本不因图片中的价格、榜单或宣传文字自动剔除图片")
    return errors, warnings


def property_item(key: str, value: Any) -> dict[str, Any]:
    return {"key": key, "value": value}


def sale_attribute_components(
    sku: dict[str, Any], include_size: bool, definitions: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    definitions = definitions or {
        "style": {"componentCode": "sale-prop-1001080021", "attrName": "颜色"},
        "size": {"componentCode": "sale-prop-1001080022", "attrName": "尺寸"},
    }
    style_definition = definitions.get("style")
    if not style_definition:
        return []
    style_properties = [
        property_item("attrValueAlias", f"[{sku['styleAlias']}]") ,
        property_item("attrName", style_definition.get("attrName") or "颜色"),
        property_item("mappingId", str(sku["styleMappingId"])),
        property_item("index", "1"),
        property_item("attrValuesSeqNo", int(sku["styleOrder"])),
    ]
    style_value = style_definition.get("valueId")
    if style_definition.get("useAliasAsValue"):
        style_value = sku["styleAlias"]
    if style_value:
        style_properties.insert(0, property_item("saleAttrValues", f"[{style_value}]"))
    components = [{
        "componentCode": style_definition["componentCode"],
        "properties": style_properties,
    }]
    size_definition = definitions.get("size")
    if include_size and size_definition:
        size_properties = [
            property_item("attrValueAlias", f"[{sku['sizeAlias']}]") ,
            property_item("attrName", size_definition.get("attrName") or "尺寸"),
            property_item("mappingId", str(sku["sizeMappingId"])),
            property_item("index", "2"),
            property_item("attrValuesSeqNo", int(sku["sizeOrder"])),
        ]
        size_value = size_definition.get("valueId")
        if size_definition.get("useAliasAsValue"):
            size_value = sku["sizeAlias"]
        if size_value:
            size_properties.insert(0, property_item("saleAttrValues", f"[{size_value}]"))
        components.append({
            "componentCode": size_definition["componentCode"],
            "properties": size_properties,
        })
    return components


def uploaded_image_url(item: dict[str, Any]) -> str:
    value = str((item.get("upload") or {}).get("imgUrl") or "").strip()
    if not value:
        raise JdoError(f"图片尚未上传：{item.get('imageKey')}")
    return value


def detail_image_url(value: str) -> str:
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("//"):
        return "https:" + value
    return "https://img10.360buyimg.com/imgzone/" + value.lstrip("/")


def ordered_images(images: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    return sorted(
        (item for item in images if item.get("role") == role),
        key=lambda item: int(item.get("order") or 0),
    )


def sku_gallery_images(
    sku_key: str,
    sku_images: dict[str, list[dict[str, Any]]],
    shared_images: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    specific_images = sku_images.get(sku_key, [])
    if len(specific_images) != 1:
        raise JdoError(f"每个SKU必须恰好有1张专属首图：{sku_key} 当前{len(specific_images)}张")
    result = [*specific_images, *shared_images]
    if len(result) > 10:
        raise JdoError(f"SKU方图最多10张：{sku_key} 当前{len(result)}张")
    return result


def build_publish_schema(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    image_mode_errors = multi_set_manifest_errors(manifest)
    if image_mode_errors:
        raise JdoError("；".join(image_mode_errors))
    product = manifest["product"]
    include_size_sale_attribute = bool(product.get("includeSizeSaleAttribute", True))
    sale_attribute_definitions = product.get("saleAttributeComponents") or None
    images = manifest.get("images") or []
    spu_images = ordered_images(images, "spu_main")
    detail_images = ordered_images(images, "detail")
    shared_sku_images = [item for item in spu_images if int(item.get("order") or 0) > 1]
    sku_images: dict[str, list[dict[str, Any]]] = {}
    for item in images:
        if item.get("role") != "sku_main":
            continue
        sku_images.setdefault(str(item.get("skuKey")), []).append(item)
    for sku_key in sku_images:
        sku_images[sku_key].sort(key=lambda item: int(item.get("order") or 0))

    components: list[dict[str, Any]] = [
        {"componentCode": "WareTitle", "value": product["title"]},
        {"componentCode": "WareBrand", "value": int(product["brandId"])},
        {
            "componentCode": "WareSkuImg",
            "value": [
                {
                    "componentCode": "img",
                    "properties": [
                        property_item("imgRefId", "0000000000"),
                        property_item("colorId", "0000000000"),
                        property_item("orderSort", str(index)),
                        property_item("imgUrl", uploaded_image_url(item)),
                    ],
                }
                for index, item in enumerate(spu_images, start=1)
            ],
        },
    ]
    if str(product.get("itemNumber") or "").strip():
        components.append({"componentCode": "WareItemnum", "value": str(product["itemNumber"]).strip()})
    if detail_images:
        detail_html = "".join(
            f"<p><img style='max-width:790px;' src='{detail_image_url(uploaded_image_url(item))}'/></p>"
            for item in detail_images
        )
        components.append({
            "componentCode": "WareEditor",
            "value": [{
                "componentCode": "detail",
                "properties": [
                    property_item("desc", detail_html),
                    property_item("zhuangBaUseFlag", "0"),
                    property_item("userAgent", "pc"),
                ],
            }],
        })
    components.append({"componentCode": "WareSevenDayReturn", "value": product["sevenDayReturnCode"]})

    category_values = []
    for item in manifest.get("categoryAttributes") or []:
        properties = [property_item("attrValues", ",".join(str(value) for value in item.get("valueIds") or []))]
        if item.get("percentage"):
            properties.append(property_item("expands", ",".join(item["percentage"])))
        if item.get("unit"):
            properties.append(property_item("units", ",".join(item["unit"])))
        category_values.append({"componentCode": item["componentCode"], "properties": properties})
    components.append({"componentCode": "CategoryGroupList", "value": category_values})
    components.append({"componentCode": "WareTransport", "value": str(product["freightTemplateId"])})

    sku_values = []
    for sku in manifest.get("skus") or []:
        style_mapping_id = str(sku["styleMappingId"])
        gallery_images = sku_gallery_images(str(sku["skuKey"]), sku_images, shared_sku_images)
        sku_components = [
            {"componentCode": "SkuJdprice", "value": str(sku["jdPrice"])},
            {"componentCode": "SkuStockNum", "value": int(sku["stock"])},
            {"componentCode": "SkuVendorSku", "value": resolved_vendor_sku(manifest, sku)},
        ]
        sku_attributes = sku.get("skuAttributes")
        if sku_attributes is None:
            sku_attributes = [
                {"componentCode": "prop-sku-243118", "valueId": sku["mountingValueId"]},
                {"componentCode": "prop-sku-243053", "valueId": sku["subjectValueId"]},
            ]
        if sku_attributes:
            sku_components.append({
                "componentCode": "SkuAttr",
                "value": [
                    {
                        "componentCode": item["componentCode"],
                        "properties": [property_item("attrValues", f"[{item['valueId']}]")],
                    }
                    for item in sku_attributes
                ],
            })
        sku_components.extend([
            {"componentCode": "SkuEnableStatus", "value": "1" if sku.get("enabled") else "2"},
            {
                "componentCode": "SkuMainImage",
                "value": [
                    {
                        "componentCode": "img",
                        "properties": [
                            property_item("imgUrl", uploaded_image_url(image)),
                            property_item("isGgt", 1 if index == 1 else 0),
                            property_item("isPrimary", 1 if index == 1 else 0),
                            property_item("imgRefId", style_mapping_id),
                            property_item("colorId", style_mapping_id),
                            property_item("orderSort", index),
                        ],
                    }
                    for index, image in enumerate(gallery_images, start=1)
                ],
            },
        ])
        sale_components = sale_attribute_components(
            sku, include_size_sale_attribute, sale_attribute_definitions,
        )
        if sale_components:
            sku_components.append({"componentCode": "VenderCategoryValue", "value": sale_components})
        sku_values.append({
            "componentCode": "SKU",
            "value": sku_components,
        })
    components.append({"componentCode": "SKUS", "value": sku_values})
    return components


def write_publish_payloads(input_path: Path, output_dir: Path) -> dict[str, Any]:
    manifest = load_json(input_path)
    schema_components = build_publish_schema(manifest)
    schema_text = json.dumps(schema_components, ensure_ascii=False, separators=(",", ":"))
    category_id = int(manifest["category"]["id"])
    product = manifest["product"]
    image_urls = sorted({uploaded_image_url(item) for item in manifest.get("images") or []})
    validation_body = {
        "scene": "pop",
        "schema": schema_text,
        "productApplyDTO": {
            "brandId": int(product["brandId"]),
            "categoryDTO": {"lastCategoryId": category_id},
        },
    }
    publish_body = {
        "scene": "pop",
        "schema": schema_text,
        "productApplyDTO": {
            "categoryDTO": {"lastCategoryId": category_id},
            "saveActionType": 1,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "schema": output_dir / "schema.json",
        "validation": output_dir / "validation-body.json",
        "publish": output_dir / "publish-body.json",
    }
    paths["schema"].write_text(json.dumps(schema_components, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["validation"].write_text(json.dumps(validation_body, ensure_ascii=False), encoding="utf-8")
    paths["publish"].write_text(json.dumps(publish_body, ensure_ascii=False), encoding="utf-8")
    return {
        "skuCount": len(manifest.get("skus") or []),
        "uniqueImageCount": len(image_urls),
        "schemaBytes": len(schema_text.encode("utf-8")),
        "paths": {key: str(value) for key, value in paths.items()},
    }


def upload(zone_id: str, category_id: int, path: Path) -> dict[str, Any]:
    body = {
        "imgBase64": base64.b64encode(path.read_bytes()).decode("ascii"),
        "zoneImageDTO": {"imgName": path.name, "categoryId": category_id},
    }
    body_path = JDO_RUNTIME.parent / f"upload-{uuid.uuid4().hex}.json"
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    try:
        return run_jdo_large(
            url="/rest/sp-product/v0/media-zones/{zoneId}/images",
            method="POST",
            body_path=body_path,
            params={"zoneId": zone_id},
            skill_id="jdo-product-material",
            yes=True,
        ).get("data") or {}
    finally:
        body_path.unlink(missing_ok=True)


def command_refresh(args: argparse.Namespace) -> int:
    result = refresh_reference(
        args.output, args.category_id, args.category_name, args.brand_category_id,
    )
    print(json.dumps({
        "ok": True,
        "output": str(args.output),
        "brandCount": len(result["brands"]),
        "requiredComponentCount": sum(item["required"] for item in result["componentCatalog"]),
        "defaultBrand": {"id": -1, "name": "无品牌"},
        "defaultMediaCategory": {
            "id": result["media"]["defaultCategoryId"],
            "name": result["media"]["defaultCategoryName"],
        },
        "warnings": result["warnings"],
    }, ensure_ascii=False, indent=2))
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    errors, warnings = validate_manifest(args.input, load_json(args.input), load_json(args.reference))
    print(json.dumps({
        "ok": not errors,
        "errorCount": len(errors),
        "warningCount": len(warnings),
        "errors": errors,
        "warnings": warnings,
    }, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


def command_upload(args: argparse.Namespace) -> int:
    manifest = load_json(args.input)
    reference = load_json(args.reference)
    errors, warnings = validate_manifest(args.input, manifest, reference)
    if errors:
        print(json.dumps({"ok": False, "stage": "preflight", "errors": errors}, ensure_ascii=False, indent=2))
        return 2
    if args.confirm != "UPLOAD_TO_JD":
        raise JdoError("真实上传必须传 --confirm UPLOAD_TO_JD")

    output = args.output or ROOT / "outputs" / "image-uploads" / datetime.now().strftime("%Y%m%d-%H%M%S") / "listing.uploaded.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    result = deepcopy(manifest)
    existing = load_json(output) if output.exists() else None
    if existing and [item.get("imageKey") for item in existing.get("images") or []] != [
        item.get("imageKey") for item in manifest.get("images") or []
    ]:
        raise JdoError("断点文件与当前图片清单不一致，请更换 --output 路径")
    if existing:
        existing_uploads = {
            item.get("imageKey"): item.get("upload")
            for item in existing.get("images") or []
            if (item.get("upload") or {}).get("imgUrl")
        }
        for item in result.get("images") or []:
            if item.get("imageKey") in existing_uploads:
                item["upload"] = existing_uploads[item["imageKey"]]
    cache: dict[str, dict[str, Any]] = {}
    for item in result.get("images") or []:
        if (item.get("upload") or {}).get("imgUrl"):
            image_path = resolve_path(args.input, item["localPath"])
            cache[hashlib.sha256(image_path.read_bytes()).hexdigest()] = item["upload"]
    uploaded_count = 0
    for item in result.get("images") or []:
        if (item.get("upload") or {}).get("imgUrl"):
            continue
        image_path = resolve_path(args.input, item["localPath"])
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if digest not in cache:
            category_id = int(item.get("mediaCategoryId") or reference["media"]["defaultCategoryId"])
            cache[digest] = upload(reference["media"]["zoneId"], category_id, image_path)
            uploaded_count += 1
            print(json.dumps({
                "stage": "upload",
                "uploadedFiles": uploaded_count,
                "file": image_path.name,
            }, ensure_ascii=False), flush=True)
        data = cache[digest]
        item["upload"] = {
            "status": "uploaded",
            "imgId": data.get("imgId"),
            "imgUrl": data.get("imgUrl"),
            "width": data.get("imgWidth"),
            "height": data.get("imgHeight"),
            "size": data.get("imgSize"),
            "uploadedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "uploadedFiles": uploaded_count,
        "warningCount": len(warnings),
        "warnings": warnings,
        "output": str(output),
    }, ensure_ascii=False, indent=2))
    return 0


def command_build_payload(args: argparse.Namespace) -> int:
    result = write_publish_payloads(args.input, args.output_dir)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


def command_validate_jd(args: argparse.Namespace) -> int:
    response = run_jdo_large(
        url="/rest/sp-product/v0/product-schemas/validations",
        method="POST",
        body_path=args.body,
        skill_id="jdo-product-publish",
        yes=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
    results = (response.get("data") or {}).get("wareKeeperDetectResultList") or []
    strong = [item for item in results if int(item.get("detectResult") or 0) == 1]
    print(json.dumps({
        "ok": not strong,
        "strongViolationCount": len(strong),
        "violationCount": len(results),
        "violations": results,
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 0 if not strong else 2


def command_publish(args: argparse.Namespace) -> int:
    if args.confirm != "PUBLISH_TO_JD":
        raise JdoError("真实发品必须传 --confirm PUBLISH_TO_JD")
    response = run_jdo_large(
        url="/rest/sp-product/v0/product-applies",
        method="POST",
        body_path=args.body,
        skill_id="jdo-product-publish",
        yes=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "data": response.get("data"), "output": str(args.output)}, ensure_ascii=False, indent=2))
    return 0


def command_edit_product(args: argparse.Namespace) -> int:
    if args.confirm != "EDIT_EXISTING_PRODUCT":
        raise JdoError("整版编辑已有商品必须传 --confirm EDIT_EXISTING_PRODUCT")
    body = load_json(args.body)
    product_apply = body.get("productApplyDTO") or {}
    if int(product_apply.get("productId") or 0) != args.product_id:
        raise JdoError("编辑请求中的productId与命令参数不一致")
    if int(product_apply.get("saveActionType") or 0) != 0:
        raise JdoError("整版编辑必须使用saveActionType=0保持商品状态不变")
    schema = json.loads(str(body.get("schema") or "[]"))
    sku_components = next(
        (item.get("value") or [] for item in schema if item.get("componentCode") == "SKUS"),
        [],
    )
    if not sku_components:
        raise JdoError("编辑Schema缺少SKU集合")
    sku_ids = []
    for sku in sku_components:
        if any(item.get("componentCode") == "SkuStockNum" for item in sku.get("value") or []):
            raise JdoError("已有商品整版编辑禁止携带SkuStockNum；请用库存专用接口单独设置")
        sku_id_component = next(
            (item for item in sku.get("value") or [] if item.get("componentCode") == "SkuId"),
            None,
        )
        if not sku_id_component:
            raise JdoError("编辑Schema存在未携带SkuId的SKU，已阻止提交")
        sku_ids.append(int(sku_id_component["value"]))
    if len(set(sku_ids)) != len(sku_ids):
        raise JdoError("编辑Schema中的SkuId重复")
    response = run_jdo_large(
        url="/rest/sp-product/v0/product-applies",
        method="POST",
        body_path=args.body,
        skill_id="jdo-product-publish",
        yes=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "productId": args.product_id,
        "skuCount": len(sku_ids),
        "data": response.get("data"),
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 0


def sale_aliases(item: dict[str, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for attribute in item.get("saleAttrs") or item.get("saleAttributeDTOList") or []:
        value = attribute.get("attrValueAlias")
        if value is None:
            values = attribute.get("attrbuteValueAlias") or attribute.get("attributeValueAlias") or []
            value = values[0] if values else ""
        aliases[str(attribute.get("attrId") or attribute.get("attributeId"))] = str(value)
    return aliases


def normalized_sale_alias(value: str) -> str:
    return value.replace("[", "").replace("]", "")


def query_sku_stocks(sku_ids: list[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for start in range(0, len(sku_ids), 20):
        rows.extend(get(
            "jdo-product-status",
            "/rest/sp-product/v0/sku-stocks",
            {"skuIdList": sku_ids[start:start + 20], "venderScene": 1},
        ).get("data") or [])
    return rows


def style_sale_attribute_id(manifest: dict[str, Any]) -> str:
    component_code = str(
        ((((manifest.get("product") or {}).get("saleAttributeComponents") or {}).get("style") or {}).get("componentCode"))
        or "sale-prop-1001080021"
    )
    match = re.fullmatch(r"sale-prop-(\d+)", component_code)
    if not match:
        raise JdoError(f"款式销售属性组件编码无效：{component_code}")
    return match.group(1)


def online_style_color_ids(detail: dict[str, Any], style_attribute_id: str = "1001080021") -> dict[str, str]:
    result: dict[str, str] = {}
    for sku in detail.get("skuList") or []:
        for attribute in sku.get("saleAttrs") or []:
            if str(attribute.get("attrId")) != style_attribute_id:
                continue
            alias = normalized_sale_alias(str(attribute.get("attrValueAlias") or ""))
            color_id = str(attribute.get("attrValues") or attribute.get("mappingId") or "")
            if not alias or not color_id:
                continue
            if alias in result and result[alias] != color_id:
                raise JdoError(f"线上同一款式对应多个颜色ID：{alias}")
            result[alias] = color_id
    return result


def build_shared_image_update_payload(
    manifest: dict[str, Any], detail: dict[str, Any], product_id: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    shared_images = [
        item
        for item in ordered_images(manifest.get("images") or [], "spu_main")
        if int(item.get("order") or 0) > 1
    ]
    if not shared_images:
        raise JdoError("没有可追加到SKU的公共主图（需要SPU主图第2张及以后）")
    if len(shared_images) > 9:
        raise JdoError(f"公共主图最多9张，当前{len(shared_images)}张")

    style_rows: dict[str, dict[str, Any]] = {}
    for sku in manifest.get("skus") or []:
        alias = normalized_sale_alias(str(sku.get("styleAlias") or ""))
        current = style_rows.get(alias)
        if current is None or int(sku.get("styleOrder") or 0) < int(current.get("styleOrder") or 0):
            style_rows[alias] = sku
    online_ids = online_style_color_ids(detail, style_sale_attribute_id(manifest))
    missing_styles = sorted(set(style_rows) - set(online_ids))
    if missing_styles:
        raise JdoError(f"线上找不到{len(missing_styles)}个款式颜色ID：{missing_styles[:5]}")
    online_groups = {
        str(group.get("uuid")): group.get("imageInfoList") or []
        for group in (detail.get("material") or {}).get("mainImages") or []
    }

    patch_operations: list[dict[str, Any]] = []
    position_update_count = 0
    for alias, sku in sorted(style_rows.items(), key=lambda item: int(item[1].get("styleOrder") or 0)):
        color_id = online_ids[alias]
        current_images = sorted(
            online_groups.get(color_id) or [],
            key=lambda item: int(item.get("orderSort") or 0),
        )
        if not current_images or not current_images[0].get("imgUrl"):
            raise JdoError(f"线上款式缺少可保留的专属首图：{alias} ({color_id})")
        gallery = [(1, str(current_images[0]["imgUrl"]))]
        gallery.extend(
            (order_sort, uploaded_image_url(image))
            for order_sort, image in enumerate(shared_images, start=2)
        )
        image_values: list[dict[str, Any]] = []
        for order_sort, image_url in gallery:
            image_values.append({
                "componentCode": "img",
                "inputType": "multiText",
                "properties": [
                    property_item("colorId", color_id),
                    property_item("imgUrl", image_url),
                    property_item("imgRefId", color_id),
                    property_item("orderSort", str(order_sort)),
                ],
            })
        position_update_count += len(image_values)
        component = {
            "componentCode": "WareSkuImg",
            "inputType": "multiText",
            "value": image_values,
        }
        patch_operations.append({
            "path": "product",
            "value": json.dumps(component, ensure_ascii=False, separators=(",", ":")),
        })
    body = {
        "applyId": str(product_id),
        "opType": 3,
        "scene": "pop",
        "productInfo": {
            "productId": product_id,
            "categoryDetail": {"lastCategoryId": int(manifest["category"]["id"])},
        },
        "productApplyDTO": {"id": str(product_id)},
        "patchOperationList": patch_operations,
    }
    return body, {
        "styleCount": len(style_rows),
        "sharedImageCount": len(shared_images),
        "patchOperationCount": len(patch_operations),
        "positionUpdateCount": position_update_count,
        "targetPositions": list(range(1, len(shared_images) + 2)),
        "preservesCurrentFirstImage": True,
    }


def command_build_shared_image_update(args: argparse.Namespace) -> int:
    manifest = load_json(args.input)
    detail = get(
        "jdo-product-update",
        "/rest/sp-product/v0/products/{productId}",
        {"productId": args.product_id, "scene": "pop"},
    ).get("data") or {}
    body, summary = build_shared_image_update_payload(manifest, detail, args.product_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": True, **summary, "output": str(args.output)}, ensure_ascii=False, indent=2))
    return 0


def command_apply_product_update(args: argparse.Namespace) -> int:
    if args.confirm != "UPDATE_PRODUCT_IMAGES":
        raise JdoError("真实修改商品图片必须传 --confirm UPDATE_PRODUCT_IMAGES")
    response = run_jdo_large(
        url="/rest/sp-product/v0/product-applies/{applyId}",
        method="PATCH",
        body_path=args.body,
        params={"applyId": str(args.product_id)},
        skill_id="jdo-product-update",
        yes=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "data": response.get("data"), "output": str(args.output)}, ensure_ascii=False, indent=2))
    return 0


def shared_image_audit(manifest: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    shared_items = [
        item
        for item in ordered_images(manifest.get("images") or [], "spu_main")
        if int(item.get("order") or 0) > 1
    ]
    compare_urls = all(str((item.get("upload") or {}).get("imgUrl") or "").strip() for item in shared_items)
    expected_urls = [uploaded_image_url(item) for item in shared_items] if compare_urls else []
    style_aliases = {
        normalized_sale_alias(str(sku.get("styleAlias") or ""))
        for sku in manifest.get("skus") or []
    }
    online_ids = online_style_color_ids(detail, style_sale_attribute_id(manifest))
    groups = {
        str(group.get("uuid")): group.get("imageInfoList") or []
        for group in (detail.get("material") or {}).get("mainImages") or []
    }
    errors: list[str] = []
    mismatches: list[dict[str, Any]] = []
    image_count_distribution: Counter[int] = Counter()
    for alias in sorted(style_aliases):
        color_id = online_ids.get(alias)
        if not color_id:
            errors.append(f"线上缺少款式颜色ID：{alias}")
            continue
        images = groups.get(color_id)
        if images is None:
            errors.append(f"线上缺少款式图片组：{alias} ({color_id})")
            continue
        image_count_distribution[len(images)] += 1
        positions = {int(image.get("orderSort") or 0): str(image.get("imgUrl") or "") for image in images}
        for position in range(2, len(shared_items) + 2):
            actual_url = positions.get(position)
            if not actual_url:
                mismatches.append({
                    "style": alias,
                    "position": position,
                    "expected": expected_urls[position - 2] if compare_urls else "存在公共主图",
                    "actual": actual_url,
                })
            elif compare_urls and actual_url != expected_urls[position - 2]:
                mismatches.append({
                    "style": alias,
                    "position": position,
                    "expected": expected_urls[position - 2],
                    "actual": actual_url,
                })
        if 1 not in positions:
            errors.append(f"款式缺少专属首图：{alias}")
        if len(images) != len(shared_items) + 1:
            errors.append(f"款式图片数量不正确：{alias} 当前{len(images)}张")
    if mismatches:
        errors.append(f"{len(mismatches)}个款式公共图片位置不一致")
    return {
        "ok": not errors,
        "styleCount": len(style_aliases),
        "mode": "multi_set",
        "sharedImageCount": len(shared_items),
        "expectedImagesPerStyle": len(shared_items) + 1,
        "urlComparisonPerformed": compare_urls,
        "imageCountDistribution": dict(image_count_distribution),
        "mismatchCount": len(mismatches),
        "mismatches": mismatches[:20],
        "errors": errors[:50],
    }


def command_verify_shared_images(args: argparse.Namespace) -> int:
    manifest = load_json(args.input)
    apply_detail = get(
        "jdo-product-update",
        "/rest/sp-product/v0/product-apply/{applyId}",
        {"applyId": str(args.product_id), "scene": "pop"},
    ).get("data") or {}
    product_detail = get(
        "jdo-product-update",
        "/rest/sp-product/v0/products/{productId}",
        {"productId": args.product_id, "scene": "pop"},
    ).get("data") or {}
    pending = shared_image_audit(manifest, apply_detail)
    online = shared_image_audit(manifest, product_detail)
    result = {
        "ok": pending["ok"] and online["ok"],
        "productId": args.product_id,
        "applyInfo": apply_detail.get("applyInfo") or {},
        "pending": pending,
        "online": online,
        "verifiedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


def command_verify_product(args: argparse.Namespace) -> int:
    manifest = load_json(args.input)
    include_size_sale_attribute = bool(
        (manifest.get("product") or {}).get("includeSizeSaleAttribute", True)
    )
    sale_definitions = (manifest.get("product") or {}).get("saleAttributeComponents") or {
        "style": {"componentCode": "sale-prop-1001080021"},
        "size": {"componentCode": "sale-prop-1001080022"},
    }
    style_attr_id = str((sale_definitions.get("style") or {}).get("componentCode") or "").removeprefix("sale-prop-")
    size_attr_id = str((sale_definitions.get("size") or {}).get("componentCode") or "").removeprefix("sale-prop-")
    expected = {
        (
            normalized_sale_alias(str(sku["styleAlias"])),
            *(
                [normalized_sale_alias(str(sku["sizeAlias"]))]
                if include_size_sale_attribute else []
            ),
        ): sku
        for sku in manifest.get("skus") or []
    }
    detail = get(
        "jdo-product-status",
        "/rest/sp-product/v0/products/{productId}",
        {"productId": args.product_id, "scene": "pop"},
    ).get("data") or {}
    product_info = detail.get("productInfo") or {}
    actual_skus = detail.get("skuList") or []

    errors: list[str] = []
    warnings: list[str] = []
    if len(actual_skus) != len(expected):
        errors.append(f"SKU总数不一致：京东{len(actual_skus)}，预期{len(expected)}")
    actual_by_combination: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in actual_skus:
        aliases = sale_aliases(item)
        combination = (
            normalized_sale_alias(aliases.get(style_attr_id, "")),
            *(
                [normalized_sale_alias(aliases.get(size_attr_id, ""))]
                if include_size_sale_attribute else []
            ),
        )
        actual_by_combination[combination] = item
    missing = sorted(set(expected) - set(actual_by_combination))
    unexpected = sorted(set(actual_by_combination) - set(expected))
    if missing:
        errors.append(f"京东缺少{len(missing)}个规格组合")
    if unexpected:
        errors.append(f"京东多出{len(unexpected)}个规格组合")
    price_mismatches = []
    for combination in sorted(set(expected) & set(actual_by_combination)):
        expected_price = round(float(expected[combination]["jdPrice"]), 2)
        actual_price = round(float((actual_by_combination[combination].get("priceInfo") or {}).get("jdPrice") or 0), 2)
        if expected_price != actual_price:
            price_mismatches.append({"combination": combination, "expected": expected_price, "actual": actual_price})
    if price_mismatches:
        errors.append(f"{len(price_mismatches)}个SKU价格不一致")
    vendor_sku_mismatches = []
    if any(str(sku.get("vendorSku") or "").strip() for sku in manifest.get("skus") or []):
        for combination in sorted(set(expected) & set(actual_by_combination)):
            expected_vendor_sku = resolved_vendor_sku(manifest, expected[combination])
            actual_vendor_sku = str(actual_by_combination[combination].get("outerId") or "").strip()
            if expected_vendor_sku != actual_vendor_sku:
                vendor_sku_mismatches.append({
                    "combination": combination,
                    "expected": expected_vendor_sku,
                    "actual": actual_vendor_sku,
                })
        if vendor_sku_mismatches:
            errors.append(f"{len(vendor_sku_mismatches)}个SKU商家编码不一致")
    status_counts = Counter(
        int((item.get("skuStatus") or {}).get("onOffShelfStatus") or 0)
        for item in actual_skus
    )
    if set(status_counts) != {1}:
        errors.append(f"SKU并非全部处于上架状态：{dict(status_counts)}")
    enable_counts = Counter(int(item.get("skuEnableStatus") or 0) for item in actual_skus)
    if set(enable_counts) != {1}:
        errors.append(f"SKU并非全部启用：{dict(enable_counts)}")
    stock_rows = query_sku_stocks([int(item["skuId"]) for item in actual_skus])
    stock_counts = Counter(int(item.get("stockNum") or 0) for item in stock_rows)
    if len(stock_rows) != len(actual_skus):
        errors.append(f"库存接口返回SKU数量不一致：{len(stock_rows)}，预期{len(actual_skus)}")
    if set(stock_counts) != {10000}:
        errors.append(f"SKU库存并非全部为10000：{dict(stock_counts)}")
    product_status = int((product_info.get("productStatus") or {}).get("productStatusNew") or 0)
    if product_status != 103:
        errors.append(f"商品未处于POP上架状态：{product_status}")
    detail_image_count = str((product_info.get("productDetailDesc") or {}).get("desc") or "").count("<img")
    expected_detail_count = sum(item.get("role") == "detail" for item in manifest.get("images") or [])
    if detail_image_count != expected_detail_count:
        errors.append(f"详情图数量不一致：京东{detail_image_count}，预期{expected_detail_count}")
    main_image_audit = shared_image_audit(manifest, detail)
    if not main_image_audit["ok"]:
        errors.append("线上多套主图校验未通过：" + "; ".join(main_image_audit["errors"][:5]))
    pricing = (manifest.get("source") or {}).get("pricing") or {}
    pricing_formula_audit = {
        "status": "verified",
        "scope": "逐SKU反推京东价",
        "plannedNationalSubsidyRate": pricing.get("nationalSubsidyRate"),
        "plannedActivityDiscountRate": pricing.get("activityDiscountRate"),
        "plannedCoupon": pricing.get("coupon"),
        "discountExecution": pricing.get("discountExecution"),
        "actualArrivalPriceVerificationRequired": bool(pricing.get("requiresPostPromotionVerification", False)),
    }
    if any(
        str(sku.get("styleAlias") or "") != normalized_sale_alias(str(sku.get("styleAlias") or ""))
        for sku in manifest.get("skus") or []
    ):
        warnings.append("源款式名中的ASCII方括号被京东销售属性语法归一化，不影响SKU规格匹配")
    result = {
        "ok": not errors,
        "productId": args.product_id,
        "productName": product_info.get("productName"),
        "productStatus": product_status,
        "expectedSkuCount": len(expected),
        "actualSkuCount": len(actual_skus),
        "combinationCount": len(actual_by_combination),
        "styleCount": len({combination[0] for combination in actual_by_combination}),
        "sizeCount": (
            len({combination[1] for combination in actual_by_combination})
            if include_size_sale_attribute else 0
        ),
        "skuStatusCounts": dict(status_counts),
        "skuEnableCounts": dict(enable_counts),
        "stockCounts": dict(stock_counts),
        "detailImageCount": detail_image_count,
        "mainImageMode": "multi_set",
        "mainImageAudit": main_image_audit,
        "priceMismatchCount": len(price_mismatches),
        "pricingFormulaAudit": pricing_formula_audit,
        "vendorSkuMismatchCount": len(vendor_sku_mismatches),
        "missingCombinations": missing[:20],
        "unexpectedCombinations": unexpected[:20],
        "errors": errors,
        "warnings": warnings,
        "verifiedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh-reference")
    refresh.add_argument("--output", type=Path, default=REFERENCE)
    refresh.add_argument("--category-id", type=int, default=CATEGORY_ID)
    refresh.add_argument("--category-name", default=CATEGORY_NAME)
    refresh.add_argument("--brand-category-id", type=int)
    refresh.set_defaults(handler=command_refresh)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--input", type=Path, required=True)
    preflight.add_argument("--reference", type=Path, default=REFERENCE)
    preflight.set_defaults(handler=command_preflight)
    upload_images = commands.add_parser("upload-images")
    upload_images.add_argument("--input", type=Path, required=True)
    upload_images.add_argument("--reference", type=Path, default=REFERENCE)
    upload_images.add_argument("--output", type=Path)
    upload_images.add_argument("--confirm", default="")
    upload_images.set_defaults(handler=command_upload)
    build_payload = commands.add_parser("build-payload")
    build_payload.add_argument("--input", type=Path, required=True)
    build_payload.add_argument("--output-dir", type=Path, required=True)
    build_payload.set_defaults(handler=command_build_payload)
    validate_jd = commands.add_parser("validate-jd")
    validate_jd.add_argument("--body", type=Path, required=True)
    validate_jd.add_argument("--output", type=Path, required=True)
    validate_jd.set_defaults(handler=command_validate_jd)
    publish = commands.add_parser("publish")
    publish.add_argument("--body", type=Path, required=True)
    publish.add_argument("--output", type=Path, required=True)
    publish.add_argument("--confirm", default="")
    publish.set_defaults(handler=command_publish)
    edit_product = commands.add_parser("edit-product")
    edit_product.add_argument("--body", type=Path, required=True)
    edit_product.add_argument("--product-id", type=int, required=True)
    edit_product.add_argument("--output", type=Path, required=True)
    edit_product.add_argument("--confirm", default="")
    edit_product.set_defaults(handler=command_edit_product)
    build_shared_image_update = commands.add_parser("build-shared-image-update")
    build_shared_image_update.add_argument("--input", type=Path, required=True)
    build_shared_image_update.add_argument("--product-id", type=int, required=True)
    build_shared_image_update.add_argument("--output", type=Path, required=True)
    build_shared_image_update.set_defaults(handler=command_build_shared_image_update)
    apply_product_update = commands.add_parser("apply-product-update")
    apply_product_update.add_argument("--body", type=Path, required=True)
    apply_product_update.add_argument("--product-id", type=int, required=True)
    apply_product_update.add_argument("--output", type=Path, required=True)
    apply_product_update.add_argument("--confirm", default="")
    apply_product_update.set_defaults(handler=command_apply_product_update)
    verify_shared_images = commands.add_parser("verify-shared-images")
    verify_shared_images.add_argument("--input", type=Path, required=True)
    verify_shared_images.add_argument("--product-id", type=int, required=True)
    verify_shared_images.add_argument("--output", type=Path, required=True)
    verify_shared_images.set_defaults(handler=command_verify_shared_images)
    verify_product = commands.add_parser("verify-product")
    verify_product.add_argument("--input", type=Path, required=True)
    verify_product.add_argument("--product-id", type=int, required=True)
    verify_product.add_argument("--output", type=Path, required=True)
    verify_product.set_defaults(handler=command_verify_product)
    args = parser.parse_args()
    try:
        return args.handler(args)
    except (JdoError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
