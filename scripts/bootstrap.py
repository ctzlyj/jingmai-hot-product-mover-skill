#!/usr/bin/env python3
"""首次使用一键自检/初始化（给同事从 SkillHub 安装后用）。

    python scripts/bootstrap.py --json            # 自检 + 自动补齐能补的
    python scripts/bootstrap.py --json --check    # 只检查，不安装不写文件
    python scripts/bootstrap.py --json --profile pop-xxx   # 用已登录 Profile 生成 store.local.json

Auto-fixes: Skill-local .venv + requirements, jdo-cli runtime (no PyInstaller
needed when the binary exists), Python 3.12 discovery, UTF-8 console. Every
remaining item comes back in ``nextActions`` with ``owner`` = ``agent`` (the
calling assistant fixes it) or ``user`` (only store identity / store login).
Secrets are never printed: keys are reported as present/absent only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import tempfile
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import win_no_window  # noqa: F401,E402

REQUIRED_MODULES = ("requests", "openpyxl", "PIL", "cv2", "numpy")
SKILL_ROOTS = (".codex/skills", ".agents/skills", ".joycode/skills", ".claude/skills")
SIBLING_JDO_SKILLS = ("jd-jingmai-batch-image-refresh",)
PLACEHOLDER_SHOPS = {"", "YOUR_SHOP_NAME", "示例店铺"}
# "" = whatever pip is configured with. pypi.org downloads opencv at ~50 KB/s on
# the office network (2026-11-04: >15 min stalled); domestic mirrors ~1.5 MB/s.
PIP_INDEXES = ("", "https://pypi.tuna.tsinghua.edu.cn/simple", "https://mirrors.cloud.tencent.com/pypi/simple")
PIP_TIMEOUT = 420


# ------------------------------------------------------------------ python
def venv_python(skill: Path = SKILL) -> Path:
    return skill / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def missing_modules(python: Path) -> list[str]:
    code = "import importlib.util as u,json,sys;print(json.dumps([m for m in sys.argv[1:] if not u.find_spec(m)]))"
    try:
        result = subprocess.run([str(python), "-c", code, *REQUIRED_MODULES], capture_output=True, text=True,
                                timeout=120)
        return json.loads(result.stdout.strip() or "[]")
    except Exception:  # noqa: BLE001
        return list(REQUIRED_MODULES)


def base_python() -> str:
    """Prefer 3.13 for the main flow; any 3.10+ works."""
    for version in ("3.13", "3.12", "3.11"):
        try:
            result = subprocess.run(["py", f"-{version}", "-c", "import sys;print(sys.executable)"],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:  # noqa: BLE001
            break
    return sys.executable


def pip_install(python: Path, packages: list[str], runner=subprocess.run) -> tuple[bool, str, str]:
    """pip install with a bounded wait, then retry on mirrors unless the user pinned an index."""
    indexes = PIP_INDEXES[:1] if os.environ.get("PIP_INDEX_URL") else PIP_INDEXES
    detail = ""
    for index in indexes:
        command = [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", *packages]
        if index:
            command += ["-i", index]
        try:
            result = runner(command, capture_output=True, text=True, timeout=PIP_TIMEOUT)
        except subprocess.TimeoutExpired:
            detail = f"timeout after {PIP_TIMEOUT}s via {index or 'default index'}"
            continue
        if result.returncode == 0:
            return True, index, ""
        detail = (result.stderr or "")[-400:]
    return False, "", detail


def ensure_venv(check_only: bool) -> dict[str, Any]:
    python = venv_python()
    created = False
    if not python.exists() and not check_only:
        subprocess.run([base_python(), "-m", "venv", str(SKILL / ".venv")], capture_output=True, timeout=600)
        created = python.exists()
    if not python.exists():
        return {"python": "", "missing": list(REQUIRED_MODULES), "created": False}
    missing = missing_modules(python)
    installed, index, detail = False, "", ""
    if missing and not check_only:
        installed, index, detail = pip_install(python, ["-r", str(SKILL / "requirements.txt")])
        missing = missing_modules(python)
    return {"python": str(python), "created": created, "installed": installed, "index": index,
            "missing": missing, "detail": detail}


# ------------------------------------------------------------------ jdo-cli
def sibling_jdo_cli(home: Path | None = None, skill_root: Path = SKILL) -> Path | None:
    home = Path(home or Path.home())
    from skill_paths import installed_skill_dir

    for name in SIBLING_JDO_SKILLS:
        root = installed_skill_dir(name, skill_root=skill_root, home=home)
        if root:
            candidate = root / "vendor/jdo-cli/bin/jdo-cli.exe"
            if candidate.is_file():
                return candidate
    return None


def find_jdo_cli() -> tuple[Path | None, str]:
    explicit = os.environ.get("JDO_CLI_PATH", "").strip()
    if explicit and Path(explicit).is_file():
        return Path(explicit), "JDO_CLI_PATH"
    official = Path.home() / ".jdo-cli" / "bin" / "jdo-cli.exe"
    if official.is_file():
        return official, "official"
    sibling = sibling_jdo_cli()
    if sibling:
        return sibling, "sibling-skill"
    return None, ""


def install_jdo_cli_from(source: Path) -> Path:
    """Copy a known-good jdo-cli binary to the official location and init it."""
    target = Path.home() / ".jdo-cli" / "bin" / "jdo-cli.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    subprocess.run([str(target), "init", "--skip-network"], capture_output=True, timeout=180)
    return target


def find_python312() -> Path | None:
    value = os.environ.get("JDO_PYTHON312", "").strip()
    if value and Path(value).is_file():
        return Path(value)
    candidates = [Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe"]
    try:
        result = subprocess.run(["py", "-3.12", "-c", "import sys;print(sys.executable)"], capture_output=True,
                                text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            candidates.insert(0, Path(result.stdout.strip()))
    except Exception:  # noqa: BLE001
        pass
    return next((path for path in candidates if path.is_file()), None)



def set_user_env(name: str, value: str) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def persist_python312(python312: Path, writer=set_user_env) -> bool:
    """jdo_listing_mvp only probes the default install path, so remember others."""
    default = Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe"
    if Path(python312) == default or user_env("JDO_PYTHON312") == str(python312):
        return False
    writer("JDO_PYTHON312", str(python312))
    return True


def prepare_runtime(exe: Path, python312: Path | None, check_only: bool) -> dict[str, Any]:
    """The large-body bridge needs jdo-cli's embedded code; extract once with any PyInstaller."""
    runtime = SKILL / "outputs" / ".runtime" / "jdo-cli.pyc"
    if runtime.exists():
        return {"runtime": str(runtime), "source": "existing"}
    if check_only or not python312:
        return {"runtime": "", "source": ""}
    code = (
        "import sys;from pathlib import Path;"
        "from PyInstaller.archive.readers import CArchiveReader as R;"
        "d=R(sys.argv[1]).extract('jdo-cli');p=Path(sys.argv[2]);p.parent.mkdir(parents=True,exist_ok=True);"
        "p.write_bytes(d)"
    )
    for python in (venv_python(), python312):
        if not python or not Path(python).exists():
            continue
        result = subprocess.run([str(python), "-c", code, str(exe), str(runtime)], capture_output=True, timeout=300)
        if result.returncode == 0 and runtime.exists():
            return {"runtime": str(runtime), "source": f"extracted-by-{Path(python).parent.parent.name}"}
    if pip_install(python312, ["pyinstaller>=6,<7", "certifi"])[0]:
        result = subprocess.run([str(python312), "-c", code, str(exe), str(runtime)], capture_output=True,
                                timeout=300)
        if result.returncode == 0 and runtime.exists():
            return {"runtime": str(runtime), "source": "extracted-after-pip"}
    return {"runtime": "", "source": "failed"}


def python312_has_certifi(python312: Path | None) -> bool:
    if not python312:
        return False
    result = subprocess.run([str(python312), "-c", "import certifi"], capture_output=True, timeout=60)
    return result.returncode == 0


def jdo_profiles(folder: Path | None = None) -> list[dict[str, Any]]:
    folder = Path(folder or Path.home() / ".jdo-cli" / "profiles")
    rows = []
    if not folder.is_dir():
        return rows
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        profile = str(data.get("profile") or path.stem).strip().lower()
        if not profile or profile == "default":
            continue
        rows.append({"profile": profile, "shopName": str(data.get("shop_name") or ""),
                     "venderId": data.get("vender_id")})
    return rows


# ------------------------------------------------------------------ store
def store_config_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"state": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return {"state": "invalid", "path": str(path), "error": type(exc).__name__}
    stores = {key: value for key, value in (data.get("stores") or {}).items() if isinstance(value, dict)}
    if stores:
        good = sorted(key for key, value in stores.items()
                      if str(value.get("profile") or key).strip().lower() not in {"", "default"}
                      and int(value.get("venderId") or 0) > 0
                      and str(value.get("shopName") or "").strip() not in PLACEHOLDER_SHOPS)
        if good:
            return {"state": "ok", "path": str(path), "profiles": good, "multiStore": True,
                    "freightTemplate": bool(int((data.get("business") or {}).get("freightTemplateId") or 0))}
    store = data.get("store") or {}
    profile = str(store.get("profile") or "").strip().lower()
    shop = str(store.get("shopName") or "").strip()
    vender = int(store.get("venderId") or 0)
    if profile in {"", "your-store-profile"} or shop in PLACEHOLDER_SHOPS or vender <= 0:
        return {"state": "placeholder", "path": str(path)}
    if profile == "default":
        return {"state": "invalid", "path": str(path), "error": "default profile is forbidden"}
    business = data.get("business") or {}
    return {"state": "ok", "path": str(path), "profile": profile, "shopName": shop, "venderId": vender,
            "freightTemplate": bool(int(business.get("freightTemplateId") or 0))}


def write_store_config(path: Path, identity: dict[str, Any]) -> bool:
    """Create store.local.json from a live-verified identity; never overwrite."""
    if path.exists() and store_config_status(path)["state"] == "ok":
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "store": {"profile": identity["profile"], "venderId": int(identity["venderId"]),
                  "shopName": identity["shopName"]},
        "business": {"freightTemplateId": 0, "freightTemplateLabel": "待查询：店铺运费模板"},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def verify_profile(profile: str) -> dict[str, Any]:
    import jdo_listing_mvp as cli

    active = cli.configure_jdo_profile(profile)
    data = cli.get("jdo-seller-info", "/rest/sp-seller/v0/venderinfo", {"scopeSet": "baseInfo"}).get("data") or {}
    shop = data.get("shopBaseInfo") or {}
    return {"profile": active, "venderId": int(data.get("venderId") or 0),
            "shopName": str(shop.get("shopName") or "").strip()}


# ------------------------------------------------------------------ others
def user_env(name: str) -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0] or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def user_env_present(name: str) -> bool:
    """Query registry value byte count only; never fetch/decrypt a model Key."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        import winreg
        from ctypes import wintypes
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            size = wintypes.DWORD()
            query = ctypes.windll.advapi32.RegQueryValueExW
            query.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
            rc = query(wintypes.HANDLE(int(key)), name, None, None, None, ctypes.byref(size))
            return rc == 0 and size.value > 2
    except (OSError, AttributeError):
        return False


def describe_key() -> dict[str, Any]:
    for name in ("JD_LLM_API_KEY", "GPT_IMAGE2_API_KEY"):
        if bool(os.environ.get(name)):
            return {"present": True, "source": f"process env {name}"}
        if user_env_present(name):
            return {"present": True, "source": f"user env {name} (metadata only)"}
    return {"present": False, "source": ""}


def sufy_status() -> dict[str, Any]:
    import skill_paths
    folder = skill_paths.sufy_scripts_dir()
    found = (folder / "sufy_image2.py").is_file()
    local = os.environ.get("LOCALAPPDATA")
    credential = Path(local) / "JoyCode/credentials/sufy-image2.dpapi" if local else None
    return {"found": found, "path": str(folder) if found else "",
            "credential": bool(credential and credential.is_file()) or bool(os.environ.get("LTS4AI_API_KEY"))}


def hioffice_status() -> dict[str, Any]:
    """Process presence, not authentication; no personal port or browser probing."""
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if os.name != "nt" or not shell:
        return {"listening": False, "processPresent": False, "authenticationVerified": False}
    try:
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command",
                                 "[bool](Get-Process -Name HiOffice -ErrorAction SilentlyContinue)"],
                                capture_output=True, text=True, timeout=15)
        present = result.returncode == 0 and result.stdout.strip().lower() == "true"
        return {"listening": present, "processPresent": present, "authenticationVerified": False}
    except (OSError, subprocess.SubprocessError):
        return {"listening": False, "processPresent": False, "authenticationVerified": False}


def directory_writable(directory: Path, check_only: bool = False) -> bool:
    """Check the closest existing ancestor; temporary write probe is immediately removed."""
    directory = Path(directory)
    while not directory.exists() and directory != directory.parent:
        directory = directory.parent
    if not directory.is_dir():
        return False
    if check_only:
        return os.access(directory, os.W_OK)
    try:
        with tempfile.TemporaryFile(prefix="mover-permission-", dir=directory):
            pass
        return True
    except OSError:
        return False


def permission_status(check_only: bool) -> dict[str, Any]:
    items = {str(p): directory_writable(p, check_only) for p in
             (SKILL, SKILL / "config", SKILL / "outputs", Path.home() / ".jdo-cli")}
    return {"writable": all(items.values()), "directories": items,
            "method": "access-check" if check_only else "temporary-write-probe"}


def browser_status() -> dict[str, Any]:
    """Find installed browser/tool files only. Never attach CDP or open a user tab."""
    from skill_paths import installed_skill_dir
    browsers = []
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            browsers += [Path(base) / "Google/Chrome/Application/chrome.exe",
                         Path(base) / "Microsoft/Edge/Application/msedge.exe"]
    found = [str(p) for p in browsers if p.is_file()]
    router = installed_skill_dir("browser-task-router")
    node = shutil.which("node")
    playwright = bool(shutil.which("playwright-cli"))
    if router and (router / "scripts/toolbox.py").is_file():
        try:
            status = subprocess.run([sys.executable, "-X", "utf8", str(router / "scripts/toolbox.py"),
                                     "status", "playwright-cli"], capture_output=True, text=True,
                                    encoding="utf-8", timeout=20)
            info = json.loads(status.stdout).get("playwright-cli") or {}
            playwright = info.get("installation") == "installed"
        except (OSError, ValueError, subprocess.SubprocessError):
            playwright = False
    return {"found": bool(found), "executables": found, "node": bool(node),
            "router": bool(router and (router / "scripts/toolbox.py").is_file()),
            "playwrightCli": playwright,
            "cdpAttached": False, "loginVerified": False,
            "note": "只检查安装文件；任务专属 Playwright/CDP 会话仍需按来源验证，禁止连接工作浏览器"}


def readiness(report: dict[str, Any]) -> dict[str, bool]:
    jdo = report.get("jdoCli") or {}
    environment = (not report["deps"].get("missing")
                   and report.get("permissions", {}).get("writable", False)
                   and all(jdo.get(k) for k in ("binary", "python312", "runtime")))
    identity = report.get("verifiedIdentity") or {}
    business = (environment and report.get("store", {}).get("state") == "ok"
                and bool(identity.get("profile")) and identity.get("profile") != "default"
                and not report.get("errors"))
    return {"environmentReady": bool(environment), "businessReady": bool(business)}


def capability_status(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    local = not report["deps"].get("missing") and report.get("permissions", {}).get("writable", False)
    states = readiness(report)
    browser = report.get("browser") or {}
    company = (report.get("apiKey") or {}).get("present", False)
    external = report.get("sufyImage2") or {}
    return {
        "localDraft": {"available": bool(local), "requires": "本地输入、Python依赖和可写目录；无需生图 Key"},
        "prePublishValidation": {"available": states["businessReady"],
                                 "requires": "京麦 CLI 店铺连接、三要素核验和当前类目 reference；无需生图 Key"},
        "companyGeneration": {"available": bool(local and company), "requires": "公司模型 Key（只查存在，不验证余额/模型可用性）"},
        "externalGeneration": {"available": bool(local and external.get("found") and external.get("credential")),
                               "requires": "sufy-image2 与使用者自己的 Key；DPAPI 文件存在不等于有效"},
        "browserCollection": {"available": bool(browser.get("found") and browser.get("node") and browser.get("router") and browser.get("playwrightCli")),
                              "requires": "浏览器路由器+Playwright、来源登录；未推广的 jd-taobao-hot-sales 不是自动安装前置"},
    }


def action(owner: str, kind: str, text: str) -> dict[str, str]:
    return {"owner": owner, "kind": kind, "action": text}


def next_actions(report: dict[str, Any]) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if report.get("permissions") and not report["permissions"].get("writable"):
        actions.append(action("agent", "directory-permission", "请让助手将 Skill 安装到当前用户可写的 Skills 目录；不要写系统目录或覆盖已有文件"))
    browser = report.get("browser")
    if browser is not None and not (browser.get("found") and browser.get("node") and browser.get("router")):
        actions.append(action("agent", "browser-optional", "只有在线浏览器采集需要：安装 Chrome/Edge、Node 和浏览器任务路由器的 Playwright；本地素材整理与京麦校验不受影响，勿连接工作浏览器"))
    if report["deps"].get("missing"):
        actions.append(action("agent", "python-deps", "用 Skill 目录下 .venv 安装 requirements.txt；失败时看 deps.detail"))
    jdo = report["jdoCli"]
    if not jdo.get("binary"):
        actions.append(action("agent", "jdo-cli", (
            "安装官方京麦 CLI 到 %USERPROFILE%\\.jdo-cli（解压官方包后执行 bin\\jdo-cli.exe init --skip-network）；"
            "或先装 SkillHub 的 jd-jingmai-batch-image-refresh（内含同版本 jdo-cli），再重跑 bootstrap 自动复制")))
    if not jdo.get("python312"):
        actions.append(action("agent", "python312", "winget install Python.Python.3.12，或设置 JDO_PYTHON312 指向 3.12 python.exe"))

    if jdo.get("binary") and not jdo.get("runtime"):
        actions.append(action("agent", "jdo-runtime", "重跑 bootstrap（不带 --check）提取 jdo-cli 运行时；仍失败则对 3.12 安装 pyinstaller"))
    profiles = report["profiles"]
    store = report["store"]
    if not profiles:
        actions.append(action("user", "store-identity", "告诉助手要操作的店铺名称（或店铺浏览器 .lnk）；助手据此建专属 Profile"))
        actions.append(action("user", "store-login", (
            "先对助手说“京麦 CLI 店铺连接”，在目标店铺专属浏览器里授权；"
            "不要让本 Skill 复制授权代码、Cookie 或 Token，不使用 default Profile")))
    if store.get("state") != "ok":
        actions.append(action("agent", "store-config", (
            "授权成功后运行 bootstrap.py --profile <Profile> 回查 venderinfo 并生成 config/store.local.json；"
            "运费模板 ID 从店铺现有商品 logisticsInfo.transportId 或店铺后台读取后填写")))
    elif not store.get("freightTemplate"):
        actions.append(action("agent", "freight-template", "store.local.json 的 business.freightTemplateId 还是 0；需整店搬运时填写真实运费模板"))
    if not report["apiKey"].get("present"):
        actions.append(action("agent", "image-key", (
            "需要生图时：按公司模型门户配置自己的 Key，存在性自检不读取值；"
            "不得写入仓库、日志或聊天")))
    if not report["sufyImage2"].get("found"):
        actions.append(action("agent", "sufy-image2", (
            "仅爆款复刻外部生图需要：安装 sufy-image2 Skill 到同一 Skills 目录，或设置 SUFY_IMAGE2_SKILL；"
            "Key 由使用者本人配置（DPAPI 或 LTS4AI_API_KEY）")))
    if not report["hiOffice"].get("listening"):
        actions.append(action("agent", "hioffice", (
            "需要内网鉴权的操作请保持京ME、HiOffice 登录运行；公开 SkillHub 安装下载不依赖固定端口。"
            "启动 HiOffice.exe 后重试")))
    return actions


def refresh_category(profile: str, category_id: int, category_name: str, brand_category_id: int | None) -> str:
    import jdo_listing_mvp as cli

    cli.configure_jdo_profile(profile)
    output = SKILL / "config" / f"category-{category_id}.reference.json"
    cli.refresh_reference(output, category_id, category_name, brand_category_id)
    return str(output)


def safe_step(step: str, function, *args) -> dict[str, Any]:
    """Platform/login failures are reported as data so the agent can act on them."""
    try:
        return {"ok": True, "step": step, "value": function(*args)}
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        return {"ok": False, "step": step, "error": str(exc)[:300]}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="输出 JSON（默认也是 JSON）")
    parser.add_argument("--check", action="store_true", help="只检查，不安装、不写文件")
    parser.add_argument("--profile", help="已登录的店铺 Profile：回查并生成 store.local.json")
    parser.add_argument("--category-id", type=int, help="同时刷新该类目实时 reference")
    parser.add_argument("--category-name", default="")
    parser.add_argument("--brand-category-id", type=int,
                        help="品牌查询用的三级类目；叶子类目报“不是三级类目”时传 categoryDetail.thirdCategoryId")
    args = parser.parse_args(argv)

    report: dict[str, Any] = {"skill": str(SKILL), "python": sys.version.split()[0], "checkOnly": args.check}
    report["permissions"] = permission_status(args.check)
    if report["permissions"]["writable"]:
        report["deps"] = ensure_venv(args.check)
    else:
        report["deps"] = {"missing": list(REQUIRED_MODULES), "python": "", "detail": "directory is not writable"}
    exe, origin = find_jdo_cli()
    if exe and origin == "sibling-skill" and not args.check:
        exe, origin = install_jdo_cli_from(exe), "copied-from-sibling-skill"
    python312 = find_python312()
    if python312:
        os.environ["JDO_PYTHON312"] = str(python312)
        if not args.check and os.name == "nt":
            safe_step("persist-python312", persist_python312, python312)
    jdo: dict[str, Any] = {"binary": str(exe) if exe else "", "origin": origin,
                           "python312": str(python312) if python312 else "",
                           "certifi312": python312_has_certifi(python312)}
    if exe:
        os.environ.setdefault("JDO_CLI_PATH", str(exe))
        jdo.update(prepare_runtime(exe, python312, args.check))
    report["jdoCli"] = jdo
    report["profiles"] = jdo_profiles()
    store_path = SKILL / "config" / "store.local.json"
    report["store"] = store_config_status(store_path)
    errors: list[dict[str, Any]] = []
    if args.profile:
        step = safe_step("venderinfo", verify_profile, args.profile)
        identity = step.get("value") or {}
        if not step["ok"]:
            errors.append(step)
        report["verifiedIdentity"] = identity
        if identity.get("venderId") and identity.get("shopName") and not args.check:
            report["storeWritten"] = write_store_config(store_path, identity)
            report["store"] = store_config_status(store_path)
        if identity.get("venderId") and args.category_id and not args.check:
            step = safe_step("reference", refresh_category, args.profile, args.category_id, args.category_name,
                             args.brand_category_id)
            if step["ok"]:
                report["reference"] = step["value"]
            else:
                errors.append(step)
    report["errors"] = errors
    report["apiKey"] = describe_key()
    report["sufyImage2"] = sufy_status()
    report["hiOffice"] = hioffice_status()
    report["browser"] = browser_status()
    report.update(readiness(report))
    report["capabilities"] = capability_status(report)
    report["resultDirectory"] = str(SKILL / "outputs")
    report["nextActions"] = next_actions(report) + [
        action("user" if "login" in item["error"].lower() or "token" in item["error"].lower() else "agent",
               "store-login" if item["step"] == "venderinfo" else item["step"],
               f"{item['step']} 失败：{item['error']}")
        for item in errors
    ]
    report["ready"] = report["environmentReady"]  # installation, not authority to publish
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
