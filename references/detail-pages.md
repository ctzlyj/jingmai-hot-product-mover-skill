# 详情页：方案、门槛、回读与补救（1.2.2）

适用于所有会**创建新商品**的路线。用户只说“上架”，不需要提醒详情页、也不需要写详情提示词。

## 为什么有这份规则

1.2.1 之前实拍上新只规划 3 个详情模块并拼成 1 张长图，`verify` 只检查详情 URL 存在；平台校验 0 违规就误报“上架完成”，详情页实际只有 1 张图。1.2.2 把详情页变成“先出方案、再生图、发布前强制门槛、发布后 PC/mobile 回读”的闭环。

## 一、detail-plan.json（自动生成）

`photo_listing.py make-plan` 依据**已确认**事实（标题、卖点、全部 SKU、尺寸重量、`facts.detail.*`）确定性生成，不调用模型、不需要用户输入。每个模块记录：`kind`、`purpose`、`factsUsed`、`skuKeys`、`photos`（仅用户实拍图）、`exactVisibleText`（画面里允许出现的文字）、`prompt`、`forbidden`、`outputFile`。

默认 6–8 个竖版模块，宽 790，模型出图 1024×1536 后等比缩放到 790，**每模块一张、按顺序**：

| kind | 职责 |
| --- | --- |
| hero | 首屏商品价值 |
| selling_points | 核心卖点（仅已确认卖点） |
| sku_options | 款式/组合/颜色/图案选择；每模块最多 4 个 SKU，**全部可售 SKU 必须被覆盖**（7 个组合 → 2 个模块） |
| scene | 真实使用场景 |
| structure | 实拍可见的结构细节，看不清的背面/配件不补画 |
| specs_package | 尺寸、重量、材质、包装清单——只排版已给出的文字 |
| aftercare | 注意事项与售后说明 |

可选事实字段（都只能来自用户）：`facts.detail.sceneText / structureNotes / careNotes / productSizeText / material / packageContents`。

### 从生图站（ctzlyj/image-studio）吸收的做法

- 每段有独立的**职责与构图契约**，共享配色/光线/材质锚点，但不重复首屏标题卡。
- 所有可见文字由模型直接绘制，本地**不叠字**；提示词把“画面文字（逐字）”与“仅供理解的事实”分开，并要求 8% 安全边距。
- 事实与证据门禁：数字、参数、认证、功效只能来自已确认事实；画面文字出现事实之外的数字或促销词，方案阶段就失败。
- 本地只做尺寸校验、等比缩放与总览预览，不画文字。
- 未移植：其文案模型与认证占位图；本 Skill 生图仅限公司网关三款 GPT 模型。

## 二、禁止内容

价格、促销、折扣、秒杀、榜单、销量排名、优惠券、来源店铺名、他牌名称与标识、水印、二维码、未经证实的认证/功效/材质/尺寸/承重。参考链接的风格摘要若含这些内容（或 `reference.bannedTerms` 里登记的来源店铺/他牌词）会被拒绝，请改写成纯版式、配色、卖点结构描述。参考原图只在 `.reference-cache` 做风格分析：不作为生成输入；其 sha256 与任何待上传详情图相同则拒绝；路径位于缓存目录则拒绝。

## 三、发布前强制门槛 `detail_pages.check_detail_set`

在 `accept-images`、`prepare`（上传前）、`build_listing` 三处强制：

1. 详情图 ≥ 6 张；与 `detail-plan.json` 的模块 id 与顺序一致。
2. 必需模块齐全（首屏、卖点、款式、场景、结构、参数包装、售后）。
3. 全部 SKU 被 `sku_options` 模块覆盖。
4. 宽度只能 750 或 790；必须竖版；文件可读；sha256 与记录一致。
5. 无重复（字节相同或感知哈希近似）；无参考原图。
6. 画面文字抽检：无促销/价格/他牌词，数字必须在已确认事实里。

任一不过：**禁止发布**，先补生成（`make-plan --only detail`、`generate`，只重做详情，不动已有主图/SKU 图），不是只给警告。

例外：用户明确“只要一张/少量”或平台/类目限制，在 `facts.detailPolicy={"type":"user-single-image"|"platform-limit","reason":"…","minimum":N}` 写明原因；确认清单显示该原因。

其他路线（链接搬运/复刻/模板导入/PDD）通过 `jdo_listing_mvp.validate_manifest` 同样拦截 <6 张详情图（`product.detailPolicy` + `product.detailExemptionReason` 可例外）。同款矩阵换主图沿用源商品详情 HTML：源详情 <6 张或 PC/mobile 不一致时 `build` 停止，先用 `detail-repair` 补源；确有原因用环境变量 `MATRIX_DETAIL_EXEMPTION_REASON`。旧爆款复刻的定稿提示词与逐张重制逻辑未改动。

## 四、确认清单

`review` 生成 `确认清单.md` 与 `preview.html`，包含：详情总图数、模块列表（id｜职责｜覆盖 SKU）、门槛结论、`详情图/详情页总览.jpg`（仅预览，不上传）。用户在预览里可要求重做某一张：`generate --redo detail-03-skuoptions`。

## 五、发布与回读

提交体的 `WareEditor` 同时写入 PC（`userAgent=pc`）和手机端（`userAgent=mobile`），两端同一批图、同顺序。`verify` 读 `productDetailDesc.desc` 与 `mobileDesc`，逐一校验数量与 URL 顺序，另重跑本地门槛。结果写入 `manifest.deliveryChecks`：

- `platformValidationPassed` 平台校验无强违规
- `productOnSale` 商品与全部 SKU 在架/启用
- `detailQualityPassed` 详情数量/顺序（PC+mobile）及本地门槛
- `videoBound` 主图视频已绑定并审核通过（来自 `manifest.video.binding`）

`statement` 字段是唯一允许对外使用的完成措辞：任一前三项失败 → “上架未完成：… 未通过”；前三项通过但视频未绑 → “上架完成（主图视频尚未绑定，不能称全部完成）”；四项都通过才是“全部完成”。`manifest.detailPlan` 记录模块数、每模块路径/SKU/线上 URL 与回读结果，供视频和信息分 Skill 只读。

## 六、detail-repair（已上架但详情不完整）

```text
python scripts/detail_repair.py plan     --package <listing-package>
python scripts/detail_repair.py generate --package <listing-package>      # 公司三款 GPT 模型并发出图
python scripts/detail_repair.py review   --package <listing-package>      # 出预览与摘要，给用户看
python scripts/detail_repair.py apply    --package <listing-package> --confirm DETAIL_PATCH --review-digest <摘要>
python scripts/detail_repair.py reconcile --package <listing-package>     # 结果不明时只回查
```

- 只读 listing-package（需有 `productId`、店铺 Profile/venderId/店名，写前复核）；只重做详情，复用事实与全部 SKU。
- 写入体只含 `WareEditor`（PC + mobile）一个 patch 项：不含价格、库存、SKU、主图、视频字段；图片上传台账复用 `.publish/uploads.json`，同一内容不重复上传。
- 防重复：写前记 `SUBMITTING`，异常记 `UNKNOWN`；之后任何 `apply/reconcile` 只回读远端，已更新则标成功，未更新也**不重发**，要求人工核对；摘要变化（用户改了图）才允许新一轮。
- 回读不过（PC/mobile 数量或顺序不一致）→ `detailQualityPassed=false`，不得称完成。
