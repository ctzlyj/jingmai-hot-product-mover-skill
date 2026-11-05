# 安装与首次使用（采销版）

## 一句标准提示词

> 请根据 https://o2.jd.com/install-skill.md，安装 skillId 11153@最新版本。读实际安装目录的 SKILL.md，先运行首次自检；自动补齐环境，告诉我哪些功能可用、结果目录在哪。店铺未授权时提示我先用“京麦 CLI 店铺连接”，不要复制授权代码；等我提供商品和店铺后，先做到发布前校验，正式上架必须等我确认。

“@最新版本”表示不传具体 version，门户会返回最新已发布版本；不要把“最新版本”作为 version 字符串提交。

## 安装位置

按官方 SkillHub 安装指南下载，使用当前 Agent 的实际 Skills 根目录。支持 .joycode/skills、.codex/skills、.agents/skills，也支持散包的 skill-<id> 目录。以安装回执和该目录的 SKILL.md 为准，禁止猜固定文件夹名。脚本通过自身 __file__ 找本 Skill；兄弟依赖通过 name frontmatter 查找，也支持 SUFY_IMAGE2_SKILL 显式配置。

后续命令均从**实际安装目录**运行，而非固定的个人目录。路径有空格时用引号。安装完成可重新扫描技能或新开会话；目录无需重装成另一 Agent 的形式。

## 第一步：首次自检

让助手执行（路径由安装回执提供）：

```powershell
python -X utf8 scripts/bootstrap.py --json
```

没有 Python 时，助手按官方安装方式安装 Python 3.12/3.13（例如 winget install Python.Python.3.12），再执行。自检自动建立 .venv、安装 requirements.txt；pip 超时会按已有回退机制尝试镜像，不改用户指定源。

检查项：Python/依赖、官方 jdo-cli 和大请求体所需 Python 3.12/运行时、Chrome/Edge/Node/浏览器路由器、模型 Key **存在性**、安装与产物目录权限。不读取 Key 值、不解密 DPAPI，不验证额度或模型可用性。不读取浏览器 Cookie，不连接用户工作浏览器，不绑定固定 CDP 端口；进程检查/子进程隐藏窗口。浏览器安装存在不等于登录或采集已可用。

成功后看到 JSON：
- environmentReady=true：核心安装环境通过；ready 是其兼容字段。
- businessReady=true：本次明确选择的非 default Profile 已通过只读店铺核验，且当前检查无平台错误；这**不代表已上架**。
- capabilities：功能是否可用，以及各项前置条件。
- nextActions：缺项的下一步，owner=agent 由助手处理；需要用户登录/店铺身份时明确提示，不能假装已完成。
- resultDirectory：实际安装目录下 outputs；标准任务结果保存到本批结果目录，助手交付完整本地路径。自检不保存 Key、Token 或账号凭据。

jdo-cli 缺失时，助手可根据同一公开安装指南安装依赖 SkillHub 10833（jd-jingmai-batch-image-refresh，内含已验证的官方二进制），然后重跑自检。公开下载不需要 HiOffice；需要内网鉴权时使用者需保持京ME/HiOffice 登录。没有可用授权时停止相关操作。

## 无 Key / 无浏览器时的功能边界

| 功能 | 生图 Key | 浏览器与登录 |
| --- | --- | --- |
| 已有本地素材/表格的整理、草稿、离线预检、构建发布体 | 无 Key 也能用 | 不需要浏览器 |
| 官方 jdo-cli 商品查询、类目参考、发布前平台校验 | 无 Key 也能用 | 需要“京麦 CLI 店铺连接”已授权的 Profile |
| 公司 Flare/Sunburst 重制图片、需要生成图的矩阵任务 | 需要使用者自己的公司 Key | 公司网关需可访问；不能用旧图冒充生成完成 |
| sufy-image2 外部生图/爆款复刻 | 需要安装 sufy-image2 并配置使用者自己的 Key | 没有就停在生图前；不自动付费回退 |
| 网页取价/淘宝登录采集/浏览器补采视频 | 不因缺生图 Key 停止 | 需要任务专属 Playwright/CDP 与来源平台登录；没有就停止该来源采集 |

Key 仅由使用者在凭据管理/环境配置界面设置，不要发给助手或写明文文件。自检只报告有没有，不代表 Key 有效。

## 店铺授权与发布前校验

没有店铺授权时，先对助手说 **“京麦 CLI 店铺连接”**，使用相应连接器完成目标店铺登录。禁止本 Skill 自己复制 OAuth 授权代码、Token 或别人的 Profile。配置从当前只读店铺身份接口核实，不复制别人配置；命令 Profile、配置 Profile、venderId、店名必须一致，禁止 default。

授权后由助手执行：

```text
python -X utf8 scripts/bootstrap.py --json --profile <已授权Profile> --category-id <叶子类目> --category-name <类目名> [--brand-category-id <三级类目>]
```

回查身份后生成 config/store.local.json（不覆盖有效配置），刷新当前类目 reference。叶子类目报“不是三级类目”时按平台返回补品牌查询三级类目，不猜编号。运费模板由店铺当前数据核实。

写入前仍逐次回查三要素；权限不足、风控、结果不明时停相关操作，先对账不重放。平台校验通过后交付预检结果和发布体；正式上架需用户明确确认。

## 淘宝来源前置条件（可选，不是所有采销必装）

jd-taobao-hot-sales 暂不推广，缺失时不自动安装它。tests/browser_collector_smoke.py 是额外的隔离浏览器测试：缺它或浏览器路由器时对应测试 **SKIP**，不影响原有离线核心测试。

真实淘宝采集仍需当前可用的来源适配器、Node/Playwright（或已验证的浏览器任务路由器）、来源平台本人登录和商品访问权限。可复用已授权的 --capture 结构化文件走本地草稿；文件必须包含真实 SKU、有效价格和详情，不能猜价格。没有合法采集能力时提示该来源暂不可用，其他本地/京麦链路不被误判成失效。具体规则见 taobao-source-adapter.md。

## 手动诊断（只由助手执行）

- 不同位置的 Python 3.12：设置 JDO_PYTHON312 指向本机真实可执行文件，不写固定个人路径。
- 非默认 jdo-cli：设置 JDO_CLI_PATH 指向官方安装文件。
- 只检查不安装：python -X utf8 scripts/bootstrap.py --json --check（权限仅静态检查；不创建配置/环境）。
- 核心测试：.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests。
- 额外浏览器测试：.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p browser_collector_smoke.py。

所有配置、缓存、日志、运行产物留在使用者本机，不进 GitHub/SkillHub 发布包。
