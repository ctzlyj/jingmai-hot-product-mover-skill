---
name: jd-jingmai-hot-product-mover
description: 将京东、淘宝或天猫商品链接及拼多多本地导出整理为可审核素材和标准 JSON/Excel，并通过京麦 jdo-cli 幂等发布、回查图片价格库存及自动上传主图视频。适用于 POP 商家跨来源搬运、修图后按原链接或商品表格重发、批量上架、失败恢复和新增来源/类目适配；不用于绕过登录、平台权限、版权授权或营销活动报名。
---

# 爆品自动搬运上架京麦

把“采集、审核、发品、视频绑定、发布后审计”视为一个可恢复事务，而不是一次盲目提交。

每次真实任务同时用于改进本个人版：记录各阶段耗时、重复调用、失败与恢复证据，优先减少重复取数、复用同店铺素材和采用已验证的批量流程。只沉淀已复现、根因确认且修复验证通过的改进；优化不能代替本批业务完成，也不能以省略商家原码、错店保护、精确定价、幂等或独立回查换速度。工具选择和授权内恢复由助手自主完成。

## 使用前

1. 仅处理用户明确授权使用的商品、图片、详情和视频。
2. 京东内部业务先读 O2 Skill，京麦发布及库存/视频维护走官方京麦 CLI；外部来源采集不强制经过 O2。涉及网页先读全局 `browser-task-router`，由助手选择现有适配器或工具，不要求用户选择供应商。具体分工见 `references/browser-source-routing.md`。
3. 阅读 `references/installation-and-configuration.md`，为每个店铺准备独立的非 `default` jdo-cli Profile 和 `config/store.local.json`，再刷新当前店铺、当前类目的实时参考文件。
4. 每次京麦写操作前调用店铺信息接口，命令 `--profile`、配置中的 `profile`、`venderId` 和 `shopName` 必须全部匹配；不匹配立即停止。
5. 新类目不得直接套用现代装饰画 Schema。先按 `references/adapting-new-category.md` 做小规模 MVP。
6. 淘宝来源先按 `references/taobao-source-adapter.md` 做只读采集和单商品 MVP；复用已核实的登录连接，只控制任务专属后台页。新采集优先用白名单字段提取器及 `--capture` 结构化交接，先核对商品ID、真实SKU、价格和详情，再进入草稿流程。禁止导航用户当前活动标签或复制登录资料。
   非装饰画可传入独立店铺、规则和实时参考文件；淘宝草稿必须附逐商品类目审核，不再默认当作装饰画。独立详情页按来源链接和商品ID绑定，整批复用一个已授权采集会话。

## 执行路由

- 用户按「京东爆款复刻模式」提供卖得好的京东链接并要求高级重制上架时，先读 `references/jd-hot-replica-handoff.md`（新对话直接执行的手册）和 `references/jd-hot-replica-mode.md`：采集后主图/详情页一律用 sufy-image2 + 统一动态提示词（按源链接清洗后长标题生成、去掉空格后的 SKU 规格尾段，`scripts/hot_replica_mode.py`）重制并直接替换原图；详情页衔接在生图前用 gemini-3.8-flash-official 源图预检注入（统一风格锚点 + 连续边界契约 + 邻居参考图，不降并发、不额外消耗生图额度）；定价按已确认规则（竞品到手价×97% 反推京东价）执行；图片落位与逐 SKU 规格图见 `references/sku-image-group-binding.md`。表格输入的每个源链接必须单独建稿上架：只做源 SKU/SPU 幂等恢复和目标类目白名单过滤，不得用 AI 同形态、销量代表款或疑似同款结论跳过；品牌必填类目（目标类目授权品牌列表不含 brandId=-1）在生图前跳过并汇总，不消耗生图。公共池和每个款式组的 sort1 都必须是卖点首图，去标注代表图只放款式组末位。
- 全店盘点、查重或发布后审计读取商品列表时，使用 `scripts/jd_product_pipeline.py:list_products` 的稳定排序和分页完整性检查；最终 SKU 必须来自逐商品详情。适用条件、失败回归和非破坏式更新见 `references/product-pagination.md`。
- 京东公开源视频优先原HTTP接口；确需浏览器补采时调用已安装路由器的 Playwright CLI，无界面独立会话按目标视频ID等待响应，不临时 `npx` 下载、固定休眠或借用工作标签。登录/验证拦截和清理结果不明不换工具盲试；见 `references/browser-source-routing.md`。

- 用户明确要求保留原链接、另发换图链接做A/B测试时，不能擅自改成原链接换图或按普通重复商品跳过。使用 `references/ab-image-testing.md` 的独立B事务；先按完整来源规格核对当前商品表的商家编码，再保留一致的A编码、实时京东价和规格。当前表与A冲突时停止该项，不能传播旧自动编码；该例外不能用于普通搬运。
- 用户明确授权纠正已有链接的商家编码时，先读 `references/merchant-code-repair.md`。当前表有值就原样采用；旧归档空码不能证明当前表空码，无法读取当前文件时不得猜码或重生成。保持原商品/SKU ID，缺码和同商品重码留异常。
- 用户明确改用商品文件夹表格的商家SKU和PDD源价补发时，使用 `references/repaired-workbook-republish.md`；缺少A链接不再排除，仍保留缺码、重码及内容问题的逐项失败清单，不能伪称严格一一配对A/B。
- 用户已要求“失败统一跳过、最后给清单”时，A/B批次启用 `--skip-failures`，商品级失败只留证并继续，不再逐条询问；店铺身份或鉴权异常仍停止全局写入。
- A/B与当前表补发的批量 `publish` 先逐件提交并立即保存回执，再统一独立回查；提交成功不算验收成功。单件结果不明先留为待核实、不重发，整批末按回执、申请和唯一技术货号核对。错店、鉴权失效、本地记录无法可靠保存或连续三次接口故障停止整批。中断后可用同一目录的 `verify` 只回查，不上传、不发品；详见 `references/deferred-batch-verification.md`。普通URL搬运流程不随此变更。

- 用户要评估或准备：只采集并生成本地审核件，不传 `--publish`。
- 用户明确要求正式上架：完成本地预检、京东合规校验后，必须要求显式确认字符串 `PUBLISH_TO_JD`。
- 旧普通 URL 搬运批次且用户未明确“表格逐链接单独上架”时：先做目标类目过滤和 AI 语义判重；非目标类目与 AI 判定的重复商品记录后静默跳过，其他失败才停止后续链接。京东爆款复刻表格输入按上文逐链接规则执行，不适用本条。
- 用户在看到判重对象和理由后明确要求保留相似款全部上架：把对应决策改为 `PROCEED`，记录 `userOverride`、原判定、匹配对象和时间；源 SPU/技术货号精确查重仍不可绕过，已有远端商品只能恢复或核验。
- 多商品批次按用户要求“全部提交后再检查”时，采用 `references/deferred-batch-verification.md` 的两阶段流程：第一阶段逐件保存 `SUBMITTING` 和创建回执但不等待传播，第二阶段统一回读；中断项先按唯一技术货号/申请单 reconcile，禁止重放。
- 用户已明确锁定本批目标类目时，不自动查询、推荐或适配其他京东类目。商品被京麦以“类目错挂”驳回后，保留 `productId`、申请状态和驳回原文，记录为本批非目标类目跳过项并继续后续商品；批次结束统一汇报。只有用户主动要求改换类目时，才进入新类目实时 Schema 和单商品 MVP 流程。
- 源商品存在视频：视频只上传一次，但同一个视频必须逐 SKU 建立关系；详见 `references/media-and-video.md`。
- 已有状态文件：从已完成阶段恢复；详情暂不可查时先查申请单状态，禁止把合规驳回误判为传播延迟，也禁止因详情传播或视频审核等待而重复创建商品。
- 淘宝来源：先生成 `taobao-mover/v1` manifest，复用现有图片、定价、发品、视频和审计链路；MVP 默认停在 `LOCAL_DRAFT`。
- 淘宝/天猫草稿的专利图、淘金币、活动价格说明和平台专属页直接删，近重复详情可经审核后删减；除此之外不做保真式排版/双图比对验收。
- 视觉检测仅保留**生图前详情页衔接预检**（`scripts/seam_preflight.py`，gemini-3.8-flash-official 源图预检，详见 `references/jd-hot-replica-mode.md`「详情页衔接」节）；生图/修图后不做品牌残留扫描、VLM 审图、质量门禁或逐张视觉验收，生成结果直接替换原图上传。爆款复刻生图统一使用 `scripts/hot_replica_mode.py` 生成的动态提示词 + `scripts/generate_with_continuity.py` 注入衔接计划（gpt-image-2.5 新链路）。返工触发条件：平台驳回上架（按驳回原因重制）、用户明确指出详情页断层（重制异类图单张）。`image_quality_gate.py` 仅保留文件技术校验辅助（编码/尺寸/路径绑定）。
- 淘宝真实 SKU 超过当前类目 `skuBindThresholdNum` 时，按完整款式组自动拆成最少且尽量均衡的多个商品；一次发布授权覆盖该次拆分，不逐件打断确认，全部结束后统一汇报各商品 ID、款式和 SKU 数。

## 旧普通搬运前判重门槛（不适用京东爆款复刻表格逐链接模式）

旧普通搬运批次在下载完整素材、生成发品申请或创建商品前，必须完成 `references/ai-duplicate-review.md`：

1. 先按用户指定的目标类目范围过滤；非目标类目写入 `SKIPPED_OUT_OF_SCOPE_CATEGORY`，继续处理下一条。
2. 完整读取店铺已有商品及同批更早候选，由 AI 结合标题、类目、核心含义、图案/款式、形态、材质、用途及必要的主图证据判断是否重复。
3. 禁止把标题标准化、关键词重合、哈希、编辑距离或字符串精确匹配直接当作重复结论；这些方法最多用于召回待 AI 审阅的候选。
4. AI 判定重复时写入 `SKIPPED_AI_DUPLICATE` 并继续，不要求用户逐条确认；整批结束后统一报告被跳过的源商品、匹配对象和判定理由。
5. 源 SPU/货号精确查重继续负责技术幂等；AI 语义判重是额外业务门槛，二者不能互相替代。

## 标准命令

```powershell
python scripts/jdo_listing_mvp.py refresh-reference `
  --profile your-store-profile `
  --category-id 35381 `
  --category-name "现代装饰画" `
  --output config/modern-decorative-painting.reference.json

python scripts/jd_product_pipeline.py run `
  --profile your-store-profile `
  --url "https://item.jd.com/源SKU.html"

python scripts/jd_product_pipeline.py run `
  --profile your-store-profile `
  --url "https://item.jd.com/源SKU.html" `
  --publish --confirm PUBLISH_TO_JD

python scripts/jd_product_pipeline.py batch `
  --profile your-store-profile `
  --input links.txt `
  --preflight-decisions preflight-decisions.json `
  --publish --confirm PUBLISH_TO_JD

python scripts/merchant_template_import.py `
  --input "装饰画搬运上品标准模板.xlsx" `
  --output-root "outputs/template-import"
```

`batch` 强制要求预检决策文件覆盖输入中的每个源 SKU；允许值只有 `PROCEED`、`SKIPPED_OUT_OF_SCOPE_CATEGORY`、`SKIPPED_AI_DUPLICATE`。缺一条即在采集和发布前整体停止，两个跳过状态写入 `state.json` 后继续下一条。

三表商家模板导入默认只生成 `LOCAL_DRAFT`，不登录、上传或发布；字段、推断和停止条件见 `references/merchant-template-import.md`。

## 不可变规则

- 图片上传会将超过64字符单位的远端素材名称改为稳定短名，不改本地文件、图片字节或SKU归属；见 `references/image-upload-names.md`。

- 源价逐 SKU 读取，任一缺价即停止；默认公式和校验见 `references/modern-decorative-painting.md`。来源适配器显式设置 `targetArrivalMultiplier` 时，目标到手价按 `源价 × 倍率 - 差额` 计算，未设置时倍率为 `1`。
- 用户明确要求直降金额、国补金额分别四舍五入到分时，必须切换到 `references/component-rounded-pricing.md` 的逐项取整口径；不能用默认连续乘折扣的结果证明严格到手价达标，也不能覆盖已精确达标的价格。
- 普通来源按倍率采用逐项取整时，在本批规则显式设置 `pricing.roundingMode="component"`；生成、预检和远端逐SKU回读贯通复算，倍率目标不再隐含减0.10元。未设置模式的旧流程保持原口径。
- 用户提供的详情图默认按顺序选入、疑点仅告警；整商品去重与详情删减按品牌信息口径经审核后执行。
- 淘宝标题、销售属性文案、主图、SKU 图、详情图和视频必须在发布前检查品牌信息、第三方平台标识、外部联系方式/二维码及其他店铺水印；宣称词与主观促销文案不做额外审计。修图只能处理违规素材，须保留原件与保留图顺序/SKU 归属；专利、价格说明、平台专属和无价值页以及近重复详情可经审核后删减，不用重复页补数；线上无法真正删图时停该商品。
- 主图校验必须读取图片实际编码，不能只信文件扩展名；扩展名为 `.jpg` 但内容为 WebP 时先转为 JPEG/PNG 合规副本再上传。包装长宽高、重量须同时校验实时 Schema 的最小值、最大值和精度约束。
- 自动拆分不得截断 SKU 或拆散同一款式的尺寸组；各子商品使用唯一技术货号和连续商家编码区间。首件上传成功后，后续子商品按文件哈希复用相同公共主图与详情图的已上传素材，禁止无意义重复上传。
- 现代装饰画固定使用多套主图；每款式首图不同，后续公共图相同。
- 保持源商品销售属性维度数量，不能从规格文字擅自新增“尺寸”维度。
- 单维度源商品的每个真实选项必须完整保留为唯一销售属性值；即使选项文字可拆出共同款式和尺寸，也只能将拆分结果用于内部商家编码。发布前按实际提交的维度投影检查组合唯一性，避免不同 SKU 在京东端折叠为同一销售属性组合。
- 商家模板已提供商家 SKU 时，必须逐字写入 `vendorSku` 并检查全工作簿唯一，不能再套用 GZH/ZTH 格式校验或被自动编码覆盖；仅在其他输入链路未提供商家 SKU 时自动生成。
- 视频上传地址首次请求不得发送 `breakPointFlag=false`；绑定体根节点必须是对象，每次只提交一个 SKU。
- 已有 SKU 绑定其他视频时停止，绝不自动覆盖。
- 已有商品整版编辑不得携带库存；库存修改只能走库存专用接口。
- 禁止使用 `default` Profile；所有 jdo-cli 调用必须绑定店铺专属 Profile，写操作前重新回查店铺身份。
- 线上商品图片补丁先 dry-run 再 apply；PATCH 提交后必须独立回读确认 `ALREADY_CURRENT`，写结果不明只 reconcile 不重放。主图 `WareSkuImg` PATCH 按槽位覆盖合并：提交比线上更少的 value 不会删除尾部图；删除主图的可靠做法是用同组干净图顶替该槽位并保持数量，真实删除能力未验证前不得假设生效。
- `publish_local_draft.py` 必须先校验 `PUBLISH_TO_JD`，再创建锁、上传素材或写状态；缺少确认时不得产生任何发布副作用。
- 商品详情返回“未查询到商品信息”时必须查询申请单详情；`draftComputeStatus=3` 立即写入 `PRODUCT_APPLY_REJECTED` 并停止，不能继续轮询或重新创建。
- 使用旧 `batch` 管线时，未完成目标类目过滤、店铺全量候选读取或 AI 语义判重时，不得创建商品；京东爆款复刻表格输入不适用 `SKIPPED_AI_DUPLICATE`，每个链接必须单独建稿上架（仅同源 SKU/SPU 幂等恢复除外）。
- 淘宝 SKU 只采用页面真实 `skuBase.skus`；逐项关联 `skuCore.sku2info` 当前有效价，禁止按属性笛卡尔积推测。单值 SKU 属性保留为固定属性，不额外扩成销售维度。

## 完成判据

只有以下证据全部满足才报告成功：商品详情可查、SKU 数量和组合一致、价格公式一致、商家编码唯一、库存专用接口一致、多套主图逐款式完整、详情图与保留页的数量/顺序一致；若有视频，还必须全部 SKU 的视频关系均为审核通过状态。

状态机、幂等恢复与停止条件见 `references/state-machine.md` 和 `references/troubleshooting-and-stop-conditions.md`。完整业务交接见 `references/handoff-manual.md`。
