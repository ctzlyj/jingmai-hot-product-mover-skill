# -*- coding: utf-8 -*-
"""No-inventory mode phase-1 local build (generic).

The four SKU columns of the source workbook are strictly position-aligned, so
empty cells must NOT be dropped. Padding rows and marketing-only rows are
excluded and recorded instead of being published as fake SKUs.

Batch root, pricing, stock, code prefix/seed and source column names come
from <batch-root>/batch-config.json (see references/no-inventory-mode.md).
"""
from __future__ import annotations
import io, json, re, sys, random, string
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SKILL_SCRIPTS = Path(__file__).resolve().parents[1]
NI_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_SCRIPTS))
sys.path.insert(0, str(NI_SCRIPTS))
import batch as ni_batch  # noqa: E402
from source_pricing import component_source_plan  # noqa: E402

ROOT = ni_batch.root_from_argv()
CONFIG = ni_batch.load_config(ROOT)

DEFAULT_PRICING = {
    "targetArrivalMultiplier": "1.35",
    "belowCompetitor": "0",
    "nationalSubsidyRate": "0",
    "activityDiscountRate": "0.15",
    "coupon": "0",
    "roundingMode": "component",
}
PRICING = {**DEFAULT_PRICING, **(CONFIG.get("pricing") or {})}
STOCK_DEFAULT = int(CONFIG.get("stock") or 200)
SEVEN_DAY_RETURN = str(CONFIG.get("sevenDayReturn") or "1")
BRAND_ID = -1
TITLE_LIMIT = 60
ALIAS_LIMIT = 48
CODE_PREFIX = str(CONFIG.get("codePrefix") or "NI")
BATCH_ID = str(CONFIG.get("batchId") or ROOT.name)
SOURCE_DUMP = str(CONFIG.get("sourceDump") or "source/workbook-1-dump.json")

# Source-workbook column names are configurable; defaults keep compatibility
# with the verified 2026-09 merchant workbook layout.
COLUMNS = {
    "itemId": "商品ID",
    "title": "商品名称",
    "saleLink": "商品销售链接",
    "attributes": "商品属性",
    "skuAttr": "SKU属性",
    "skuPrice": "SKU价格",
    "skuStock": "SKU库存",
    "skuImage": "SKU属性图",
    "mainImage": "主图{i}",
    "detail": "商品描述图",
    "video": "商品视频",
    **(CONFIG.get("columns") or {}),
}
EXTRA_BRANDS = {str(k): list(v) for k, v in (CONFIG.get("extraBrands") or {}).items()}

PLACEHOLDER_RE = re.compile(r"(TB1AHXiGXXXXXXAXVXX|imglazyload|spaceball|placehold)", re.I)

PLATFORM_TERMS = ["淘宝", "天猫", "淘金币", "拼多多", "抖音", "小红书", "京东", "1688",
                  "阿里巴巴", "天猫超市", "官方旗舰店", "旗舰店", "专卖店", "专营店",
                  "企业店", "自营", "快手", "微店"]
GEO_TERMS = ["原装进口", "日本", "德国", "进口", "日式", "德式"]
PROMO_TERMS = ["立减", "厂家直销", "工厂直销", "国补", "国家补贴", "正品", "旗舰", "官方",
               "秒杀", "特价", "限时", "包邮", "买一送一", "第二件半价", "补贴",
               "加购", "收藏", "大牌", "爆款", "热销", "抢购", "下单", "领券", "优惠券",
               "券后", "促销", "送大礼包", "免费送", "包退", "包换",
               "店长推荐", "镇店之宝", "母婴急住", "新房急住", "暴力", "特惠装", "大礼包"]
EXTREME_TERMS = ["全网最低", "最便宜", "最低价", "史上最低", "第一", "顶级", "极致", "绝对",
                 "唯一", "国家级", "世界级", "特级", "冠军", "独家", "史上", "百分百",
                 "王牌", "最好", "最强", "最佳", "极品", "首选", "特效", "万能", "神器"]
PRICE_TERMS = ["到手价", "券后价", "原价", "划线价", "标价", "销售价", "促销价", "活动价",
               "京东价", "淘宝价", "天猫价"]
CLAIM_PATTERNS = [
    r"除醛率\s*\d+(?:\.\d+)?%", r"除味率\s*\d+(?:\.\d+)?%", r"去除率\s*\d+(?:\.\d+)?%",
    r"净化率\s*\d+(?:\.\d+)?%", r"杀菌率\s*\d+(?:\.\d+)?%", r"抑菌率\s*\d+(?:\.\d+)?%",
    r"\d+(?:\.\d+)?%\s*顾客选择", r"\d+(?:\.\d+)?%\s*用户选择", r"\d+(?:\.\d+)?%\s*人选",
    r"权威机构认证", r"权威认证", r"国家专利", r"专利技术", r"检测报告", r"明星代言",
    r"\d+\s*倍",
]
STRONG_MARKETING = ["国补", "立减", "警惕", "认准", "专业认证", "行业优选", "支持对比",
                    "全额退款", "守护家人", "危害健康", "品质更有保障", "假一赔十",
                    "好评", "抢购", "全网", "销量第一", "秒杀"]
SPEC_TOKEN_RE = re.compile(
    r"\d+\s*(?:KG|kg|Kg|kG|G|g|克|斤|包|袋|盒|瓶|片|个|支|卷|米|㎡|L|ML|ml|升|抽|m|M)"
)
ALL_REMOVAL = PLATFORM_TERMS + GEO_TERMS + PROMO_TERMS + EXTREME_TERMS + PRICE_TERMS


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def title_units(value: str) -> int:
    return sum(1 if ord(ch) < 128 else 2 for ch in value)


def truncate_units(value: str, limit: int) -> str:
    out, units, cut = [], 0, False
    for ch in value:
        width = 1 if ord(ch) < 128 else 2
        if units + width > limit:
            cut = True
            break
        out.append(ch)
        units += width
    text = "".join(out).rstrip()
    if cut and text and re.fullmatch(r"[A-Za-z0-9]", text[-1]):
        trimmed = re.sub(r"[A-Za-z0-9]+$", "", text).rstrip()
        if trimmed:
            text = trimmed
    return text


def clean_alias(raw: str, brands: list[str]) -> str:
    text = str(raw or "")
    for term in sorted(set(brands), key=len, reverse=True):
        term = term.strip()
        if not term:
            continue
        if re.fullmatch(r"[A-Za-z0-9]+", term):
            text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", " ", text,
                          flags=re.I)
        else:
            text = text.replace(term, " ")
    for pattern in CLAIM_PATTERNS:
        text = re.sub(pattern, " ", text)
    for term in ALL_REMOVAL:
        text = text.replace(term, " ")
    text = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_marketing_row(raw: str) -> bool:
    text = str(raw or "").strip()
    if not text:
        return False
    if ";" in text or "\uff1b" in text:
        return False
    if SPEC_TOKEN_RE.search(text):
        return False
    return any(term in text for term in STRONG_MARKETING)


def media_files(item_dir: Path, role: str) -> list[Path]:
    if not item_dir.is_dir():
        return []
    return sorted(p for p in item_dir.glob(f"{role}-*") if p.stat().st_size > 200)


def url_list(raw: str) -> list[str]:
    return [u.strip() for u in str(raw or "").split("|")]


def main() -> None:
    dump = load(ROOT / SOURCE_DUMP)
    recs = [r for r in dump["records"] if str(r.get(COLUMNS["itemId"]) or "").strip()]
    mapping = {int(row["row"]): row for row in load(ROOT / "reports" / "category-mapping.json")}
    raw_root = ROOT / "images" / "raw"

    ledger_path = ROOT / "ledger.json"
    ledger = load(ledger_path) if ledger_path.is_file() else {
        "batchId": BATCH_ID, "createdAt": now(), "items": {}, "codes": {}}
    ledger.setdefault("codes", {})
    ledger.setdefault("items", {})
    rng = random.Random(f"{BATCH_ID}-v1")

    def new_code(prefix: str) -> str:
        alphabet = string.ascii_uppercase + string.digits
        while True:
            code = prefix + "".join(rng.choice(alphabet) for _ in range(9))
            if code not in ledger["codes"]:
                ledger["codes"][code] = None
                return code

    def brand_terms(rec: dict) -> list[str]:
        terms: list[str] = []
        try:
            attrs = json.loads(rec[COLUMNS["attributes"]])
        except Exception:
            attrs = {}
        brand = str(attrs.get("品牌") or "").strip()
        if brand and brand not in {"无品牌", "其他家", "无", "其他", "other"}:
            terms.append(brand)
            for part in re.split(r"[/|（(]", brand):
                part = part.strip()
                if len(part) >= 2:
                    terms.append(part)
        terms.extend(EXTRA_BRANDS.get(str(rec[COLUMNS["itemId"]]), []))
        return sorted({t for t in terms if t}, key=len, reverse=True)

    plan_items, skipped, cleanup_rows, alias_report = [], [], [], []
    source_report, pricing_rows = [], []

    for rec in recs:
        item_id = str(rec[COLUMNS["itemId"]])
        m = mapping[rec["_row"]]
        keyword = m["keyword"]
        base = {"sourceRow": rec["_row"], "sourceItemId": item_id, "keyword": keyword}
        if m["status"] != "MAPPED":
            skipped.append({**base, "status": m["status"], "reason": m["reason"],
                            "title": rec[COLUMNS["title"]]})
            continue

        brands = brand_terms(rec)
        raw_title = str(rec[COLUMNS["title"]])
        body = clean_alias(raw_title, brands)
        title = truncate_units(f"{keyword} {body}".strip(), TITLE_LIMIT)
        cleanup_rows.append({"sourceItemId": item_id, "rawTitle": raw_title,
                             "brandsRemoved": brands, "cleanedBody": body,
                             "finalTitle": title, "titleUnits": title_units(title)})

        attrs = url_list(rec[COLUMNS["skuAttr"]])
        prices = url_list(rec[COLUMNS["skuPrice"]])
        stocks = url_list(rec[COLUMNS["skuStock"]])
        images = url_list(rec[COLUMNS["skuImage"]])
        count = len(attrs)
        if not (len(prices) == len(stocks) == len(images) == count):
            skipped.append({**base, "status": "SKIPPED_MISALIGNED_SOURCE_COLUMNS",
                            "reason": f"SKU列长度不一致 attrs={count} prices={len(prices)} "
                                      f"stock={len(stocks)} images={len(images)}",
                            "title": raw_title})
            continue
        if not any(a.strip() for a in attrs):
            skipped.append({**base, "status": "SKIPPED_INCOMPLETE_SOURCE_SKU",
                            "reason": "源表SKU属性/价格/库存全为空，无法确认逐SKU到手价",
                            "title": raw_title})
            continue

        local_files = media_files(raw_root / item_id, "sku")
        non_empty = [(i, url) for i, url in enumerate(images) if url]
        if len(non_empty) != len(local_files):
            skipped.append({**base, "status": "SKIPPED_SKU_IMAGE_MISMATCH",
                            "reason": f"SKU图URL数={len(non_empty)} 本地文件数={len(local_files)}",
                            "title": raw_title})
            continue
        sku_image_by_index = {i: {"sourceIndex": i + 1, "url": url, "file": str(path)}
                              for (i, url), path in zip(non_empty, local_files)}

        includes, excluded = [], []
        for index, (attr, price, stock) in enumerate(zip(attrs, prices, stocks)):
            if not attr.strip():
                excluded.append({"sourceIndex": index, "kind": "PADDING_ROW",
                                 "raw": attr, "price": price, "reason": "源表占位空行"})
                continue
            if is_marketing_row(attr):
                excluded.append({"sourceIndex": index, "kind": "MARKETING_ROW",
                                 "raw": attr, "price": price,
                                 "reason": "源表宣传行（无规格单位且含营销短语），非真实销售规格"})
                continue
            includes.append(index)
        if not includes:
            skipped.append({**base, "status": "SKIPPED_INCOMPLETE_SOURCE_SKU",
                            "reason": "剔除占位/宣传行后无真实SKU", "title": raw_title})
            continue

        main_files = media_files(raw_root / item_id, "main")
        main_urls = []
        for i in range(1, 11):
            value = str(rec.get(COLUMNS["mainImage"].format(i=i)) or "").strip()
            if value and not PLACEHOLDER_RE.search(value):
                main_urls.append(value)
        descs = re.findall(r"<img[^>]+src=[\"']([^\"']+)[\"']", str(rec[COLUMNS["detail"]]))
        descs = [x for x in descs if x and not PLACEHOLDER_RE.search(x)]
        detail_files = media_files(raw_root / item_id, "detail")
        if len(main_urls) != len(main_files):
            skipped.append({**base, "status": "SKIPPED_MAIN_IMAGE_MISMATCH",
                            "reason": f"主图URL数={len(main_urls)} 本地文件数={len(main_files)}",
                            "title": raw_title})
            continue
        if len(descs) != len(detail_files):
            skipped.append({**base, "status": "SKIPPED_DETAIL_IMAGE_MISMATCH",
                            "reason": f"详情图URL数={len(descs)} 本地文件数={len(detail_files)}",
                            "title": raw_title})
            continue
        if not detail_files:
            skipped.append({**base, "status": "SKIPPED_MISSING_DETAIL_MATERIAL",
                            "reason": "源表无任何详情图素材（商品描述图/手机详情/长图链接均为空），"
                                      "不得用主图冒充详情",
                            "title": raw_title})
            continue

        spu_code = ledger["codes"].get(f"spu:{item_id}") or new_code(CODE_PREFIX)
        ledger["codes"][f"spu:{item_id}"] = spu_code

        sku_rows, collisions, seen = [], [], {}
        invalid_price = False
        dropped_after_clean = 0
        for index in includes:
            attr = attrs[index]
            price = prices[index].strip()
            if not re.fullmatch(r"\d+(?:\.\d+)?", price):
                skipped.append({**base, "status": "SKIPPED_INVALID_SOURCE_PRICE",
                                "reason": f"SKU[{index}]价格非法：{price!r}", "title": raw_title})
                invalid_price = True
                break
            parts = [p.strip() for p in re.split(r"[;\uff1b]", attr) if p.strip()]
            alias = clean_alias(" ".join(parts), brands)
            if not alias:
                excluded.append({"sourceIndex": index, "kind": "EMPTY_AFTER_CLEAN",
                                 "raw": attr, "price": price,
                                 "reason": "清理违规词后无有效内容"})
                dropped_after_clean += 1
                continue
            base_alias = alias
            if alias in seen:
                seen[alias] += 1
                alias = f"{base_alias} {seen[base_alias]}"
                collisions.append({"sourceIndex": index, "source": attr,
                                   "duplicateOf": base_alias})
            else:
                seen[alias] = 1
            key = f"sku:{item_id}:{index}"
            vendor_sku = ledger["codes"].get(key) or new_code(spu_code + "-")
            ledger["codes"][key] = vendor_sku
            plan = component_source_plan(price, PRICING)
            sku_rows.append({
                "sourceIndex": index,
                "sourceAttribute": attr,
                "saleAlias": alias,
                "saleAliasUnits": title_units(alias),
                "saleAliasChars": len(alias),
                "sourcePrice": price,
                "targetArrivalPrice": str(plan.target_final_price),
                "jdPrice": str(plan.jd_price),
                "stock": STOCK_DEFAULT,
                "vendorSku": vendor_sku,
                "skuImage": sku_image_by_index.get(index),
            })
            pricing_rows.append({"sourceItemId": item_id, "sourceIndex": index,
                                 "sourcePrice": price,
                                 "targetArrivalPrice": str(plan.target_final_price),
                                 "jdPrice": str(plan.jd_price)})
        if invalid_price:
            continue
        if not sku_rows:
            skipped.append({**base, "status": "SKIPPED_NO_USABLE_SKU",
                            "reason": "全部SKU清理违规词后无有效内容", "title": raw_title})
            continue
        if len(sku_rows) + dropped_after_clean != len(includes):
            skipped.append({**base, "status": "SKIPPED_SKU_PARSE_INCONSISTENT",
                            "reason": f"SKU解析不完整 kept={len(sku_rows)} "
                                      f"dropped={dropped_after_clean} includes={len(includes)}",
                            "title": raw_title})
            continue

        trims = [r for r in sku_rows if r["saleAliasChars"] > ALIAS_LIMIT]
        alias_report.append({"sourceItemId": item_id, "skuCount": len(sku_rows),
                             "collisions": collisions,
                             "overLengthAliases": [{"sourceIndex": r["sourceIndex"],
                                                    "saleAlias": r["saleAlias"],
                                                    "chars": r["saleAliasChars"]} for r in trims]})
        source_report.append({"sourceItemId": item_id, "sourceRow": rec["_row"],
                              "totalSourceRows": count, "keptRows": len(sku_rows),
                              "excludedRows": excluded})

        video_url = str(rec[COLUMNS["video"]] or "").strip()
        video_file = media_files(raw_root / item_id, "video")

        candidate = {
            "sourceItemId": item_id,
            "sourceUrl": rec[COLUMNS["saleLink"]],
            "sourceRow": rec["_row"],
            "keyword": keyword,
            "categoryId": m["categoryId"],
            "categoryName": m["categoryName"],
            "title": title,
            "itemNumber": spu_code,
            "brandId": BRAND_ID,
            "sevenDayReturnCode": SEVEN_DAY_RETURN,
            "stock": STOCK_DEFAULT,
            "pricing": PRICING,
            "mainImages": [{"sourceIndex": i + 1, "url": url, "file": str(path)}
                           for i, (url, path) in enumerate(zip(main_urls, main_files))],
            "detailImages": [{"sourceIndex": i + 1, "url": url, "file": str(path)}
                             for i, (url, path) in enumerate(zip(descs, detail_files))],
            "video": ({"url": video_url, "file": str(video_file[0])}
                      if video_url and video_file else None),
            "excludedSourceRows": excluded,
            "skus": sku_rows,
        }
        save(ROOT / "candidates" / item_id / "source-normalized.json", candidate)
        ledger["items"][item_id] = {
            "sourceRow": rec["_row"], "keyword": keyword, "status": "LOCAL_NORMALIZED",
            "categoryId": m["categoryId"], "categoryName": m["categoryName"],
            "title": title, "itemNumber": spu_code, "skuCount": len(sku_rows),
            "mainImageCount": len(main_files), "detailImageCount": len(detail_files),
            "video": bool(video_url and video_file), "updatedAt": now(),
        }
        plan_items.append({"sourceRow": rec["_row"], "sourceItemId": item_id,
                           "keyword": keyword, "categoryId": m["categoryId"],
                           "categoryName": m["categoryName"], "title": title,
                           "itemNumber": spu_code, "skuCount": len(sku_rows),
                           "excludedRowCount": len(excluded)})

    ledger["updatedAt"] = now()
    save(ledger_path, ledger)
    save(ROOT / "batch-plan.json", {
        "batchId": BATCH_ID, "createdAt": now(),
        "sourceFile": dump["sourceFile"], "sourceSha256": dump["sha256"],
        "mode": "local_capture_only_no_publish", "pricing": PRICING,
        "itemCount": len(plan_items), "skippedCount": len(skipped),
        "items": plan_items, "skipped": skipped})
    save(ROOT / "reports" / "title-cleanup.json", cleanup_rows)
    save(ROOT / "reports" / "sku-alias-report.json", alias_report)
    save(ROOT / "reports" / "sku-source-report.json", source_report)
    save(ROOT / "reports" / "pricing-verification.json", pricing_rows)
    save(ROOT / "reports" / "skipped.json", skipped)

    errors = []
    forbidden = ALL_REMOVAL + ["日本", "德国", "进口"]
    for row in cleanup_rows:
        final = row["finalTitle"]
        for term in forbidden:
            if term in final:
                errors.append({"sourceItemId": row["sourceItemId"], "term": term})
        for brand in row["brandsRemoved"]:
            if brand and brand in final:
                errors.append({"sourceItemId": row["sourceItemId"], "brandLeft": brand})
        if title_units(final) > TITLE_LIMIT:
            errors.append({"sourceItemId": row["sourceItemId"], "tooLong": title_units(final)})
    save(ROOT / "reports" / "title-validation.json", errors)

    print("candidates:", len(plan_items), "skipped:", len(skipped))
    print("title errors:", len(errors), errors[:6])
    print("max title units:", max((title_units(x["title"]) for x in plan_items), default=0))
    print("sku total:", sum(x["skuCount"] for x in plan_items))
    print("max sku per item:", max((x["skuCount"] for x in plan_items), default=0))
    print("category counts:", dict(Counter(x["categoryName"] for x in plan_items)))
    print("keyword x category:", dict(Counter((x["keyword"], x["categoryName"])
                                              for x in plan_items)))
    print("excluded rows:", sum(x["excludedRowCount"] for x in plan_items))
    print("excluded kinds:", dict(Counter(e["kind"] for r in source_report
                                          for e in r["excludedRows"])))
    print("alias over-length:", sum(len(r["overLengthAliases"]) for r in alias_report))
    print("alias collisions:", sum(len(r["collisions"]) for r in alias_report))
    print("max alias chars:", max((max(s["saleAliasChars"] for s in
                                       load(ROOT / "candidates" / i["sourceItemId"] /
                                            "source-normalized.json")["skus"])
                                   for i in plan_items), default=0))
    print("--- skipped ---")
    for s in skipped:
        print("  ", s["sourceItemId"], s["status"], s.get("reason", "")[:70])


if __name__ == "__main__":
    main()
