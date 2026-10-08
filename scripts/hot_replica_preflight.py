# -*- coding: utf-8 -*-
"""Hot-replica preflight for one source SKU (formal, parameterized).

Chain position (single link): brand gate -> detail-page drop classification
(gemini double pass, intersection only) -> seam continuity plan -> flexible
dynamic-prompt remake plan (ALL spu_main images, per-source SKU spec images,
style representative images).

Formalized from the battle-tested task-local 62_preflight_and_plan.py
(2026-09-28..30 zeya batches). Store-specific fields are removed; the target
category reference is passed via --reference and feeds the hard
hot_replica_mode.brand_gate() check BEFORE any plan is written. Brand-required
categories exit with SKIPPED_BRAND_REQUIRED and no plan (no generation quota).
"""
import argparse, base64, json, pathlib, re, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8")

SUFY = pathlib.Path(r"C:\Users\caotong.888\.joycode\skills\sufy-image2\scripts")
sys.path.insert(0, str(SUFY))
SKILL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
from sufy_image2 import load_saved_api_key  # noqa: E402
import jd_product_mover as mover  # noqa: E402
import hot_replica_mode as hotmode  # noqa: E402

API_ROOT = "https://api.lts4ai.com/v1"
DEFAULT_MODEL = "gemini-3.8-flash-official"

DROP_PROMPT = """这是某个京东商品详情页的源图序列（红色数字 1 到 N 是显示顺序）。我要重制上架，需要先剔除无价值页。
只输出一个紧凑JSON（不要markdown、不要多余文字）：{"drops":[{"i":页码,"reason":"类别"}]}
剔除类别仅限：品牌信息/价格活动/榜单销量/平台专属/公司介绍/源头工厂/客服售后/专利/淘金币 无价值页。
规则：仅当整页主体是上述类别时才剔除；正常的产品展示、卖点、尺寸、场景、材质、使用方法页一律保留。宁可保留也不误删。没有可剔项就输出 {"drops":[]}。"""

PROMPT = """这是某个京东商品详情页的源图序列（红色数字#1到#N是显示顺序）。我会对每张图独立重制，需要在重制前防止相邻图风格断层。只输出一个紧凑JSON对象（不要markdown代码块、不要多余文字）：
{
 "spec": "整组详情页必须统一的背景色调（色系和色值范围）、光线、字体风格、版式语言（40字内中文）",
 "pages": [{"i":1,"title":"内容类型10字内","prev":false,"next":true}]
}
规则：相邻两页共享同一背景、同一场景被切开、或色调版式明显连续才设 prev/next=true；独立小节设为 false。pages 必须按 #1 到 #N 逐页覆盖，不得遗漏。"""


def parse_json_loose(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def ask(image_path: pathlib.Path, prompt: str, model: str, max_side: int = 1600,
        max_tokens: int = 16000) -> str:
    from PIL import Image
    im = Image.open(image_path)
    im.thumbnail((max_side, max_side))
    buf_path = image_path.with_suffix(".tmp-send.png")
    im.convert("RGB").save(buf_path, "PNG")
    b64 = base64.b64encode(buf_path.read_bytes()).decode()
    buf_path.unlink()
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        API_ROOT + "/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {load_saved_api_key()}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        data = json.loads(r.read().decode())
    msg = data["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""


def ask_drop(image_path: pathlib.Path, model: str) -> dict:
    for attempt in range(1, 4):
        raw = ask(image_path, DROP_PROMPT, model)
        data = parse_json_loose(raw)
        if isinstance(data, dict) and isinstance(data.get("drops"), list):
            return data
        print(f"drop attempt {attempt}: incomplete, retrying")
    raise RuntimeError("gemini drop classification incomplete after 3 attempts")


def extract_page_objects(raw: str) -> dict:
    out = {}
    for m in re.finditer(r"\{[^{}]*\"i\"\s*:\s*(\d+)[^{}]*\}", raw or ""):
        try:
            obj = json.loads(m.group(0))
            i = int(obj.get("i") or 0)
            if i > 0 and obj.get("title"):
                out[i] = obj
        except Exception:
            continue
    return out


def extract_spec(raw: str) -> str:
    m = re.search(r"\"(?:spec|unifiedStyleSpec)\"\s*:\s*\"([^\"\\]{10,})", raw or "")
    return m.group(1) if m else ""


def build_sheet(folder: pathlib.Path, items, out_path: pathlib.Path, cell_w=300, cols=10):
    from PIL import Image, ImageDraw
    thumbs = []
    for item in items:
        im = Image.open(folder / item["localPath"]).convert("RGB")
        im.thumbnail((cell_w, 1400))
        thumbs.append(im)
    h = max(t.height for t in thumbs)
    rows = (len(thumbs) + cols - 1) // cols
    cw = max(t.width for t in thumbs) + 8
    ch = h + 36
    sheet = Image.new("RGB", (cols * cw, rows * ch), (240, 240, 240))
    d = ImageDraw.Draw(sheet)
    for n, im in enumerate(thumbs, 1):
        r, c = divmod(n - 1, cols)
        x, y = c * cw + 4, r * ch + 30
        sheet.paste(im, (x, y))
        d.rectangle([x, r * ch, x + 46, r * ch + 26], fill=(200, 0, 0))
        d.text((x + 10, r * ch + 7), f"#{n}", fill=(255, 255, 255))
    sheet.save(out_path)
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sku", required=True, help="source SKU/SPU id; draft folder is <draft-root>/<sku>_*")
    ap.add_argument("--reference", required=True,
                    help="target category reference JSON (feeds the hard brand gate)")
    ap.add_argument("--draft-root", default=str(SKILL / "outputs" / "migrations"))
    ap.add_argument("--work-dir", default=None,
                    help="artifact dir (default <draft>/hot-replica): sheets, continuity plan, remake plan")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--drop-passes", type=int, default=2)
    ap.add_argument("--page-attempts", type=int, default=6)
    a = ap.parse_args()
    SKU = a.sku
    root = pathlib.Path(a.draft_root)
    folder = next(root.glob(SKU + "_*"), None)
    if not folder:
        print(f"FAIL: draft folder not found for {SKU} under {root}")
        return 1
    work = pathlib.Path(a.work_dir) if a.work_dir else folder / "hot-replica"
    work.mkdir(parents=True, exist_ok=True)

    # Hard brand gate BEFORE any plan / generation quota.
    reference = mover.read_json(pathlib.Path(a.reference))
    gate = hotmode.brand_gate(reference, str(reference.get("category", {}).get("name") or ""))
    (work / f"brand-gate-{SKU}.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
    if not gate["ok"]:
        print("BRAND_GATE", gate["decision"], "|", gate["evidence"])
        return 2
    print("BRAND_GATE PROCEED |", gate["evidence"])

    listing = mover.read_json(folder / "listing.json")
    details = sorted([i for i in listing["images"] if i.get("role") == "detail"],
                     key=lambda x: int(x.get("order") or 0))

    # v3 three-layer dynamic prompt (title with SKU-spec tail stripped).
    prompt_title = hotmode.strip_sku_spec(str(listing["product"].get("title") or ""))
    prompt_dynamic = hotmode.build_dynamic_prompt(prompt_title)
    (work / f"prompt-{SKU}.txt").write_text(prompt_dynamic, encoding="utf-8")
    print("DYNAMIC PROMPT:", prompt_dynamic)

    full_sheet = build_sheet(folder, details, work / f"drop-sheet-{SKU}.png")
    drop_passes = [ask_drop(full_sheet, a.model) for _ in range(max(1, a.drop_passes))]
    drop_maps = [{int(d.get("i")): str(d.get("reason") or "无价值页")
                  for d in (dp.get("drops") or []) if d.get("i")} for dp in drop_passes]
    drop_by_index = {i: drop_maps[0][i] for i in drop_maps[0] if all(i in m for m in drop_maps)}
    dropped_details = {details[i - 1]["imageKey"]: drop_by_index[i]
                       for i in sorted(drop_by_index) if 1 <= i <= len(details)}
    kept = [item for n, item in enumerate(details, 1) if n not in drop_by_index]
    print(f"drop pass: {len(dropped_details)} dropped, {len(kept)} kept | {dropped_details}")

    details = kept
    sheet_path = build_sheet(folder, details, work / f"preflight-sheet-{SKU}.png")
    print(f"sheet: {sheet_path.name} ({len(details)} pages)")

    pages_by_i, spec = {}, ""
    for attempt in range(1, a.page_attempts + 1):
        raw = ask(sheet_path, PROMPT, a.model)
        (work / f"preflight-raw-{SKU}.md").write_text(raw, encoding="utf-8")
        data = parse_json_loose(raw)
        spec = spec or extract_spec(raw)
        if isinstance(data, dict) and isinstance(data.get("pages"), list):
            for obj in data["pages"]:
                i = int(obj.get("i") or 0)
                if i > 0:
                    pages_by_i[i] = obj
        pages_by_i.update(extract_page_objects(raw))
        if len(pages_by_i) >= len(details) and spec:
            break
        print(f"attempt {attempt}: merged {len(pages_by_i)}/{len(details)} pages, retrying")
    if len(pages_by_i) < len(details):
        print(f"FAIL: gemini pages incomplete after {a.page_attempts} attempts "
              f"({len(pages_by_i)}/{len(details)}); raw saved")
        return 1
    pages = [pages_by_i[i] for i in range(1, len(details) + 1)]

    kept_idx = list(zip(details, pages))
    cont_images = []
    for new_pos, (item, page) in enumerate(kept_idx, 1):
        cont_images.append({
            "order": new_pos, "displayIndex": new_pos, "imageKey": item["imageKey"],
            "source": item["localPath"], "output": f"重制-固定/详情图/{item['imageKey']}.png",
            "title": str(page.get("title") or "")[:40],
            "continuousWithPrev": bool(page.get("prev")) and new_pos > 1,
            "continuousWithNext": False,
            "topContract": "", "bottomContract": "",
        })
    for i in range(len(cont_images) - 1):
        cont_images[i]["continuousWithNext"] = cont_images[i + 1]["continuousWithPrev"]
    cont = {"sku": SKU, "model": a.model, "sheet": sheet_path.name,
            "unifiedStyleSpec": spec[:800],
            "images": cont_images}
    (work / f"continuity-plan-{SKU}.json").write_text(
        json.dumps(cont, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"continuity: {len(cont_images)} kept pages, drops={dropped_details}")

    # dynamic-prompt remake plan (flexible main count)
    mains_src = [i["localPath"] for i in listing["images"] if i.get("role") == "spu_main"]
    if len(mains_src) < 2:
        print(f"FAIL: spu_main images must be >= 2, got {len(mains_src)}")
        return 1
    images, order = [], 0
    for n, src in enumerate(mains_src, 1):
        order += 1
        images.append({"imageKey": f"SPU-MAIN-{n:02d}", "role": "spu_main", "order": order,
                       "source": src, "output": f"重制-固定/主图/SPU-MAIN-{n:02d}.png",
                       "prompt": prompt_dynamic})
    for new_pos, (item, page) in enumerate(kept_idx, 1):
        order += 1
        images.append({"imageKey": item["imageKey"], "role": "detail", "order": new_pos,
                       "source": item["localPath"],
                       "output": f"重制-固定/详情图/{item['imageKey']}.png",
                       "prompt": prompt_dynamic})
    sku_mains = [i for i in listing["images"] if i.get("role") == "sku_main"]
    seen = {}
    for im in sku_mains:
        seen.setdefault(im["localPath"], im["imageKey"])
    deannotate = []
    for n, (src, key) in enumerate(seen.items(), 1):
        out = f"重制-固定/SKU图/SKU图{n}.png"
        images.append({"imageKey": f"SKU-MAIN-{n:03d}", "source": src, "output": out,
                       "prompt": prompt_dynamic})
        deannotate.append({"imageKey": f"SKU-REP-{n:03d}", "source": out, "output": out,
                           "prompt": prompt_dynamic})
    plan = {"task": f"{SKU} hot-replica (dynamic prompt + continuity)",
            "title": listing["product"]["title"], "mode": "dynamic-prompt+continuity",
            "promptTitle": prompt_title,
            "droppedDetails": dropped_details,
            "skuMainPolicy": "dynamic-prompt-regenerate",
            "images": images, "deannotate": deannotate}
    (work / f"remake-plan-{SKU}.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"plan: mains={len(mains_src)} details={len(kept_idx)} sku={len(seen)} "
          f"reps={len(deannotate)} drops={len(dropped_details)}")
    print(f"NEXT: generate_with_continuity.py --draft {folder} --plan "
          f"{work / ('remake-plan-' + SKU + '.json')} --continuity "
          f"{work / ('continuity-plan-' + SKU + '.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
