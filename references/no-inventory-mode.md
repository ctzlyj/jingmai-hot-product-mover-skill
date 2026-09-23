# 无货源模式搬运上架

来源：2026-09-22/23 千百日用品质店店香薰批实测跑通后的通用化沉淀。本页只保留已复现、已验证的规则；未验证事项列入文末待办，不编造结果。

## 1. 模式定义与触发

无货源模式 = 商家直接提供一份包含全部素材信息的 Excel（主图、SKU 图、详情图、视频、价格、规格等），表格即素材源。

- 不再强制完整淘宝链接 / mi_id 校验，不再要求线上采集登录。
- 不启动临时 Chrome、不反复要求登录、不动用户正在使用的浏览器标签。
- 采集环节替换为“表格审计 + 素材落盘校验”。
- 相似款不自动舍弃，只阻止同一来源/同一编码的技术性重复。

本模式只做商品搬运上架素材处理与流程沉淀；不发布授权外的商品，不改动用户自行下架的商品。

## 2. 批次目录与 batch-config.json

每个批次一个根目录（`--root` 或环境变量 `NO_INVENTORY_ROOT`），布局固定：

```text
<batch-root>/
  batch-config.json        profile / venderId / shopName / freightTemplateId / categories / pricing
  store-config.json        本店身份（A.identity() 三要素核对用）
  ledger.json              商品状态 + 已生成编码（必须复用，禁止重新随机）
  source/                  源表 dump
  images/raw|compliant/    原始与合规素材
  candidates/<item>/      candidate.json / state.json / normalized/
  reports/                 审计、定价、改图、发布回执
```

`batch-config.json` 关键字段（香薰批实测配置为默认值）：

```json
{
  "batchId": "<batch-id>",
  "profile": "<store-profile>",
  "venderId": 0,
  "shopName": "<店铺名>",
  "freightTemplateId": 0,
  "stock": 200,
  "sevenDayReturn": "1",
  "codePrefix": "<大写字母前缀>",
  "personHandling": "remove",
  "pricing": {
    "targetArrivalMultiplier": "1.35",
    "belowCompetitor": "0",
    "nationalSubsidyRate": "0",
    "activityDiscountRate": "0.15",
    "coupon": "0",
    "roundingMode": "component"
  },
  "categories": {
    "<categoryId>": {"name": "<类目名>", "reference": "<schema 缓存文件>", "sale": {"style": {}, "size": {}}, "brandRequired": false}
  },
  "columns": {"itemId": "商品ID", "title": "商品名称", "skuAttr": "SKU属性", "skuPrice": "SKU价格", "skuStock": "SKU库存", "skuImage": "SKU属性图", "mainImage": "主图{i}", "detail": "商品描述图", "video": "商品视频", "saleLink": "商品销售链接", "attributes": "商品属性"},
  "extraBrands": {},
  "saleUnitHints": {"liquid": [], "solid": []}
}
```

## 3. 输入审计

- 逐行审计 xlsx：素材 URL 可下载、SKU/规格真实、价格可核实、详情图齐备。
- 表格关键词列的词作为清理后标题的前缀。
- 素材无法确认、SKU 缺价、规格不完整的商品：记录状态码后跳过，不阻塞整批。
- 验证批状态码分布（45 件）：VERIFIED=31、SKIPPED_BRAND_REQUIRED=10、SKIPPED_INCOMPLETE_SOURCE_SKU=3、SKIPPED_MISSING_DETAIL_MATERIAL=1。
- SKU 四列严格按位置对齐，空单元格不得丢弃；占位行、纯宣传行剔除并记录，不发布假 SKU。

## 4. 类目与品牌规则

- 类目唯一允许范围 = JoySpace 允许类目表；京东类目 ID 和名称必须存在于表内，表外类目一律不选。
- 类目表“关键词”列作为标题前缀，避免重复关键词，遵守京东标题长度限制。
- 全部商品默认无品牌 `brandId=-1`。
- 类目强制要求品牌且无法选无品牌：记录 `SKIPPED_BRAND_REQUIRED` 舍弃，不编造品牌、不改挂表外类目。

## 5. 标题与 SKU 清理

标题、SKU 销售属性名称必须去除：品牌词、日本、德国、进口、淘宝/天猫等第三方平台词、旗舰、官方、正品、立减、厂家直销、国补、极限词、特殊符号和 emoji。只保留中文、英文字母、数字和空格。

最终标题格式：`类目关键词 + 清理后的商品标题`（≤60 字符单位）。必须保留全部真实 SKU 和真实属性组合，不得制造或删除规格。

## 6. SPU/SKU 编码与幂等

- SPU 编码、商品货号、商家 SKU 编码全部随机生成（安全英文字母+数字），不用淘宝 ID、不用旧批次编码；以京麦实时 Schema 允许的字符和长度为准。
- 全店唯一 + 本批次唯一，生成后立即写入 ledger/state。
- 同一商品重试必须复用原编码，禁止重新随机。
- 写入结果不明：先按已保存编码和申请单查询远端 reconcile，禁止盲目重发（`SUBMISSION_RESULT_REQUIRES_RECONCILIATION` → 先查再决定）。

## 7. 价格规则

- 来源价 = 每个真实 SKU 当前可核实的到手价（不用划线价、最低展示价或其他 SKU 价格代替）。
- 目标到手价 = 来源价 × 1.35；只按默认 15% 活动反推京东价：无国补、无优惠券、不减 0.10。
- 必须调用 `scripts/source_pricing.py` 的 `component_source_plan(source_price, pricing)`，禁止手算或简单除法；配置见第 2 节 pricing 块。
- “目标到手价”只是 15% 规则的理论值，不得称为消费者页面当前真实到手价。
- 默认库存 200，七天退货代码 "1"。

## 8. 图片合规与生图流水线

### 8.1 违规点清单（标题、主图、SKU 图、详情图、视频画面）

品牌/Logo/商标、旗舰、官方、专卖店、商标；电话/手机号/微信/二维码及联系方式；厂家直销、源头工厂、专利；德国、日本、进口、外国图标；淘宝/天猫等第三方平台信息与平台活动图（淘金币图）；价格说明图、图中价格、极限词、水印；详情页公司介绍页；人物肖像（含疑似真人）一律移除并去掉人名。

### 8.2 处理规则（实测有效）

- 一律使用生图模型改图，禁止本地 PS 掩码合成（实测会错位、空白、空洞）。
- 不是抹掉违规词，而是合理重构画面，不留空白；商品本体（外观、颜色、规格、数量）不得变形；整体比原图更好看但不做大幅修改。
- 品牌信息图、价格说明图、淘金币图、公司介绍页、源头工厂图：直接删除，不重绘。
- 人物处理统一口径：只要有人（不管是否真人），用 sufy “移除人物 + 背景自然补全”；不再尝试“替换人物”（sufy 对真人替换有安全拒绝，移除模式实测全部通过）。
- 每张修改图重新扫描并生成合规报告；用户判定保留原图的按用户决定执行。

### 8.3 模型路由（实测通过率）

生图池顺序：Oxygen-Product-Pro → Oxygen-Imagen → sufy-image2，失败自动流水线转下一个，同图打回重试不超过 2 轮。

| 模型 | 定位 | 严格编辑通过率 | 备注 |
|---|---|---|---|
| Oxygen-Product-Pro | 商品主图/场景图、文字渲染强 | ~37% | 内部网关 `http://llm-gw.jd.local/v1/images/edits`，429/500 频发，并发 2-3 稳定 |
| Oxygen-Imagen | 最多 8 参考图、高保真编辑 | ~68% | 同上网关，人物类任务更适合 |
| sufy-image2 | 高并发兜底主力 | ~93% | 生图任务效率优先，全兜底 |

验收：LTS4AI `gemini-2.5-flash` 双图对比（原图 vs 修改图），任务感知口径（人物任务允许构图变化；损伤修复任务允许修字；移除模式按移除口径判）。PASS 优先级 pro > imagen > sufy。

API Key 一律走 DPAPI 凭据（`%LOCALAPPDATA%\JoyCode\credentials\` 下 `oxygen-product-pro.dpapi`、`sufy-image2.dpapi`），密钥绝不进源码、日志、文档或回复；Key 失效找用户要新的并立即换 DPAPI 存储。

## 9. 视频策略

- 直接照搬原链接视频，不扫描、不审核视频内容。
- 绑定走 `jd_product_pipeline.automate_main_video`（幂等：跳过已上传视频和已绑定 SKU），逐 SKU `video_relation_audit` 回查，全部审核通过才算完成。
- 已有 SKU 绑定其他视频时停止，绝不自动覆盖。

## 10. 两阶段发布流程

**阶段1（本地候选）**：审计表格 → 素材落盘 → 类目映射 → 标题/SKU 清理 → 随机编码入 ledger → 改图 → 定价 → candidate/合规报告/台账 → 本地静态校验 → 整批汇报，等待 `PUBLISH_TO_JD`。不上传、不 validate、不发布。

**阶段2（正式上架）**：店铺身份三要素回查（店铺名 + venderId + jdo Profile 完全一致）→ 刷新类目实时 Schema → 本地预检 → validate → 逐件 publish 立即存回执 → 全部提交后统一 verify → 单件失败记录后继续 → 写状态不明先 reconcile 禁止盲目重放 → 逐 SKU 回查标题/类目/编码/京东价/库存/图片/详情/上架状态。

### 线上补丁规则（实测）

- 先 dry-run 再 apply；`PATCH_SUBMITTED` 后必须独立回读确认 `ALREADY_CURRENT`，PATCH 成功不等于生效。
- 主图数量变化必须显式 `--allow-count-change`。
- SKU 线上图源取 `candidates/<item>/normalized/`（不是 compliant）。
- 详情全量刷新走 `refresh_detail_full.py`。
- 用户自行下架的商品只备素材，不代为上架。

## 11. 脚本清单（scripts/ni/）

| 脚本 | 作用 |
|---|---|
| `build_candidates.py` | 表格→candidate/ledger 构建（参数化列名/关键词/编码前缀） |
| `listing_batch.py` | 阶段1 prepare / 阶段2 validate·publish·verify·run（幂等状态机） |
| `assemble_compliant.py` | 合规图装配（原件保留、md5 复用、删除项按 plan） |
| `compliance_gate.py` + `ocr_queue.ps1` | OCR 终检门禁（Windows OCR，任务队列可参数化） |
| `compose_model_edits.py` | 生图编辑任务编排（保留原像素，仅违规区域用模型输出） |
| `oxygen_pool.py` | 内部网关生图池（pro/imagen 路由，并发 2-3） |
| `sufy_fallback.py` | sufy 高并发兜底（人物=移除口径） |
| `oxygen_verify.py` | LTS4AI 视觉验收（任务感知口径，人物默认移除模式） |
| `apply_model_edits.py` | 验收 PASS 图回写 candidates（dry-run/apply） |
| `publish_batch.py` / `publish_retry.py` | 批量发布与幂等重试（reconcile before replay） |
| `verify_local.py` / `reuse_verify.py` | 本地残留核验与复用模型输出核验 |
| `bind_videos.py` / `reaudit_videos.py` | 视频照搬绑定 + review_pending 复审 |
| `update_main_images.py` / `update_detail_images.py` / `refresh_detail_full.py` | 线上图增量补丁（含 `--allow-count-change`） |
| `batch_remote_audit.py` | 整批远端审计（标题/类目/编码/价格/库存/上架） |
| `vision_audit.py` / `vision_flags_round.py` | 违规扫描与必修项标记 |
| `batch.py` | 批次根目录/配置解析（`--root` / `NO_INVENTORY_ROOT`） |

一次性调试脚本（`_*.py`、`probe_*.py`）和日志不迁移。

## 12. 店铺接入

新店铺逐店接入：

1. 用官方京麦 CLI 在有效授权内查询店铺真实 venderId 和店铺名，禁止按店名猜测。
2. 建店铺专属 jdo Profile（禁止 default）。
3. `config/store.local.json` 的 `stores` 白名单增加该店条目（venderId、shopName、freightTemplateId），已有条目不动。
4. 写入前 `A.identity()` 三要素核对（店铺名 + venderId + Profile 完全一致）。
5. 首批商品先跑阶段1本地候选，再按确认发布。

venderId 未实时核验的店铺条目必须保持 `enabled=false` + `pendingVenderId=true`，身份校验会直接拒绝使用。

## 13. 已复现故障经验

- 内部网关 429/500 频发：并发压到 2-3，失败自动转 sufy，同图打回重试不超过 2 轮。
- sufy 拒绝真人替换任务：改“移除人物 + 背景自然补全”口径后全 PASS。
- 本地 PS 掩码出现错位/空白：全部改为生图模型重生成后重新上传。
- 纯品牌信息图：删除比修复更快更安全。
- 线上 PATCH 后必须独立回读；PATCH_SUBMITTED ≠ 生效。
- PowerShell 读取 UTF-8 JSON 显示乱码：用 .NET ReadAllText(UTF8) 或 Python 处理台账。
- 登录失效/风控：保存进度停止请求，不把后续商品批量标记失败，不启动临时 Chrome。

## 14. 待办（未验证，不编造）

- 7 店中其余 6 家（泽雅生活百货店、泽雅品质店、泽雅甄选店、泽雅优选店、腾祺品质店、腾祺优选店）venderId 尚未实时核验，接入前必须京麦 CLI 实查。
- Oxygen-Product-Pro / Oxygen-Imagen 在其他类目的通过率未验证，按本类目实测数据参考。
- 无货源模式在其他类目（非香薰/活性炭）的 Schema 推断规则需要小规模 MVP 后再固化。
