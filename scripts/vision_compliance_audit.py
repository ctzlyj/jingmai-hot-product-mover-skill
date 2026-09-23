# -*- coding: utf-8 -*-
"""Two-tier vision compliance audit for listing images via LTS4AI chat models.

Tier 1 (primary) audits every image with the cheap fast model (default
gemini-2.5-flash-lite). Tier 2 re-audits only flagged or unreadable images with
the stronger model (default gemini-2.5-flash). API key is read from the
process-local LTS4AI_API_KEY environment variable and is never persisted.
"""
import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_PRIMARY_MODEL = "gemini-2.5-flash-lite"
DEFAULT_REVIEW_MODEL = "gemini-2.5-flash"
API = "https://api.lts4ai.com/v1/chat/completions"

SYS_PROMPT = ("你是京东商品图片合规审核员。逐字读取图片上的所有可见文字与水印标记，然后只输出 JSON，不要解释。")
USER_PROMPT = ("检查这张商品图。找出下列违规内容：品牌名/品牌Logo/商标、旗舰店/官方/专卖店/授权、电话手机号微信号二维码邮箱、"
               "厂家直销/工厂直销/直供、德国/日本/进口/海外等产地进口词、淘宝/天猫/TMALL/拼多多/抖音/小红书等第三方平台信息、"
               "其他店铺水印、极限词（最、第一、第一名、顶级、绝对、唯一、100%、除尽等）、"
               "价格说明（原价/划线价/券后价/售价/标价/指导价/元/¥/折）、促销（秒杀/立减/优惠券/限时/包邮/国补/补贴/赠品）、"
               "榜单排名（榜单/TOP/热卖/热销/销量/排行榜）。\n"
               "输出 JSON：{\"violations\":[{\"term\":\"命中的违规词\",\"text\":\"该词所在的完整文字片段\"}],\"other_text\":\"图中其它主要文字\"}\n"
               "没有任何违规时 violations 为空数组。")


def encode_image(path, max_side=1400, jpeg_quality=82):
    """Return base64 JPEG data URL body, or None when the image cannot be read."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            scale = min(1.0, max_side / float(max(im.size)))
            if scale < 1.0:
                im = im.resize((max(1, int(im.size[0] * scale)), max(1, int(im.size[1] * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=jpeg_quality)
    except Exception:
        return None
    return base64.b64encode(buf.getvalue()).decode("ascii")


def is_review_needed(record):
    """Primary records go to tier-2 review when flagged or not reliably readable."""
    if not record.get("ok"):
        return True
    parsed = record.get("parsed")
    if not isinstance(parsed, dict):
        return True
    violations = parsed.get("violations")
    if violations is None:
        return True
    return bool(violations)


def _extract_json(text):
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else None
    except ValueError:
        return None


def call_model(key, model, path, timeout):
    image_b64 = encode_image(path)
    if image_b64 is None:
        return {"ok": False, "error": "read-fail"}
    body = {"model": model, "temperature": 0, "max_tokens": 900,
            "messages": [{"role": "system", "content": SYS_PROMPT},
                         {"role": "user", "content": [
                             {"type": "text", "text": USER_PROMPT},
                             {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image_b64}}]}]}
    request = urllib.request.Request(API, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "HTTP %s" % exc.code,
                "detail": (exc.read() or b"")[:200].decode("utf-8", "replace"), "latency": time.time() - started}
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "latency": time.time() - started}
    latency = time.time() - started
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return {"ok": False, "error": "bad-response", "detail": json.dumps(data)[:300], "latency": latency}
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    usage = data.get("usage") or {}
    return {"ok": True, "latency": latency,
            "parsed": _extract_json(content), "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens")}}


def load_done(out_path):
    done = {}
    if out_path.is_file():
        with io.open(out_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("ok"):
                        done[record["path"]] = record
                except (ValueError, KeyError, TypeError):
                    continue
    return done


def run_pass(key, model, paths, out_path, concurrency, timeout):
    done = load_done(out_path)
    todo = [p for p in paths if p not in done]
    print("model=%s total=%d todo=%d" % (model, len(paths), len(todo)), flush=True)
    if not todo:
        return done
    lock_write_out = io.open(out_path, "a", encoding="utf-8")
    completed = [0]

    def work(path):
        record = {"path": path, "model": model, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  **call_model(key, model, path, timeout)}
        return record

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(work, path) for path in todo]
        for future in as_completed(futures):
            record = future.result()
            lock_write_out.write(json.dumps(record, ensure_ascii=False) + "\n")
            lock_write_out.flush()
            completed[0] += 1
            if completed[0] % 25 == 0:
                print("  done %d/%d" % (completed[0], len(todo)), flush=True)
    lock_write_out.close()
    return load_done(out_path)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", required=True, help="UTF-8 text file with one image path per line")
    parser.add_argument("--out", required=True, help="primary tier JSONL output")
    parser.add_argument("--review-out", required=True, help="tier-2 review JSONL output")
    parser.add_argument("--summary", required=True, help="final summary JSON path")
    parser.add_argument("--primary-model", default=DEFAULT_PRIMARY_MODEL)
    parser.add_argument("--review-model", default=DEFAULT_REVIEW_MODEL)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--review-concurrency", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=180)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    key = (os.environ.get("LTS4AI_API_KEY") or "").strip()
    if not key:
        print("NO_KEY")
        return 2
    with io.open(args.list, encoding="utf-8") as handle:
        paths = [line.strip() for line in handle if line.strip()]
    if args.limit:
        paths = paths[:args.limit]
    primary = run_pass(key, args.primary_model, paths, Path(args.out), args.concurrency, args.timeout)

    review_paths = [p for p in paths if is_review_needed(primary.get(p, {}))]
    print("review-needed:", len(review_paths), flush=True)
    review = run_pass(key, args.review_model, review_paths, Path(args.review_out), args.review_concurrency, args.timeout)

    summary = {"model_primary": args.primary_model, "model_review": args.review_model,
               "total": len(paths), "review_needed": len(review_paths), "results": []}
    for path in paths:
        primary_record = primary.get(path, {})
        item = {"path": path, "primary": {"ok": primary_record.get("ok"),
                                          "violations": (primary_record.get("parsed") or {}).get("violations"),
                                          "error": primary_record.get("error")}}
        if is_review_needed(primary_record):
            review_record = review.get(path, {})
            if review_record.get("ok") and isinstance(review_record.get("parsed"), dict):
                item["status"] = "VIOLATION" if review_record["parsed"].get("violations") else "CLEAN"
                item["review"] = {"violations": review_record["parsed"].get("violations")}
            else:
                item["status"] = "UNVERIFIED"
                item["review"] = {"ok": review_record.get("ok"), "error": review_record.get("error")}
        else:
            item["status"] = "CLEAN"
        summary["results"].append(item)
    summary["counts"] = {status: sum(1 for item in summary["results"] if item["status"] == status)
                         for status in ("CLEAN", "VIOLATION", "UNVERIFIED")}
    Path(args.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary["counts"], ensure_ascii=False), flush=True)
    return 0 if summary["counts"].get("UNVERIFIED", 0) == 0 else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
