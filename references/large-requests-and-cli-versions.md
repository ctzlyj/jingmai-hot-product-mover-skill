# 大请求与京麦 CLI 版本（1.2.6）

## 什么是“大请求”

Windows 命令行最长约 32KB。官方 `jdo-cli invoke` 只有 `--body "<JSON>"`，没有读文件的参数（0.3.4 与 0.3.7 一致）。发品整版 schema、带图片 Base64 的请求经常超过这个长度。实测：body 32500 字符能发，33000 字符起直接报 `WinError 206`；本 Skill 过往 75 份发布体里 12 份超过 32000 字节，最大 25 万字节。发品接口的 `schema` 是一个整体字符串，没有分片或追加接口，不能硬拆。

## 现在怎么发

- **小请求**（命令行放得下）：直接用官方 CLI，不需要系统 Python，也不提取 CLI 内部代码。
- **大请求**：用 `jdo_large_body_bridge.py` 从文件读 body，运行官方 CLI 自带的 `cmd_invoke`。这段代码只在 **Python 3.12** 宿主上运行（实测 3.13 宿主会崩溃退出）。

## 版本不再是门槛

桥不再因“运行时 sha256 / CLI 版本不认识”而拒绝。遇到没见过的 CLI 版本，先在子进程里确认 `cmd_invoke` 仍存在、且仍读取我们设置的全部字段（url、method、params、body、path_params、timeout、dry_run），通过就放行（结果按运行时指纹缓存，不同版本互不影响）；不通过才提示“这个版本改变了大请求的发送方式”。已在 CLI 0.3.4 与 0.3.7 上验证通过，记录见任务验收目录。

不属于版本门槛、保持不变：写入前核对店名/venderId/Profile、写入结果不明先回查不重试、凭据不进日志。

## 自检怎么说

没有可用的 Python 3.12，或 CLI 改了接口时，自检只说三件事：受影响（发品、整版编辑、属性补全、素材绑定这类大请求）、不受影响（查询、审计、授权、本地生成及其他只读和小请求）、下一步怎么办。桥不可用**不会**让 `environmentReady` 变成 false，只在 `capabilities.largeRequests` 和 `nextActions` 里单独列出。

## 为什么没有去掉 Python 3.12（方案结论）

- 方案甲（用 CLI 自带环境或拆分 body）：不可行。官方 CLI 不接受脚本/`-c`/`-m`，`PYTHONPATH/PYTHONSTARTUP/PYTHONHOME` 对冻结程序无效；内嵌的 `python312.dll` 不能脱离 `python.exe` 单独承载；官方 `invoke` 没有文件读取通道。
- 方案乙（自己按签名规则发请求）：签名规则公开（`md5(appSecret + 排序后的key+value + appSecret).upper()`），但需要读取凭据管理器里的 AppSecret 与 OAuth Token。**必须由负责人先确认合规，未确认前不做**，也不读取、不记录这些凭据。
- 因此在官方给 `invoke` 增加 `--body-file` 之前，大请求仍需要 Python 3.12。

## 分发（需要 jingmai-cli-linker 配合，本仓库不改）

- `vendor/jdo-cli`（SkillHub 10833 包内）仍内置 0.3.4，等于我们在分发旧版 CLI。建议 `jingmai-cli-linker` 的 `--install-jdo` 不再依赖内置二进制，改从官方 update 通道取最新（`jdo-cli update check` 返回当前稳定版的下载路径与 sha256，已核对 0.3.7 windows-x64 zip 为 `76a1011e…`）；或至少把内置版本更新为 0.3.7。
- 本 Skill 兼容两者，不再要求某个固定版本。
