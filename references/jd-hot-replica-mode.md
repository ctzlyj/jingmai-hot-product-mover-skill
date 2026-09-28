# 京东爆款复刻模式（2026-09-27）

## 模式定义

用户提供**卖得好的京东商品链接**和目标 POP 店铺：采集完整商品信息 → 素材全量**高级重制**（商品本体不变，主图/详情页全部换成更高级的版本）→ 图片门禁与视觉审图 → 用户审核 → 发布到目标店铺 → 独立回查。用户随后自行在 1688 寻找货源获取价格优势；选品与供货不由本流程负责。

本模式取代已删除的旧“无货源模式”（xlsx 建批、全店 brandId=-1、随机编码入 ledger、批次根目录管线，已于 2026-09-27 以 `2b27b13` 移除）。不得再引用 `scripts/ni/`、`references/no-inventory-mode.md`、`listing_batch.py`、`batch-config.json`。

与保真搬运的本质差异：图片验收目标从“与来源首图排版一致”改为 **商品本体一致 + 更高级美观 + 无违规点 + 排版正常美观**。

## 生图与提示词规则（源自 image-studio 已验证实现）

知识来源：自有生图平台 `ctzlyj/image-studio`（main=`e4e4011`，本地 `D:\caotong.888\Documents\ChatGPT\image-studio`）。商品详情图相关实现在 `src/server/replica/`（爆款复刻）、`src/server/ai/segment-visual-plan.ts` + `image-prompt.ts`（标准 A+ 六段视觉计划）、`src/server/workbench/`（自定义生图三协议客户端）、`src/server/ai/sufy-image-provider.ts`（provider 契约）。本 Skill 直接调用 LTS4AI sufy-image2，不依赖该平台在线服务。

### 主图高级重制

- 由 agent 依据来源主图与商品事实自主设计生图提示词；改图一律直接走 LTS4AI sufy-image2（用户已确认不走内部生图模型），并发 2-3、重试≤2 轮，禁止本地 PS 掩码。款式/SKU 尺寸标注图如需去掉文字，用 sufy 编辑"移除全部文字+标注线，商品本体/款式/场景不变，遮挡区自然补全"（泡澡桶 4 张代表图已验证）。源 SKU 图本身保留原样，作为按 SKU 上传的规格图。
- 参考图即保真契约：商品**颜色、版型、轮廓、材质、图案、结构、配件和比例**以用户商品参考图为准，本体不得变形、不得改变内容。
- 多主图组按“连续购买路径”组织而非重复首图：主图（一个核心购买理由：1 条主标题 + 1 条副标题 + ≤2 个可信证据标签）、场景图、卖点图、白底图、认证图（不得虚构认证）、四宫格图。文字只能来自标题、参考图或用户明确提供的信息；无证据时减少文字。
- 重构图不留空白空洞、无乱码错字；所有可见中文由生图模型逐字绘制，不使用 HTML/SVG/Canvas/本地字体叠字。
- 人物统一用 sufy“移除人物+背景自然补全”口径。
- 直接删除：专利图、淘金币图、价格/活动说明图、品牌信息图、公司介绍页、源头工厂图、平台专属图、无价值页、违规密集页。
- 标题/SKU/文案去除：品牌词、日本/德国/进口、第三方平台词、旗舰/官方/正品/立减/厂家直销/国补/极限词/特殊符号。

### 详情页爆款复刻（image-studio replica 方法）

来源详情长图作为**结构模板**，按以下流程复刻为高级版本：

1. **切片分析**：将来源详情长图按纵向模块边界切片（归一化 0-1 坐标，≤12 段；超限则不合并、如实上报），每段记录 `purpose`（用途）、`purposeDescription`、`layoutSummary`（布局摘要）。仅分析版式结构与信息类别，模板中的品牌、价格、促销、参数、承诺**一律不当作用户商品事实**。
2. **事实与文案**：从采集到的商品事实（标题、属性、SKU、已确认卖点）出发生成每段文案（title/subtitle/body/bullets/exactVisibleText）；缺失事实先问用户，**禁止照抄模板品牌、Logo、价格、促销、功效或承诺**，禁止编造。
3. **逐段生成**：参考图顺序 = 模板切片（仅约束构图、配色、版式节奏和视觉结构）+ 用户商品主图 + 至多 1 张辅助图（场景段选场景图、品牌/服务段选 Logo 图、其余选细节图）。提示词固定要素：目标画布尺寸、模块用途与布局摘要、“必须替换模板中的商品、Logo、文字、价格和促销元素”、“商品形态以用户商品参考图为准”、`exactVisibleText` 逐字渲染清单、禁止本地叠字、四周 ≥8% 安全边距、指定文字之外禁止任何可读伪文字。
4. **尺寸与拼接**：输出宽 750px；provider 尺寸取最接近比例档（如 2:3=1024x1536），生成后校验实际像素并等比缩放到 750 宽，全部段通过门禁后纵向拼接为长图。
5. 若来源详情本身违规密集或无价值页过多，先按视觉审图规则删减，再对保留页做复刻；无法可靠保真时停该商品。

### LTS4AI sufy-image2 API 契约

- Base URL `https://api.lts4ai.com/v1`，`Authorization: Bearer`，模型默认 `SF-gpt-image-2.5-flare`（日常批量），`SF-gpt-image-2.5-sunburst`（精修/重要成片），`SF-gpt-image-2`（延续原方案）；保留用户显式选择，不得静默回退。
- 文生图 `POST /images/generations`（JSON：model/prompt/size/output_format=png，不发送 n、seed、response_format）；多参考编辑 `POST /images/edits`（multipart，image 按参考顺序重复，JPEG/PNG/GIF/WebP，≤12 张且源字节 ≤15MB）。
- 比例映射：1:1→1024x1024，16:9/4:3/3:2→1536x1024，9:16/3:4/2:3/4:5→1024x1536，21:9/5:4/2:1→auto；4K 有独立映射；编辑时 Adaptive 按第一张参考图取最近档。
- Key 走 DPAPI 凭据（`%LOCALAPPDATA%\JoyCode\credentials\sufy-image2.dpapi`、`oxygen-product-pro.dpapi`），只进进程环境变量，绝不进源码/日志/文档/回复。
- 超时 1K/2K=10 分钟、4K=30 分钟；重试预算 2 次（1s/3s），仅显式 429 与只读 GET 5xx；**生图 POST 出现 408/409/5xx、网络中断或响应不完整即结果不明：先对账已产出图片与计费，不得自动重放整批**；已成功的输出文件保留，失败批不重放成功任务。

## 门禁与验收

- `image_quality_gate.py` 已实现高级重制口径（首个 MVP 已交付，2026-09-28）：运行加 `--gate-mode remake`，报告 `promptVersion=5` 且携带 `gateMode":"remake"`，与 fidelity（v4）版本与模式双向隔离、混用即拦截。`productSame` 严格（商品本体颜色/版型/轮廓/材质/图案/结构/配件/比例/数量一致）、`violationsCleared`、`qualityOk`、`noHoles`、`noNewArtifacts`、`textLegible` 必查；`layoutPreserved` 已由 `layoutClean`（排版正常美观、无重叠/溢出/错位）取代，不再要求与来源图排版/文案一致，首图允许新构图；整条塌缩带检测不适用于 remake（重制本就会重画所有横带），空洞由单图 layoutOk/uncertain 与 pair 模型 noHoles/layoutClean 把关。回归测试见 `tests/test_image_quality_gate.py` 三个 remake 用例。
- 视觉审图：`gemini-2.5-flash-lite` 全量初筛，疑似项与看不清的图用 `gemini-2.5-flash` 复核；两档冲突不得判干净。删除/放行结论必须多遍收敛，且模型逐字引用违规原文作证据；单遍判断不可靠（实测同批两遍判删 24 vs 2），拿不准保留并标记待审。
- 缓存按提示词版本与图片 SHA-256 绑定；构图疑似塌缩带强制 BLOCK。

## 发布与回查

- 沿用现有京东来源链路：`jd_product_mover.py`/`jd_product_pipeline.py` 采集建稿，`jdo_listing_mvp.py`/`publish_local_draft.py` 发布，`deferred_publish_batch.py` 幂等回查。
- 图片落位规则（2026-09-28 泡澡桶全链路验证）：主图画廊只按颜色（第一维销售属性）分组；同款式共用一套组图 `[款式代表图@1 + 公共主图@2-9]`，SKU 缩略图=款式代表图；**逐 SKU 尺码图写入 `SkuGgtImage` 规格图**（imgRefId=该 SKU 的 uuid/skuId），选尺码时展示对应标注图。编辑场景 colorId/imgRefId 必须用 VenderCategoryValue 卡片的 mappingId（UUID）；原子 PATCH 的落位陷阱与恢复流程见 `references/sku-image-group-binding.md`。
- 每次京麦写操作前 `A.identity()` 三要素核对（店铺名+venderId+Profile 完全一致），禁止 default Profile；其余 6 店 venderId 首次使用时用京麦 CLI 实时查询核对，禁止按店名猜。
- 正式发布需显式确认字符串 `PUBLISH_TO_JD`；线上补丁先 dry-run 再 apply，PATCH 后独立回读确认 `ALREADY_CURRENT`；写结果不明只 reconcile 不重放。
- 原链接视频可照搬（`automate_main_video` 幂等绑定并逐 SKU 回查）；已有 SKU 绑定其他视频时停止，不覆盖。
- 用户自行下架的商品不代为上架；不动用户正在用的浏览器标签。

## 定价规则（用户已确认，2026-09-28 泡澡桶批次验证）

- 基准：**竞品（源链接同款 SKU）到手价，我方在其基础上降低 3%**。竞品到手价按源 SKU 京东价 ×（1−竞品补贴率）取（表格"到手价"列优先；无表时用源商品标签推补贴率，如焕新补贴 15%，锚点 288→244.8 已验证）。
- 目标到手价 `T = 竞品到手 × 0.97`，逐 SKU 按 `listing.json` 的 `sourcePrice` 映射，不得按尺码猜阶梯。
- 反推京东价满足：`Jd − round(Jd×0.15, 2) − round(Jd×0.85×0.15, 2) = T`（直降 15% + 国补 Jd×85%×15%，均为默认配置活动）。在 `T/0.7225` 附近 ±0.60 内搜分值精确解；无精确解才用最近似解并在回执注明。
- 逐 SKU 写 `SkuJdprice` 随整版编辑提交；线上生效后逐 SKU 复核 `jdPrice` 与反算到手价，40/40 需全对。锚点示例：源 288 → 竞品到手 244.8 → 我方到手 237.46 → 京东价 328.66。
- 后续拿到 1688 供货价要调价时，仍按此公式重新反推，不直接改到手价。

## MVP 执行方式

先做单商品端到端：用户提供第一条京东链接、目标店铺和定价基准后，采集 → 重制 → 门禁 → 用户审核 → 发布 → 回查，全链路记录各阶段耗时与失败点，跑通后再批量。新类目按 `references/adapting-new-category.md` 小规模验证，不直接套旧 Schema。

## 已验证里程碑与待办

- 2026-09-28：首条真实链接（泡澡桶 10233331213967 → 10036194410506）端到端跑通：sufy 重制主图/详情 → 门禁 → 发布 → 颜色组+代表图修整 → 40 SKU 规格图 → 竞品到手价×97% 反推定价 40/40 生效。新对话按 `references/jd-hot-replica-handoff.md` 执行。
- 待办：京东来源详情长图的切片分析 Python 侧适配（文本模型多图 JSON 分析已在 image-studio 验证，尚未接入本 Skill 主流程）；规格图批量上传后的买家端样式抽查待用户实测反馈。
