# TOP销量SKU聚合草稿（只保存草稿）

## 模式定位

把店铺指定类目近 30 天销量 TOP N 的 SPU（各取最高销量 SKU）聚合成一条新链接草稿：

- 单一「颜色」销售维度，规格值写完整文案；
- 每个 SKU 多图（源商品公共图组 + 参照 SKU 首图，至少 5 张）；
- 价格严格等于源 SKU，不做任何加价、折扣或取整；
- 每个 SKU 独立详情（默认生成逐 SKU HTML + 映射表 + 京麦客户端人工步骤）；
- 只保存草稿（`saveActionType=0`），回读 `productStatus=105`，绝不上架。

触发词：**聚合上新、整合链接、类目TOP SKU聚合、组合链接草稿**。

本模式是 `jd-jingmai-hot-product-mover` 的一个执行模式，不是第 13 项业务能力，也不修改总入口 registry。

## 与现有能力的关系

- 复用 `scripts/jdo_listing_mvp.py` 的 `configure_jdo_profile`、`mark_jdo_store_verified`、`get`、`run_jdo_large`，不复制新的 jdo 桥。
- 复用 `scripts/win_no_window.py` 抑制子进程控制台窗口。
- SKU 维度详情边界沿用 `references/jd-sku-detail-mode.md`：仅京麦客户端人工设置。
- 源详情中的第三方平台标识、联系方式、二维码、水印、版权/授权风险按 mover 现有审核规则处理。

## 前置条件

1. 店铺已授权，使用店铺专属 jdo-cli Profile；禁止 `default`。
2. 用户已明确 TOP N、规格命名、标题、库存、是否只保存草稿。
3. 目标类目存在「颜色」销售属性维度；否则无法挂 SKU 图。
4. 所有源 SPU 的 `brandId`、`categoryId`、关键类目属性 `goodsAttrInfos` 一致；不一致必须停止并让用户确认。
5. 源商品公共主图至少 5 张；不足时停止，不补图、不猜图。

## 六步流程

### 第 0 步：task_config.json

最小骨架：

```json
{
  "venderId": 12345678,
  "shopName": "店铺名",
  "jdoProfile": "pop-shop-xxx",
  "categoryId": 53181,
  "topN": 3,
  "title": "聚合链接标题",
  "stock": 100,
  "draftOnly": true,
  "topSkus": [
    {"n": 1, "pid": "100000000001", "skuId": "10000000000101", "price": "19.90", "spec": "黑色20支"},
    {"n": 2, "pid": "100000000002", "skuId": "10000000000201", "price": "29.90", "spec": "白色30支"}
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| venderId | 是 | 店铺 ID，采集/提交前与 Profile 身份交叉校验 |
| shopName | 是 | 店铺名称，与 Profile 身份交叉校验 |
| jdoProfile | 是 | 店铺专属 Profile，禁止 default |
| categoryId | 是 | 三级叶子类目 ID |
| topN | 否 | 聚合 SPU 数，默认 5；必须与 topSkus 数量一致 |
| title | 是 | 新链接标题，需覆盖各源商品核心词 |
| stock | 否 | 每 SKU 库存，默认 100 |
| draftOnly | 否 | 必须为 true；本模式不提供上架路径 |
| topSkus | 是 | 数组：n、pid、skuId、price、spec |
| sourceAppliesPath | 否 | 源商品 apply 详情文件，默认 `source_applies.json` |
| consistencyOverride | 否 | 一致性不一致时的显式确认：`{"confirmed": true, "reason": "..."}` |
| review.detailAccepted | 否 | 源详情命中第三方标记时的显式确认 |

### 第 1 步：collect（只读）

```powershell
python -X utf8 scripts/top_sku_aggregation.py collect --config task_config.json
```

- 盘点全店在售商品，拉商品详情，按 `categoryId` 过滤；
- 扫近 30 天订单，只申请 `skuItemInfoList`，不碰收货人信息；
- **SPU 排名按该 SPU 全部 SKU 的 `orderQty30d` 汇总**，不是商品列表的 `salesVolume`；
- 每个 SPU 取 `orderQty30d` 最高的 SKU；
- 输出 `category_products.json`、`product_details.json`、`sku_sales_top.json`、`source_applies.json`、`top_skus.json`；
- 断点续跑：已拉取的商品详情会跳过；可用 `--from-sales-file sku_sales_top.json` 复用销量结果。

`sku_sales_top.json` 会记录 `rankingBasis=orderQty30d`，并注明 `salesVolume` 仅作参考。

### 第 2 步：确认规格命名并补全配置

把 `top_skus.json` 的结果整理进 `task_config.json` 的 `topSkus`：

- `spec` 写完整文案（如「无痕衣架黑色20支」），不要拆成颜色+尺码两维；
- 规格值不能包含 `[`、`]`、逗号或换行；
- 同一批规格值、参照 SKU 不得重复；
- 标题需覆盖各源商品核心词。

### 第 3 步：build（只写本地）

```powershell
python -X utf8 scripts/top_sku_aggregation.py build --config task_config.json
```

构建前强制检查：

- 所有源 SPU 的 brandId、categoryId、关键类目属性一致；
- 每个参照 SKU 属于配置的源商品；
- 每个参照 SKU 有「颜色」销售属性；
- 价格严格等于源 SKU（按分比较）；
- 每个 SKU 图组至少 5 张；
- 源商品详情非空。

输出：

- `publish_body.json`（`saveActionType=0`）
- `validation_body.json`
- `schema_debug.json`
- `mapping.json`
- `aggregate_manifest.json`
- `新链接对照表.csv`
- `sku{N}_detail_pc_{pid}.html` / `sku{N}_detail_mobile_{pid}.html`
- `sku_detail_mapping.json`
- `sku-detail-manual.md`
- `detail_review.json`
- `consistency_report.json`
- `确认清单.md`（含 checklistHash）

### 第 4 步：submit（只建草稿）

```powershell
python -X utf8 scripts/top_sku_aggregation.py submit --config task_config.json --confirm <checklistHash 前8位>
```

规则：

- 必须先核对 `确认清单.md`，并用其中的 `checklistHash` 前 8 位确认；
- `saveActionType` 必须为 0；
- 已有 `applyId` 或状态为 `CREATED` 时停止；
- 状态为 `SUBMITTING`/`UNKNOWN` 时先调用同一回查逻辑，不直接死锁；
- 回查确认已创建则写 `CREATED` 并停止；确认未创建才允许 `--retry`；
- 提交前按 `topSkus` 的 `pid`/`skuId` 重新读取源 SKU 当前价，与配置价按分比较；不一致停止，提示重新 build/确认，不自动改价、不自动提交；
- 提交前先写 `submit_state.json` 的 `SUBMITTING`，再发创建请求；
- 超时/异常/未取到 applyId 写 `UNKNOWN`，先按 applyId 或标题+时间回查；
- 禁止换工具、换目录重投。

只读回查命令：

```powershell
python -X utf8 scripts/top_sku_aggregation.py reconcile --config task_config.json
```

- 找到且时间可确认：写 `CREATED` + `applyId`，退出码 0；
- 未找到：保持原状态，退出码 1；
- 标题命中但时间不可确认：保持原状态，退出码 2，需人工核对。

`submit_state.json` 状态：

```text
PREPARED -> SUBMITTING -> CREATED
                      \-> UNKNOWN -> (回查) CREATED | PREPARED(--retry)
```

### 第 5 步：SKU 维度详情（人工）

本模式**不包含网页自动化**。`build` 已生成逐 SKU 详情 HTML、映射表和客户端人工步骤：

1. 在京麦客户端打开目标商品编辑页；
2. 进入详情编辑器，切换到「详情随 SKU 切换」；
3. 按 `sku_detail_mapping.json` 逐个选择对应详情；
4. 保存后运行 `verify` 回读；
5. 只有 `skuList[].productDetailDesc.desc` 全部非空，才视为 SKU 详情完成。

交付时必须记录：

- `detailMode=SPU`
- `skuDetailManualPending=true`
- `skuDetailPendingKeys`（待设置 SKU 列表）
- 客户端人工步骤

### 第 6 步：verify（只读）

```powershell
python -X utf8 scripts/top_sku_aggregation.py verify --config task_config.json
```

四项验收：

1. 每个 SKU 有价且等于源价；
2. 销售属性只有「颜色」；
3. 每个图组至少 5 张；
4. 每个 SKU 的 `productDetailDesc.desc` 非空（人工完成后）。

另加草稿状态检查：`productStatus=105`；`103` 表示已在售，必须报警。

商品状态查询优先使用 apply 详情里的 `productInfo.productId`，只有 productId 缺失时才回退 applyId；查不到任何商品行时写入明确 problem，不静默判失败。

SKU 数量校验：`len(skus) == len(expected)`，并列出缺失/多出的规格值；缺失或多出都写入 problems，`verify` 返回非 0。

退出码：

- `0`：四项全部通过；
- `3`：草稿与三项通过，但 SKU 详情仍待人工；
- `2`：存在其他问题。

## 六个冲突的收口

### 1. SKU 维度详情

- 默认按 `references/jd-sku-detail-mode.md` 的硬边界：仅京麦客户端人工设置。
- 不探测 `descModel`、灰度 key、接口参数或 JS chunk。
- 不包含 Playwright/CDP/网页自动化脚本。
- 交付 `skuDetailManualPending=true`，人工完成后回读 `skuList[].productDetailDesc.desc`。
- 只有在用户明确授权并指定测试店/类目/TOP N 后，才可做一次受控网页路径验证；该验证是一次性实验，不进入本 Skill 的默认模式，且必须回读 `skuList[].productDetailDesc.desc`，不能只看页面保存成功。

### 2. 提交幂等

- `submit_state.json` 记录 `PREPARED/SUBMITTING/CREATED/UNKNOWN`。
- 提交前若已有 `applyId` 则停止。
- 状态为 `SUBMITTING`/`UNKNOWN` 时先运行 `reconcile` 只读回查；未找到才允许 `--retry`。
- 提交前按 `topSkus` 重读源 SKU 当前价；不一致停止并要求重新 build。
- 禁止换工具、换目录重投。
- 提交前必须先写 `SUBMITTING`，再发创建请求。

### 3. TOP N 口径

- SPU 排名按该 SPU 全部 SKU 的 `orderQty30d` 汇总，不按商品列表 `salesVolume`。
- `salesVolume` 只作为参考字段记录，不参与 TOP N 排名。
- `sku_sales_top.json` 显式记录 `rankingBasis=orderQty30d`。

### 4. 业务一致性

- 聚合前断言所有源 SPU 的 `brandId`、`categoryId`、关键类目属性一致。
- 不一致时停止，并输出 `consistency_report.json`。
- 用户确认后可在 `task_config.json` 写 `consistencyOverride`，确认清单会显示覆盖原因。
- 尺寸/重量/运费模板/承诺取 TOP1 的 SPU 值，必须在确认清单中明示。
- 源详情中的第三方平台标识、联系方式、二维码、水印、版权/授权风险按 mover 现有审核规则处理；命中时 `submit` 会阻止，直到 `review.detailAccepted=true`。

### 5. 浏览器 Profile 锁

- 不按 `*jd-material-pop*` 过滤并结束 chrome 进程。
- 不按 PID 强杀、不删除别人的锁文件。
- 用 `profile-check` 检测 `SingletonLock`/`SingletonCookie`/`SingletonSocket`/`lockfile` 和占用该 Profile 的 chrome 进程。
- 检测到占用时停止，让用户关闭对应浏览器窗口后重试。

### 6. 打包/测试

- `SKILL.md` 保留 `version` 元数据。
- 补本参考文档和 README 入口。
- 补假数据测试：配置校验、payload 构建、价格断言、单颜色维度、图片≥5、逐 SKU 详情映射、提交状态机、无网络。
- 运行包不包含 `tests/`、`docs/`、`.work/`、`__pycache__`、`outputs/`。
- 用现有 `stage_mover.py` 生成候选包，并做文件数/哈希/敏感路径校验。

## 产物清单

| 文件 | 说明 |
|---|---|
| `aggregate_manifest.json` | 贯穿 build/submit/verify 的清单；含 checklistHash、SKU 映射、详情待办 |
| `新链接对照表.csv` | 序号/参照SKU/源商品ID/源价/新价/规格/图片数/详情字符数 |
| `sku{N}_detail_pc_{pid}.html` | 逐 SKU PC 详情 |
| `sku{N}_detail_mobile_{pid}.html` | 逐 SKU 手机详情 |
| `sku_detail_mapping.json` | 规格 → 源商品/SKU → 详情文件 |
| `sku-detail-manual.md` | 京麦客户端人工步骤 |
| `确认清单.md` | 提交前确认项与 checklistHash |
| `submit_state.json` | 提交状态机 |
| `verify_report.json` | 回读验收结果 |
| `browser_evidence/` | 仅人工/受控验证时保存证据，默认不生成 |

## 完成判据

只有以下证据全部满足才报告完成：

- 草稿可查，`productStatus=105`；
- 每个 SKU 有价且等于源价；
- 销售属性只有「颜色」；
- 每个图组至少 5 张；
- 每个 SKU 的 `productDetailDesc.desc` 非空（人工完成后）；
- `submit_state.json` 为 `CREATED`，且有 applyId。

SKU 详情未完成时，只能报告「草稿已创建，SKU 详情待京麦客户端人工设置」，不能报告「全部完成」。

## 边界

- 不登录、不写店铺、不付费、不发布，除非用户当轮明确授权。
- 不读取/输出 Cookie、Token、密码、完整页面 HTML。
- 订单扫描只用 `skuItemInfoList`，不碰收货人信息。
- 不覆盖本地 `jingme-assistant 1.0.3`、PSD 11530、视频 11267 等受保护对象。
- 不发布 SkillHub，不改总入口 registry，不碰商家版/其他业务 Skill。