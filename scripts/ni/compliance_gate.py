# -*- coding: utf-8 -*-
"""Final compliance gate: OCR the remediated images and look for residual violations."""
import argparse, hashlib, io, json, re, subprocess, sys, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch as ni_batch  # noqa: E402

ROOT = ni_batch.root_from_argv()
REP = ROOT / "reports"
MAP = REP / "compliant-image-map.json"
QUEUE = REP / "ocr-box-queue-final.tsv"
BOXES = REP / "ocr-boxes-final.ndjson"
PS1 = Path(__file__).resolve().with_name("ocr_queue.ps1")
REPORT = REP / "final-image-compliance.json"

MUST_REMOVE = {
    "PLATFORM": ["淘宝", "天猫", "淘工厂", "1688", "拼多多", "抖音", "小红书", "快手", "唯品会",
                 "阿里巴巴", "京东超市", "京东自营", "京东", "聚划算", "淘金币", "TMALL", "TMall"],
    "GEO": ["原装进口", "日本", "德国", "进口", "德式", "日式", "韩国进口", "意大利", "法国进口", "海外"],
    "PROMO": ["旗舰店", "旗舰", "官方", "专卖店", "专营店", "厂家直销", "工厂直销", "直销",
              "国补", "国家补贴", "正品", "包邮", "秒杀", "限时", "特价", "买一送一",
              "第二件半价", "立减", "领券", "优惠券", "券后", "抢购", "爆款", "热销",
              "店长推荐", "镇店之宝", "好评", "加购", "收藏", "免费送", "包退", "包换",
              "补贴", "国补补贴", "反季", "清仓"],
    "EXTREME": ["全网最低", "最便宜", "最低价", "史上最低", "国家级", "世界级", "特级", "冠军",
                "独家", "百分百", "王牌", "首选", "万能", "神器", "极致", "顶级", "绝对", "唯一",
                "最好", "最强", "最佳", "极品", "特效", "第一品牌", "销量第一", "全网第一", "绝对安全",
                "第一", "第1", "第一名", "全网", "全网热", "领先品牌", "销量领先"],
    "PRICE": ["原价", "划线价", "标价", "销售价", "促销价", "活动价", "到手价", "券后价",
              "价格说明", "售价", "折扣价", "限时价", "特惠价", "被比较价格", "专柜价", "吊牌价", "指导价"],
    "CONTACT": ["电话", "手机号", "微信号", "二维码", "客服热线", "服务热线", "传真", "QQ", "扫一扫",
                "扫码", "热线", "旺旺"],
}
CLAIM_ONLY = ["授权", "专利", "权威", "检测报告", "除醛率", "去除率", "净化率", "杀菌率", "抑菌率",
              "医用", "治疗", "消炎", "抗菌", "国际标准", "国标认证"]
CLAIM_PAT = [r"99(?:\.[0-9])?%", r"100%", r"[0-9]+\s*倍", r"除尽", r"永久"]
PHONE_PAT = [r"1[3-9][0-9]{9}", r"400[- ]?[0-9]{3}[- ]?[0-9]{4}", r"0[0-9]{2,3}[- ][0-9]{7,8}"]


def md5f(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def brand_terms():
    terms = set()
    src = REP / "image-violations-raw.json"
    if src.is_file():
        d = json.load(io.open(src, encoding="utf-8"))
        for v in d.values():
            for t in (v.get("hits") or {}).get("BRAND", []) or []:
                terms.add(t)
    return sorted(terms)


def build_queue(all_finals=False):
    m = json.load(io.open(MAP, encoding="utf-8"))
    rows, seen = [], set()
    for item, info in m["items"].items():
        for g in ("spu_main", "sku_main", "detail"):
            for e in info["groups"].get(g) or []:
                if not e.get("remediated") and not all_finals:
                    continue
                p = Path(e["compliant"])
                if not p.is_file() or str(p) in seen:
                    continue
                seen.add(str(p))
                rows.append((md5f(p), str(p)))
    QUEUE.write_text("\n".join("%s\t%s" % r for r in rows), encoding="utf-8")
    return rows


def run_ocr():
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS1)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5400)
    return (r.stdout or "")[-300:]


def scan_line(text, terms):
    n = re.sub(r"\s+", "", text or "")
    hits = []
    for cat, lst in terms.items():
        for t in lst:
            if t and t.lower() in n.lower():
                hits.append((cat, t))
    return hits


def load_whitelist():
    p = REP / "ocr-whitelist.json"
    if not p.is_file():
        return []
    return (json.load(io.open(p, encoding="utf-8")).get("entries") or [])


def whitelisted(entry, item, name, term, text):
    n = re.sub(r"\s+", "", text or "")
    return (entry.get("item") == item and entry.get("name") == name
            and entry.get("term") == term
            and re.sub(r"\s+", "", entry.get("matched") or "") in n)


def scan():
    brands = brand_terms()
    wl_entries = load_whitelist()
    recs = {}
    with io.open(BOXES, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            recs[d["md5"]] = d
    out = {"schema": "final-image-compliance/1", "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "termSet": {"mustRemoveCategories": sorted(MUST_REMOVE), "brandCount": len(brands)},
           "whitelist": wl_entries,
           "items": {}}
    stats = {"checked": 0, "clean": 0, "residual": 0, "ocrFail": 0, "claimOnly": 0, "whitelisted": 0}
    for line in io.open(QUEUE, encoding="utf-8"):
        if not line.strip():
            continue
        md5, path = line.rstrip("\n").split("\t")
        d = recs.get(md5)
        stats["checked"] += 1
        if not d or not d.get("ok"):
            stats["ocrFail"] += 1
            out["items"][path] = {"ocrOk": False}
            continue
        must, claim = [], []
        item_id = Path(path).parent.name
        fname = Path(path).name
        for ln in d["lines"]:
            t = ln["text"]
            for cat, term in scan_line(t, MUST_REMOVE):
                if any(whitelisted(e, item_id, fname, term, t) for e in wl_entries):
                    stats["whitelisted"] = stats.get("whitelisted", 0) + 1
                    continue
                must.append({"cat": cat, "term": term, "text": t, "box": [ln["x"], ln["y"], ln["w"], ln["h"]]})
            for b in brands:
                if b and b.lower() in re.sub(r"\s+", "", t).lower():
                    if any(whitelisted(e, item_id, fname, b, t) for e in wl_entries):
                        stats["whitelisted"] = stats.get("whitelisted", 0) + 1
                        continue
                    must.append({"cat": "BRAND", "term": b, "text": t, "box": [ln["x"], ln["y"], ln["w"], ln["h"]]})
            nm = re.sub(r"\s+", "", t)
            for t2 in CLAIM_ONLY:
                if t2 in nm:
                    claim.append({"cat": "CLAIM", "term": t2, "text": t})
            for pat in CLAIM_PAT + PHONE_PAT:
                for mm in re.finditer(pat, nm):
                    claim.append({"cat": "CLAIM_PAT", "term": mm.group(0), "text": t})
        rec = {"ocrOk": True, "width": d.get("width"), "height": d.get("height"),
               "text": " ".join(x["text"] for x in d["lines"]),
               "residual": must, "claimOnly": claim}
        out["items"][path] = rec
        if must:
            stats["residual"] += 1
        else:
            stats["clean"] += 1
            if claim:
                stats["claimOnly"] += 1
    out["stats"] = stats
    REPORT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue-only", action="store_true")
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--all-finals", action="store_true")
    a = ap.parse_args()
    if a.scan_only:
        print(json.dumps(scan(), ensure_ascii=False))
        sys.exit(0)
    rows = build_queue(all_finals=a.all_finals)
    print("queue:", len(rows))
    if a.queue_only:
        sys.exit(0)
    print("ocr:", run_ocr())
    print(json.dumps(scan(), ensure_ascii=False))
