# -*- coding: utf-8 -*-
"""Round-based vision-audit flag mapper (audit file + output configurable)."""
import argparse, collections, hashlib, io, json, re, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch as ni_batch  # noqa: E402

R = ni_batch.root_from_argv()
REP = R / "reports"

# 只保留“必须清除”的类别（与用户明确清单一致：品牌/产地进口/平台/联系/价格/促销/榜单/极限）
MUST_KEY = ["旗舰","官方","专卖","专营","厂家","工厂","直供","源头","直发","授权",
            "日本","德国","GERMANY","GERMAN","进口","进囗","海外","韩国","美国","法国","意大利","欧洲","法式",
            "淘宝","天猫","TMALL","拼多多","抖音","小红书","快手","唯品会","1688","京东",
            "电话","手机","二维码","微信","邮箱","旺旺","热线","扫一扫","扫码","QQ",
            "原价","划线","标价","售价","券后","到手价","指导价","专柜","吊牌","价格说明","折扣","低价",
            "秒杀","立减","限时","优惠券","包邮","国补","补贴","赠品","赠送","买一送一","买就送","买1发",
            "榜单","排行","TOP","热卖","热销","销量","爆款","好评","回购",
            "第一","第1","顶级","绝对","唯一","除尽","除净","100%","全网","极品","王牌","神器","特效","极致",
            "最强","最好","最低","最便","领先","冠军","独家","首选"]
# 百分比/数值功效宣称（用户要求清极限词与数值宣称）
NUM_PAT = re.compile(r"(?:\d+(?:\.\d+)?\s*(?:%|％|倍|㎡|平方米)|99\.?\d*%?)")


def md5f(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def norm(s):
    return re.sub(r"\s+", "", s or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", default=str(REP / "vision-audit-final2.jsonl"))
    ap.add_argument("--ocr", default=str(REP / "ocr-boxes-final.ndjson"))
    ap.add_argument("--out", default=str(REP / "vision-flagged2.json"))
    ap.add_argument("--nums", action="store_true", help="同时把百分比/数值宣称计入必修")
    a = ap.parse_args()
    recs = {}
    for line in io.open(a.ocr, encoding="utf-8") if Path(a.ocr).is_file() else []:
        line = line.strip()
        if line:
            d = json.loads(line)
            recs[d["md5"]] = d
    flag = collections.OrderedDict()
    nocov = []
    for line in io.open(a.audit, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if not d.get("ok"):
            continue
        vio = (d.get("parsed") or {}).get("violations") or []
        ts = []
        for v in vio:
            if isinstance(v, dict):
                t = str(v.get("term") or ""); txt = str(v.get("text") or "")
            else:
                t, txt = str(v), ""
            if t and any(k in t or k in txt for k in MUST_KEY):
                ts.append(t or txt)
            elif a.nums and NUM_PAT.search(txt or t):
                ts.append(t or txt)
        if ts:
            flag[d["path"]] = ts
    print("audited", sum(1 for _ in io.open(a.audit, encoding="utf-8")), "flagged", len(flag))
    for p, ts in flag.items():
        dd = recs.get(md5f(Path(p)))
        lines = (dd or {}).get("lines") or []
        if not any(any(k in norm(ln["text"]) for k in MUST_KEY) for ln in lines):
            nocov.append((p, ts, len(lines)))
    print("flagged with NO windows-OCR line match:", len(nocov))
    for p, ts, n in nocov:
        print("   ", Path(p).parent.name + "/" + Path(p).name, "ocrlines=", n,
              json.dumps(ts, ensure_ascii=False)[:110])
    json.dump({"schema": "vision-flagged/1", "audit": a.audit, "nums": a.nums,
               "images": {k: v for k, v in flag.items()}},
              io.open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("saved", a.out)


if __name__ == "__main__":
    main()
