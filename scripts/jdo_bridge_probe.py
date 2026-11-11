"""Large-request bridge helpers: interface probe (no version/hash gate), host lookup, plain-language status.

Why a bridge at all: the official `jdo-cli invoke` only takes `--body "<json>"`; Windows caps a command line at
~32 KB, so big bodies (a publish schema, base64 images) cannot be sent that way. The bridge runs the CLI's own
embedded `cmd_invoke` with the body read from a file. Measured: that embedded code only runs on a real Python 3.12
host (a 3.13 host crashes with 0xC0000005), there is no body-file option in CLI 0.3.4 or 0.3.7, and the publish
`schema` is one atomic string, so it cannot be split. Until an official --body-file exists the bridge stays.

What is NOT a gate anymore: the CLI version and the runtime sha256. A runtime we have never seen is accepted when a
child process proves `cmd_invoke` exists and reads every attribute we set. Store/profile checks, "unknown result is
looked up, never replayed" and secrecy are unrelated to the version and are enforced elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# Windows rejects a command line of ~32.7K characters (measured: 32500-char body ok, 33000 -> WinError 206).
COMMAND_LINE_BODY_LIMIT = 30000
NEEDED_ATTRIBUTES = ('url', 'method', 'params', 'body', 'path_params', 'timeout', 'dry_run')
AFFECTED = ['发品', '整版编辑', '属性补全', '素材绑定']
UNAFFECTED = ['查询', '审计', '授权', '本地生成']

_PROBE_SOURCE = r'''
import sys, json, marshal
from pathlib import Path
ns = {"__name__": "jdo_cli_embedded", "__file__": "jdo-cli.py"}
exec(marshal.loads(Path(sys.argv[1]).read_bytes()), ns)
f = ns.get("cmd_invoke")
attrs = sorted(n for n in (f.__code__.co_names if f else ()) if n in %s)
print(json.dumps({"host": "%%d.%%d.%%d" %% sys.version_info[:3], "cmd_invoke": callable(f), "argcount": f.__code__.co_argcount if f else 0, "attrs_read": attrs}))
''' % (repr(NEEDED_ATTRIBUTES),)


def needs_bridge(body_text):
    """True when the body cannot travel on a Windows command line (counted like the OS does: UTF-16 units)."""
    return len(str(body_text or '').encode('utf-16-le')) // 2 > COMMAND_LINE_BODY_LIMIT


def runtime_sha(data):
    return hashlib.sha256(data).hexdigest()


def probe_runtime(runtime_path, runtime_bytes, *, python, cache):
    """Child-process check that this CLI runtime still has the call the bridge relies on. Cached per runtime hash."""
    digest = runtime_sha(runtime_bytes)
    cache = Path(cache)
    known = {}
    if cache.is_file():
        try:
            known = json.loads(cache.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            known = {}
    if digest in known:
        return {**known[digest], 'sha256': digest, 'cached': True}
    Path(runtime_path).parent.mkdir(parents=True, exist_ok=True)
    if not Path(runtime_path).exists() or Path(runtime_path).read_bytes() != runtime_bytes:
        Path(runtime_path).write_bytes(runtime_bytes)
    result = {'ok': False, 'reason': ''}
    try:
        proc = subprocess.run([str(python), '-X', 'utf8', '-c', _PROBE_SOURCE, str(runtime_path)], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=120)
        data = json.loads((proc.stdout or '').strip().splitlines()[-1]) if proc.returncode == 0 and (proc.stdout or '').strip() else None
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        data = None
    if not data:
        result['reason'] = 'probe-failed'
    elif not data.get('cmd_invoke'):
        result['reason'] = 'cmd_invoke-missing'
    else:
        missing = [a for a in NEEDED_ATTRIBUTES if a not in (data.get('attrs_read') or [])]
        result = {'ok': not missing, 'reason': ('missing-attributes:' + ','.join(missing)) if missing else '', 'host': data.get('host'),
                  'attrsRead': data.get('attrs_read')}
    result['userMessage'] = '' if result['ok'] else interface_changed_message()
    try:
        known[digest] = {k: v for k, v in result.items()}
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(known, ensure_ascii=False, indent=1), encoding='utf-8')
    except OSError:
        pass
    return {**result, 'sha256': digest, 'cached': False}


def interface_changed_message():
    return ('这个版本的京麦 CLI 改变了大请求的发送方式，目前不能用它来「' + '、'.join(AFFECTED) + '」。'
            '查询、审计、授权、本地生成不受影响。下一步：先不要升级或回退 CLI，把这条提示发给 Skill 维护人，由他适配后再继续。')


def host_version(python):
    try:
        out = subprocess.run([str(python), '-c', 'import sys;print(sys.version_info[0],sys.version_info[1])'], capture_output=True, text=True, timeout=30)
        major, minor = out.stdout.split()[:2]
        return int(major), int(minor)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_bridge_python(*, env=None, home=None, extra=None):
    """A real Python 3.12 (the only host the embedded CLI code runs on). 3.13 and others are rejected on purpose."""
    env = os.environ if env is None else env
    home = Path(home or Path.home())
    candidates = []
    if str(env.get('JDO_PYTHON312') or '').strip():
        candidates.append(Path(env['JDO_PYTHON312']))
    candidates.append(home / 'AppData' / 'Local' / 'Programs' / 'Python' / 'Python312' / 'python.exe')
    for value in (extra if extra is not None else _launcher_candidates()):
        candidates.append(Path(value))
    for path in dict.fromkeys(candidates):
        if path.is_file() and host_version(path) == (3, 12):
            return path
    return None


def _launcher_candidates():
    found = []
    try:
        out = subprocess.run(['py', '-3.12', '-c', 'import sys;print(sys.executable)'], capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            found.append(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    found.append(r'C:\Program Files\LibreOffice\program\python.exe')
    return found


def bridge_status(*, exe, python, runtime_ok):
    """Plain-language state for the self-check. Never mentions hashes, probes or internals."""
    available = bool(exe and python and runtime_ok and runtime_ok.get('ok'))
    if available:
        return {'available': True, 'affected': [], 'unaffected': UNAFFECTED, 'userMessage': ''}
    if not exe:
        step = '先完成「京麦 CLI 店铺连接」安装官方京麦 CLI。'
    elif not python:
        step = '让助手为你安装 Python 3.12（官方安装包或应用商店均可）；公司电脑如果被拦截安装，请联系 IT 放行，或把这条提示发给 Skill 维护人。'
    else:
        step = '把这条提示发给 Skill 维护人，由他适配当前京麦 CLI 版本。'
    msg = ('受影响的操作：' + '、'.join(AFFECTED) + '这类大请求暂时不能做。\n'
           '不受影响：' + '、'.join(UNAFFECTED) + '，以及其他只读和小请求，可以照常使用。\n'
           '下一步：' + step)
    return {'available': False, 'affected': AFFECTED, 'unaffected': UNAFFECTED, 'userMessage': msg}
