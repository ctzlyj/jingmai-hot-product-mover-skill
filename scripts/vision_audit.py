# -*- coding: utf-8 -*-
"""Independent compliance audit of final images using an LTS4AI vision chat model."""
import argparse, base64, io, json, os, sys, time, threading, urllib.request, urllib.error, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import cv2, numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

API = "https://api.lts4ai.com/v1/chat/completions"
from image_quality_gate import PROMPT_VERSION
PRIMARY_MODEL = "gemini-2.5-flash-lite"  # tier-1: full-pass first screen
REVIEW_MODEL = "gemini-2.5-flash"        # tier-2: review only flagged/unclear images

def needs_review(rec):
    """Tier-2 review only for violations flagged or unreliable/unreadable tier-1 results."""
    if not rec.get("ok"): return True
    parsed = rec.get("parsed")
    if not isinstance(parsed, dict): return True
    violations = parsed.get("violations")
    if not isinstance(violations, list) or not isinstance(parsed.get("flags"), list): return True
    if any(parsed.get(k) is not True for k in ("legible", "layoutOk", "valuable")): return True
    if parsed.get("uncertain") or parsed.get("flags"): return True
    return bool(violations)

SYS = ("你是京东商品图片合规审核员。逐字读取图片上的所有可见文字与水印标记，然后只输出 JSON，不要解释。")
from image_quality_gate import SINGLE_PROMPT as USER, DELETE_FLAGS, _flags
_lock = threading.Lock()

def b64img(p, maxside=1400):
    img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None: return None
    h, w = img.shape[:2]
    s = min(1.0, maxside/float(max(h, w)))
    if s < 1.0:
        img = cv2.resize(img, (max(1, int(w*s)), max(1, int(h*s))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return base64.b64encode(buf.tobytes()).decode("ascii") if ok else None

def call(key, model, path, timeout=180):
    b = b64img(path)
    if b is None: return {"ok": False, "error": "read-fail"}
    body = {"model": model, "temperature": 0, "max_tokens": 900,
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": [
                             {"type": "text", "text": USER},
                             {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}}]}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "HTTP %s" % e.code, "detail": (e.read() or b"")[:200].decode("utf-8", "replace")}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    try:
        txt = d["choices"][0]["message"]["content"]
    except Exception:
        return {"ok": False, "error": "bad-response", "detail": json.dumps(d)[:300]}
    if isinstance(txt, list):
        txt = "".join(c.get("text", "") for c in txt if isinstance(c, dict))
    import re
    m = re.search(r"\{.*\}", txt, re.S)
    parsed = None
    if m:
        try: parsed = json.loads(m.group(0))
        except Exception: parsed = None
    return {"ok": True, "raw": txt[:1500], "parsed": parsed}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default=PRIMARY_MODEL)
    ap.add_argument("--review-model", default=REVIEW_MODEL)
    ap.add_argument("--no-review", action="store_true", help="skip tier-2 flash review of flagged/unclear images")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()
    key = ""
    try:
        from oxygen_verify import load_key
        key = load_key()  # DPAPI credential first
    except Exception:
        key = (os.environ.get("LTS4AI_API_KEY") or "").strip()
    if not key:
        print("NO_KEY"); return 2
    paths = [x.strip() for x in io.open(a.list, encoding="utf-8") if x.strip()]
    if a.limit: paths = paths[:a.limit]
    out = Path(a.out)
    done = {}
    if out.is_file():
        for line in io.open(out, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    d = json.loads(line)
                    path = Path(d["path"])
                    if (d.get("ok") and d.get("promptVersion") == PROMPT_VERSION
                            and path.is_file() and d.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest()):
                        done[d["path"]] = d
                except Exception: pass
    todo = [p for p in paths if p not in done]
    print("total", len(paths), "todo", len(todo), flush=True)
    lock = threading.Lock()
    f = io.open(out, "a", encoding="utf-8")
    n = [0]
    def work(p):
        r = call(key, a.model, p, a.timeout)
        digest = hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).is_file() else None
        rec = {"path": p, "model": a.model, "promptVersion": PROMPT_VERSION, "sha256": digest,
               "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "escalated": False, **r}
        if not a.no_review and needs_review(r):
            r2 = call(key, a.review_model, p, a.timeout)
            first = {"ok": r.get("ok"), "error": r.get("error"),
                     "violations": ((r.get("parsed") or {}).get("violations")),
                     "flags": sorted(_flags(r.get("parsed")))}
            rec = {"path": p, "model": a.review_model, "promptVersion": PROMPT_VERSION, "sha256": digest,
                   "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "escalated": True, "firstPass": first, **r2}
            if _flags(r.get("parsed")) & DELETE_FLAGS:
                rec["reviewConflict"] = bool(r2.get("ok") and not (_flags(r2.get("parsed")) & DELETE_FLAGS))
                rec["parsed"] = r.get("parsed")  # never erase patent/platform evidence
        with lock:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n"); f.flush()
            n[0] += 1
            if n[0] % 25 == 0: print("  done", n[0], "/", len(todo), flush=True)
        return rec
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = [ex.submit(work, p) for p in todo]
        for _ in as_completed(futs): pass
    f.close()
    print("finished", n[0], flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
