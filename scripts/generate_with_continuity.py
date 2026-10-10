# -*- coding: utf-8 -*-
"""Shared sufy-image2 remake runner driven by a remake plan JSON."""
import argparse, json, shutil, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

SUFY_CLI = r"C:\Users\caotong.888\.joycode\skills\sufy-image2\scripts\sufy_image2.py"
PY = sys.executable

def parse_json(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        import re
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None

def attempt(refs, prompt, workdir, ratio="Adaptive", timeout=600):
    # 2026-09-30: gpt-image-2.5 new chain (NextAICore chat adapter) is the
    # sufy-image2 client default; do not pin the retired SF-* model IDs.
    # refs: [own source] + optional neighbor source refs (continuity mode).
    cmd = [PY, "-X", "utf8", SUFY_CLI, "edit", "--prompt", prompt]
    for r in refs:
        cmd += ["--image", str(r)]
    cmd += ["--ratio", ratio, "--quality", "2K", "--output-dir", str(workdir)]
    started = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    res = parse_json(p.stdout)
    outs = (res or {}).get("outputs") or []
    if res and res.get("ok") and outs:
        return {"ok": True, "output": outs[0]["path"], "elapsed": round(time.time() - started, 1)}
    return {"ok": False, "error": ("%s\n%s" % (p.stderr or "", p.stdout or ""))[-800:],
            "elapsed": round(time.time() - started, 1)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--workers", type=int, default=10)  # 2026-09-30 verified: latency flat (p50 38s), 13.2 imgs/min, 502 self-heals
    ap.add_argument("--attempts", type=int, default=2)
    ap.add_argument("--continuity", default=None,
                    help="continuity plan JSON from 58_seam_preflight.py; injects seam contracts + neighbor source refs into the first pass")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    D = Path(a.draft)
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    LOG = Path(a.log)
    lock = threading.Lock()

    # --- pre-generation seam handling (2026-09-30 user requirement: solve
    # continuity BEFORE generation, keep 10 workers, spend no extra quota) ---
    CONT = json.loads(Path(a.continuity).read_text(encoding="utf-8")) if a.continuity else None
    ENH = {}
    if CONT:
        spec = (CONT.get("unifiedStyleSpec") or "").strip()
        imgs = CONT.get("images") or []
        for i, img in enumerate(imgs):
            key = img.get("imageKey")
            if not key:
                continue
            clauses = []
            nb_keys = []
            if img.get("continuousWithPrev") and i > 0:
                prev = imgs[i - 1]
                clauses.append(
                    f"本图顶边必须与上一张（{prev.get('title') or '上一张'}）的底边自然衔接："
                    f"{img.get('topContract') or '背景色调、光线保持一致，无突兀切换'}。")
                if prev.get("imageKey"):
                    nb_keys.append(prev["imageKey"])
            if img.get("continuousWithNext") and i < len(imgs) - 1:
                nxt = imgs[i + 1]
                clauses.append(
                    f"本图底边必须与下一张（{nxt.get('title') or '下一张'}）的顶边自然衔接："
                    f"{img.get('bottomContract') or '背景色调、光线保持一致，无突兀切换'}。")
                if nxt.get("imageKey"):
                    nb_keys.append(nxt["imageKey"])
            if not clauses:
                pass  # standalone section: still carry the unified style anchor
            suffix = (f"【整组详情页衔接要求】整组详情页统一风格：{spec}。"
                      f"本图是详情页从上到下的第{img.get('displayIndex')}张。"
                      + "".join(clauses)
                      + "重制时保持整组统一背景色调、光线、字体与版式，即使本图是独立小节，也要与整组详情页风格同族，"
                      + "上下边缘不得出现与相邻图冲突的分隔线、色带或背景突变。")
            ENH[key] = {"suffix": suffix, "neighborKeys": nb_keys}

    by_key = {i["imageKey"]: i for i in plan.get("images") or []}
    if CONT:
        for img in CONT.get("images") or []:
            by_key.setdefault(img.get("imageKey"), img)

    def neighbor_ref_paths(enh):
        """Neighbor style refs: prefer already-generated neighbor output (rework
        case: match finalized neighbors), fall back to neighbor source (first
        pass: match original page style)."""
        paths = []
        for nk in enh.get("neighborKeys") or []:
            nb = by_key.get(nk)
            if not nb:
                continue
            for rel in (nb.get("output"), nb.get("source")):
                if not rel:
                    continue
                p = D / rel
                if p.is_file():
                    paths.append(p)
                    break
        return paths

    def log(row):
        with lock:
            with LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    items = [(i, "images") for i in plan.get("images") or []] + [(i, "deannotate") for i in plan.get("deannotate") or []]
    def work(pair):
        item, kind = pair
        key = item["imageKey"]
        src = D / item["source"]
        out = D / item["output"]
        if src == out:
            # Pure reuse marker (fast plans): the SKU-MAIN task owns this file.
            log({"imageKey": key, "kind": kind, "ok": True, "skipped": "reuse-marker"})
            return key, True
        if out.is_file() and out.stat().st_size > 10000:
            log({"imageKey": key, "kind": kind, "ok": True, "skipped": "exists"})
            return key, True
        if a.dry_run:
            enh = ENH.get(key, {}) if kind == "images" else {}
            prompt = item["prompt"]
            if kind == "deannotate" or str(item.get("role")) == "spu_main" \
                    or str(item.get("imageKey", "")).startswith(("SPU-MAIN-", "SKU-MAIN-")):
                ratio = "1:1"
            else:
                ratio = "Adaptive"
            if enh.get("suffix"):
                prompt = prompt + "\n\n" + enh["suffix"]
                nb_refs = neighbor_ref_paths(enh)
                if nb_refs:
                    prompt += "\n参考图顺序：第1张是本图原图；其余是相邻详情页参考图，仅用于统一色调、背景和版式，严禁把它们中的商品、文字或图案内容画进本图。"
            print(f"[dry-run] {key} kind={kind} ratio={ratio} refs={neighbor_ref_paths(enh) if enh else []}\n  prompt: {prompt}\n")
            return key, True
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = D / "重制" / "_tmp" / key
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        for n in range(1, a.attempts + 1):
            try:
                enh = ENH.get(key, {}) if kind == "images" else {}
                prompt = item["prompt"]
                refs = [src]
                if enh.get("suffix"):
                    prompt = prompt + "\n\n" + enh["suffix"]
                nb_paths = neighbor_ref_paths(enh)
                if nb_paths:
                    prompt += "\n参考图顺序：第1张是本图原图；其余是相邻详情页参考图，仅用于统一色调、背景和版式，严禁把它们中的商品、文字或图案内容画进本图。"
                    for p in nb_paths:
                        # keep multipart bytes low: shrink neighbor style refs
                        from PIL import Image
                        nb = tmp / ("nb_" + p.stem + ".jpg")
                        im = Image.open(p).convert("RGB")
                        im.thumbnail((1100, 1100))
                        im.save(nb, "JPEG", quality=88)
                        refs.append(nb)
                # 2026-09-30: JD requires spu_main/sku_main images to be 1:1.
                # Generate them natively square (verified 1254x1254 output);
                # only detail pages keep the adaptive vertical ratio. Do NOT
                # rely on post-generation padding - it is a fallback only.
                if kind == "deannotate" or str(item.get("role")) == "spu_main" \
                        or str(item.get("imageKey", "")).startswith(("SPU-MAIN-", "SKU-MAIN-")):
                    ratio = "1:1"
                else:
                    ratio = "Adaptive"
                r = attempt(refs, prompt, tmp, ratio=ratio)
            except Exception as e:
                r = {"ok": False, "error": f"exception: {e}"}
            if r.get("ok") and Path(r["output"]).is_file():
                shutil.copy2(r["output"], out)
                log({"imageKey": key, "kind": kind, "ok": True, "attempt": n, "elapsed": r.get("elapsed"), "path": item["output"]})
                return key, True
            log({"imageKey": key, "kind": kind, "ok": False, "attempt": n, "elapsed": r.get("elapsed"), "error": (r.get("error") or "")[-400:]})
            time.sleep(3)
        return key, False

    todo = [p for p in items if not (D / p[0]["output"]).is_file()]
    print("todo", len(todo))
    ok, fail = [], []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for key, success in ex.map(work, todo):
            (ok if success else fail).append(key)
            print(("OK " if success else "FAIL ") + key, flush=True)
    print(f"done ok={len(ok)} fail={len(fail)}")
    if fail:
        print("failed:", ",".join(fail))
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
