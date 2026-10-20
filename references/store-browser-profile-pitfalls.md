# 店铺浏览器 profile 操作防错规则（实测事故沉淀）

背景：2026-10-20 英达齐齐店任务中，助手在 PowerShell 5.1 脚本里用 `[char]` 码拼接中文店铺名「英达齐齐店」时算错一个字符（40754≠40784），静默创建了一个乱码名字的全新空 Chrome profile（`茱迾鼲鼲店`），随后在该空白浏览器里探测登录态，得出「店铺登录态失效」的错误结论，反复要求用户扫码，浪费近 1 小时。用户桌面存在多个名称相近的店铺快捷方式（英达齐齐店/齐齐店/小达齐齐店），每个是独立会话，进一步放大了混淆风险。

## 硬性规则

1. **禁止在 PowerShell 5.1 脚本中拼接中文路径或中文 profile 名。** 任何含中文的 `.ps1` 必须**由 Python 生成且写入时带 UTF-8 BOM**（`encoding="utf-8-sig"`），或干脆不含中文字面量（用 `$PSScriptRoot`、参数传入、`[System.IO.File]` API）。无 BOM 的 UTF-8 中文在 PowerShell 5.1 下按 GBK 解析，语法直接损坏或语义静默漂移。
2. **启动/关闭店铺浏览器前后必须用进程命令行回读校验 profile 路径。** 启动后：`Get-CimInstance Win32_Process -Filter "name='chrome.exe'"` 枚举所有 `--user-data-dir` 实例，逐一比对**磁盘真实目录名**（可用 `Get-ChildItem ... -Directory` 的结果做基准），确认目标实例的 user-data-dir 与预期完全一致（逐字符相等，不是模糊匹配）。发现未知/乱码目录立即停止并清理，不得在其上做任何探测。
3. **判定「登录态失效」前必须先证明探测的是正确浏览器。** 证据链：CDP `HttpAgent` 头或 `Target.getTargetInfo` 的 browserContext → 进程 PID → 该 PID 的命令行 user-data-dir → 与店铺快捷方式解析出的 profile 路径一致。四环缺失任何一环，结论无效，不得让用户扫码。
4. **匹配进程命令行时注意引号。** 快捷方式启动的 Chrome 命令行是 `--user-data-dir="C:\...\店名"`（带引号），`-like '*--user-data-dir=店名*'` 会失配；用 `-match` 配合 `user-data-dir="?...店名` 或直接比较 `CommandLine.IndexOf(profilePath) >= 0`。
5. **关闭用户正在使用的店铺浏览器必须先优雅关闭**（`CloseMainWindow()`，等 8 秒落盘），仅残留进程才 force-kill；强制结束进程可能丢内存中未落盘的会话 Cookie。禁止上来就 Stop-Process -Force。
6. **同端口 CDP 复用检查**：起 `--remote-debugging-port` 前先探测端口是否已被占用；Chrome 对已占 profile 的新启动请求会把 URL 转发给现有实例（此时新参数里的调试端口会被**忽略**），表现为「启动成功但 CDP 连不上」——这是 2026-10-20 事故的直接表象之一。正确做法：确认旧实例已全部退出后再启动，或直接复用现有实例而其没有调试端口时，才允许优雅重启。
7. **店铺名相近的多 profile 场景（英达齐齐店/齐齐店/小达齐齐店等）**：所有操作必须从用户给出的**具体 .lnk 文件**解析（WScript.Shell CreateShortcut），禁止凭记忆或名称前缀猜测 profile。

## 正确的店铺浏览器登录复用流程（验证通过）

1. 解析 .lnk 得到 profile 目录。
2. `jdo-cli login --profile <name> --no-browser` 后台起回调（stdout/stderr 重定向到文件，记录 pid）。
3. 从 stderr 提取授权 URL，写入 auth-url.txt（UTF-8）。
4. 用带 BOM 的 UTF-8 ps1 或直接 Python subprocess 以**解析出的精确 profile 路径**启动 Chrome 打开授权 URL（若旧实例活着且无调试端口，新窗口 URL 会转发进现有实例——此时授权页就开在用户店铺浏览器里，直接让用户在授权页确认即可）。
5. 轮询 login stdout 出现 `"ok": true` 即 token 入库；随后立刻用 venderinfo 接口核验 venderId/shopName 三要素并 `mark_jdo_store_verified`。
