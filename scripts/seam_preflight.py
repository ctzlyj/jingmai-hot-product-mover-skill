# -*- coding: utf-8 -*-
"""Pre-generation detail-page seam analysis (LTS4AI gemini-3.8-flash-official).

Reads the SOURCE detail image sequence of one draft, asks the vision model ONE
question (which adjacent pages are continuous, shared style / edge contracts),
and writes a continuity plan that 23_generate.py injects into the FIRST
remake pass. Costs one vision call, zero generation quota.
"""
import argparse, base64, json, pathlib, re, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8")

SUFY = pathlib.Path(r"C:\Users\caotong.888\.joycode\skills\sufy-image2\scripts")
sys.path.insert(0, str(SUFY))
from sufy_image2 import load_saved_api_key  # noqa: E402

API_ROOT = "https://api.lts4ai.com/v1"
MODEL = "gemini-3.8-flash-official"

PROMPT = """这是某个京东商品详情页的源图序列（从左到右即买家端从上到下显示，红色数字#1到#N是显示顺序，灰色竖条是图间分隔）。我会对每张图独立重制，必须在重制前防止相邻图风格断层。
请分析并只输出一个JSON对象（不要markdown代码块、不要输出JSON以外的任何文字）：
{
 "unifiedStyleSpec": "整组详情页必须统一的背景色调（给色系名称和大致色值范围）、光线、字体风格、版式语言，用可执行的中文描述",
 "pages": [
  {"index": 1,
   "title": "本页内容类型（如产品特写/尺寸图/场景图/卖点海报）",
   "continuousWithPrev": false,
   "continuousWithNext": true,
   "topContract": "若与上一页连续：本页顶边必须呈现的背景/留白/内容边界；若独立：本页顶边作为独立小节的要求",
   "bottomContract": "同上，针对本页底边与下一页"}
 ]
}
判定规则：相邻两页在原图里共享同一背景、同一场景被切开、或色调版式明显连续，才设 continuous=true；独立小节（如白底参数表、单独海报）设为 false，允许风格切换。契约只描述风格与边缘衔接，不要发明新的商品内容。pages 必须按 #1 到 #N 逐页覆盖，不得遗漏。"""


def ask(image_path: pathlib.Path, prompt: str, max_side: int = 1600) -> str:
    from PIL import Image
    im = Image.open(image_path)
    im.thumbnail((max_side, max_side))
    buf_path = image_path.with_suffix(".tmp-send.png")
    im.convert("RGB").save(buf_path, "PNG")
    b64 = base64.b64encode(buf_path.read_bytes()).decode()
    buf_path.unlink()
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "temperature": 0.1,
        "max_tokens": 6000,
    }).encode()
    req = urllib.request.Request(
        API_ROOT + "/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {load_saved_api_key()}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        data = json.loads(r.read().decode())
    msg = data["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""


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


def build_sheet(details, folder: pathlib.Path, out_path: pathlib.Path):
    from PIL import Image, ImageDraw
    thumbs = []
    for item in details:
        im = Image.open(folder / item["source"]).convert("RGB")
        im.thumbnail((360, 1600))
        thumbs.append(im)
    h = max(t.height for t in thumbs)
    w = sum(t.width + 8 for t in thumbs)
    sheet = Image.new("RGB", (w, h), (240, 240, 240))
    d = ImageDraw.Draw(sheet)
    x = 0
    for n, im in enumerate(thumbs, 1):
        sheet.paste(im, (x, 0))
        d.rectangle([x, 0, x + 46, 26], fill=(200, 0, 0))
        d.text((x + 10, 7), f"#{n}", fill=(255, 255, 255))
        x += im.width + 8
    sheet.save(out_path)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    D = pathlib.Path(a.draft)
    plan = json.loads(pathlib.Path(a.plan).read_text(encoding="utf-8"))
    m = re.match(r"remake-plan-(\d+)", pathlib.Path(a.plan).name)
    sku = m.group(1) if m else "unknown"
    details = sorted([i for i in plan.get("images") or [] if i.get("role") == "detail"],
                     key=lambda x: int(x.get("order") or 0))
    if len(details) < 2:
        print("detail images < 2, no seam analysis needed")
        return 0
    sheet = build_sheet(details, D, pathlib.Path(f"preflight-sheet-{sku}.png"))
    print(f"sheet: {sheet} ({len(details)} pages)")
    pages = []
    for n in (1, 2, 3):
        raw = ask(sheet, PROMPT)
        pathlib.Path(f"preflight-raw-{sku}.md").write_text(raw, encoding="utf-8")
        data = parse_json_loose(raw)
        pages = (data or {}).get("pages") or []
        if len(pages) == len(details) and (data or {}).get("unifiedStyleSpec"):
            break
        print(f"attempt {n}: incomplete gemini JSON ({len(pages)}/{len(details)} pages), retrying...")
    if len(pages) != len(details):
        print(f"FAIL: gemini returned {len(pages)} pages, expected {len(details)}; raw saved to preflight-raw-{sku}.md")
        return 1
    images = []
    for pos, (item, page) in enumerate(zip(details, pages), 1):
        images.append({
            "order": int(item.get("order") or 0),
            "displayIndex": pos,
            "imageKey": item["imageKey"],
            "source": item["source"],
            "output": item["output"],
            "title": str(page.get("title") or "")[:80],
            "continuousWithPrev": bool(page.get("continuousWithPrev")) if pos > 1 else False,
            "continuousWithNext": bool(page.get("continuousWithNext")) if pos < len(details) else False,
            "topContract": str(page.get("topContract") or "")[:400],
            "bottomContract": str(page.get("bottomContract") or "")[:400],
        })
    out = {
        "sku": sku,
        "model": MODEL,
        "sheet": sheet.name,
        "unifiedStyleSpec": str((data or {}).get("unifiedStyleSpec") or "")[:800],
        "images": images,
    }
    out_path = pathlib.Path(a.out) if a.out else pathlib.Path(f"continuity-plan-{sku}.json")
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    seams = [(p["displayIndex"], p["displayIndex"] + 1) for p in images if p["continuousWithNext"]]
    print(f"saved: {out_path}")
    print(f"continuous seams: {seams or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
