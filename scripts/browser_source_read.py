from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse


class BrowserReadError(RuntimeError):
    pass


def run_read_script(script, *, toolbox=None, timeout=75):
    toolbox = Path(toolbox) if toolbox else Path.home() / ".agents/skills/browser-task-router/scripts/toolbox.py"
    if not toolbox.is_file():
        raise BrowserReadError("Browser router is not installed; select an available authorized read-only provider")
    session = "mover-read-" + uuid.uuid4().hex[:16]
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("PLAYWRIGHT_") and key != "NODE_OPTIONS"}
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    command = [sys.executable, "-X", "utf8", str(toolbox), "run", "playwright-cli", "--", "-s=" + session]
    with tempfile.TemporaryDirectory(prefix="mover-browser-read-", ignore_cleanup_errors=True) as directory:
        root = Path(directory).resolve()
        config = root / "browser.json"
        config.write_text(json.dumps({"extension": False, "browser": {"browserName": "chromium", "isolated": True,
                          "launchOptions": {"headless": True, "channel": "chrome"}}}), encoding="utf-8")
        code = root / "read.js"
        code.write_text(script, encoding="utf-8")

        def run(*arguments, limit=timeout):
            options = {"cwd": root, "env": environment, "capture_output": True, "text": True,
                       "encoding": "utf-8", "timeout": limit, "shell": False}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run([*command, *arguments], **options)
            if result.returncode or "### Error" in result.stdout:
                raise BrowserReadError("Owned browser command failed; no automatic replay")
            return result.stdout

        result = None
        failure = None
        try:
            run("open", "about:blank", "--config", str(config), "--idle-timeout=60000")
            output = run("--raw", "run-code", "--filename", str(code))
            result = json.loads(output)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise BrowserReadError("Browser read did not produce verified data; inspect login, challenge or source availability without replay")
        except (OSError, subprocess.SubprocessError, ValueError, BrowserReadError) as error:
            failure = error if isinstance(error, BrowserReadError) else BrowserReadError("Browser read interrupted or returned invalid data; no automatic replay")
        finally:
            try:
                run("close", limit=30)
            except (OSError, subprocess.SubprocessError, BrowserReadError):
                failure = BrowserReadError(f"Owned browser cleanup not confirmed: {session}; do not start a replacement blindly")
        if failure:
            raise failure
        return result


def read_jd_video(product_url, video_id):
    parsed = urlparse(product_url)
    if parsed.scheme != "https" or parsed.hostname not in {"item.jd.com", "item.m.jd.com"} or parsed.username or parsed.password:
        raise BrowserReadError("JD video reader requires a public JD product URL")
    if not re.fullmatch(r"/(?:product/)?\d+\.html", parsed.path) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(video_id)):
        raise BrowserReadError("Invalid JD product or video identity")
    request = {"url": f"https://{parsed.hostname}{parsed.path}", "videoId": str(video_id)}
    template = (Path(__file__).parent / "browser_collectors/jd_video.js").read_text(encoding="utf-8")
    script = template.replace("__MOVER_REQUEST__", json.dumps(request, ensure_ascii=True))
    result = run_read_script(script)
    if result.get("videoId") != str(video_id) or not isinstance(result.get("text"), str) or not result["text"]:
        raise BrowserReadError("Browser video response does not match the requested video")
    return result["text"]


def read_jd_pc_video(product_url):
    """Read the PC-only <video> element some JD products carry.

    2026-09-30 evidence (10201583840642): some source videos never appear in
    the mobile page's mainVideoId (gray switch off) and only render as a PC
    <video> element. The mobile-first collector misses them, so collect()
    falls back to this probe before declaring the source video-less.
    """
    parsed = urlparse(product_url)
    if parsed.scheme != "https" or parsed.hostname != "item.jd.com" or parsed.username or parsed.password:
        raise BrowserReadError("JD PC video reader requires a public item.jd.com URL")
    if not re.fullmatch(r"/\d+\.html", parsed.path):
        raise BrowserReadError("Invalid JD product path")
    script = """
async page => {
  try {
    await page.goto(__URL__, {waitUntil: 'domcontentloaded', timeout: 30000});
    await page.waitForTimeout(8000);
    return await page.evaluate(() => {
      const seen = [];
      for (const el of document.querySelectorAll('video')) {
        const src = el.currentSrc || el.src || '';
        if (/^https?:\\/\\//.test(src)) {
          seen.push({downloadUrl: src, durationSeconds: Math.round(el.duration || 0)});
        }
      }
      return {ok: seen.length > 0, videos: seen};
    });
  } catch (e) {
    return {ok: false, error: String(e), videos: []};
  }
}
""".replace("__URL__", json.dumps(f"https://{parsed.hostname}{parsed.path}", ensure_ascii=True))
    result = run_read_script(script, timeout=150)
    videos = result.get("videos") if isinstance(result, dict) else None
    if not videos:
        raise BrowserReadError("PC page did not expose a downloadable product video")
    return videos[0]
