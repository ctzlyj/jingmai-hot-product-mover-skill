"""给批量脚本用的子进程静默包装：调用 jdo-cli 等 CLI 时不弹控制台窗口。

用法（在脚本 import jdo_listing_mvp 之前）：

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import win_no_window  # noqa: F401

Windows 下会给 subprocess.run/Popen/call/check_output 统一追加
CREATE_NO_WINDOW 与 STARTF_USESHOWWINDOW/SW_HIDE，避免逐条命令闪黑框。
"""

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000

_original_run = subprocess.run
_original_popen = subprocess.Popen


def _hidden_kwargs(kwargs):
    flags = kwargs.get("creationflags") or 0
    kwargs["creationflags"] = flags | CREATE_NO_WINDOW
    if sys.platform == "win32":
        startupinfo = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
    return kwargs


def run(*args, **kwargs):
    return _original_run(*args, **_hidden_kwargs(kwargs))


class Popen(_original_popen):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **_hidden_kwargs(kwargs))


subprocess.run = run
subprocess.Popen = Popen
subprocess.call = lambda *a, **k: run(*a, **k).returncode
subprocess.check_output = lambda *a, **k: run(*a, stdout=subprocess.PIPE, **k).stdout

