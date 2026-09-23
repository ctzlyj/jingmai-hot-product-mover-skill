# 淘宝来源适配

## 路由

淘宝链接只新增来源采集适配器，继续复用现有 `manifest/listing`、图片上传、逐 SKU 定价、视频绑定、状态机和发布后审计。新来源先完成一个商品的 `LOCAL_DRAFT` MVP，不直接批量。

## 浏览器安全

- 个人版默认沿用原来成功的日常 Chrome + WebCLI Browser Bridge；见 `browser-source-routing.md`。不能把离线测试或专用 Playwright 页面接收函数当作自动切换业务登录窗口的授权。
- 先读全局 `browser-task-router`，由助手复用已核实账号的后台连接；不要求用户在多个浏览器工具之间做技术选择。新工具的空白独立会话不等于已登录淘宝。
- `open` 必须新建独立标签并锁定返回的 page ID；禁止把用户活动标签作为导航目标。
- 后续 `eval/click/wait` 必须携带该 page ID。新结果页按目标 URL 精确定位，不依赖 `active:true`。
- 不复制 Cookie，不关闭用户标签；安全验证、登录失效或目标 page 消失时保存状态并停止。

## 数据模型

1. 从页面服务端状态读取 `skuBase`、`skuCore`、商品、店铺、主图、款式图和视频。
2. 真实 SKU 集合只认 `skuBase.skus`。每项按 `skuId` 关联 `skuCore.sku2info`，缺价、重复或数量不一致立即停止。
3. 当前有效源价优先取 `subPrice.priceMoney`；不存在时取 `price.priceMoney`，同时保留优惠前价供审计。
4. 只有多个取值、实际改变 SKU 组合的属性进入 `saleProperties`；单值“组合形式”“外框类型”等写入 `sourceFixedSkuProperties`，不得制造额外维度。
5. 款式图按颜色属性值读取；详情图读取 `data-name=singleImage` 的 `data-src`，全部保留并保持 DOM 顺序。
   重复出现的详情图也保留其位置与数量，不能按URL去重：真实页面曾有27个详情节点，被旧解析器去重成26张。`test_repeated_detail_images_preserve_source_order_and_count` 覆盖重复图与协议相对URL的回归。
6. 主图视频记录源视频 ID、下载 URL 和封面；下载后从 MP4 元数据确认时长，仍按“上传一次、逐 SKU 绑定”发布。

## 当前实现

项目脚本 `scripts/taobao_product_mvp.py` 接收已捕获的页面 bootstrap 与详情 DOM JSON，生成 `manifest.json`、`listing.json`、`pricing-verification.json`、`duplicate-review.json`、SKU 审核 Excel 和 `state.json`（`LOCAL_DRAFT`，无 `productId`）。

新采集优先使用 `scripts/browser_collectors/taobao_capture.js`，在任务专属商品页通过所选工具只读执行该函数，保存 `taobao-browser-capture/v1` JSON；不要保存整个页面上下文、Cookie、Token或账号资料。提取器只导出商品、店铺标识、真实SKU及价格、图片和视频字段。

`--capture <采集JSON>` 替代 `--bootstrap` 与 `--detail-dom`；后两项旧输入仍兼容，但不能混用不同页面数据。解析器在访问目标店铺前核对输入链接、页面和数据里的商品ID一致，拒绝空SKU、重复SKU、缺价、空详情及计数不一致。真实SKU仍只来自 `skuBase.skus`，不会按属性笛卡尔积补齐。草稿记录采集文件SHA-256，便于后续核对来源。

提取器默认详情范围为 `current-document`。主页面没有详情节点、但商品状态提供 `item.pcADescUrl` 时，返回 `DESCRIPTION_PAGE_REQUIRED` 及白名单化链接，而不是把空详情当成功。在任务专属详情页等待 `[data-name="singleImage"]` 节点加载后，向提取器传入 `{ok:true,pageUrl,images}`。只接受来源明确提供、商品ID一致的淘宝详情页，结果记录 `linked-description-page`、`detailPageUrl`，Python侧再次核对来源。

Playwright可复用 `scripts/browser_collectors/taobao_read.mjs` 的 `readTaobaoProduct(ownedPage, sourceUrl)`。调用者提供任务专属页并在整批复用同一个已授权浏览器会话；助手不应为每件商品重新启动或切换登录上下文。该函数不登录、不复制Cookie、不启动浏览器，仅关闭自己创建的详情页。以字符串执行 `taobao_capture.js` 时必须实际调用函数：`page.evaluate('(' + collector + ')()')`，不能只求值成函数对象。

数量匹配不证明懒加载、折叠或iframe外还有内容时已全采。助手必须观察目标详情区域并核对顺序和完整性；等待DOM完成不等于等待详情节点完成。登录页、安全验证或详情未就绪时保留未完成状态，不下载空素材、更不发布；需要其他适配器时仍交付同一结构，不拿空白浏览器强行冒充业务登录。具体分工见 `browser-source-routing.md`。

## 非装饰画输入

真实样本复现了旧解析器只接受“颜色分类+尺寸”、遇到单个“颜色”维度失败，以及把所有淘宝商品写成装饰画类目的问题。解析器现在保留真实属性路径、单值固定属性、源价种类、可用库存和源商家编码，不再伪造来源类目。采集解析支持零个、一个或多个属性；不意味着目标发布模型已支持全部结构，超过两个销售维度仍明确停止而非合并丢失。

生成草稿时可显式传入 `--store-config`、`--rules`、`--reference`，并必须提供 `--category-review`。审核JSON包含 `sourceItemId`、`targetCategoryId`、`decision:"PROCEED"` 和有证据的 `reason`，绑定当前商品并与规则、实时模板一致。来源类目未知时保留空值，不拿目标类目冒充来源事实。旧淘宝调用也需补这份审核，不再自动映射为装饰画。

仅装饰画规则沿用原有装裱/风格适配；其他类目保留其独立规则。包装长宽高、重量和型号可由逐商品规则的 `productDefaults` 传入，但必须有真实资料支撑；缺少必填、品牌授权、3C或运费信息时不得编造。仍需新类目样本发布及独立回查，单元测试和模拟浏览器通过不能替代线上验收。

回归验证：`python -B -m unittest discover -s tests -q`；`python -B tests/browser_collector_smoke.py -q` 包括独立详情页延迟加载、原文档兼容、实际函数调用和任务页清理。单元测试另覆盖错误商品/来源链接、私密字段排除、真实单维SKU与类目审核绑定。

正式上传素材、京东合规校验或发布前，继续执行店铺白名单核验、AI 语义判重、内容合规预检和现有发布确认门槛。

## 发布前内容合规门槛

用户明确要求直接使用接口返回价、无需追查优惠后价时，仅在该批规则设置 `acceptPrePromotionReturnedPrice=true`，并作为 `taobao_capture.js` 的第二个参数传入。该模式保留逐真实 SKU 返回价，输出 `sourcePriceBasis=taobao-returned-price`；不因“优惠前”或价格区登录文案停住，也不额外切换规格追查优惠。默认模式不变；实际登录页、安全验证、缺价、商品身份和素材完整性仍照常检查。该选择不扩展店铺或发布授权，不代表已经验证结算价。

“登录查看更多优惠”价格文案不证明账号未登录：真实页面曾同时显示已登录账号，账号接口返回成功，但仍保留该文案。采集器仅返回 `EFFECTIVE_PRICE_UNVERIFIED`，将实际账号登录状态与逐 SKU 到手价证据分开核实；保留上下文，不因此要求重复登录或触发登录冷却。只有实际登录页、验证拦截或账号接口的明确登录失败才进入相应恢复流程。优惠前价格仍不得直接冒充到手价。

已登录页面的初始化 `skuCore` 仍可能只有优惠前价。真实样本通过在同一任务页选中真实 SKU，读取该 SKU、数量 1 对应的实时优惠结果 `skuCouponInfoWithQuantity[skuId + "_quantity1"]`，确认 `subPrice` 为店铺优惠后价；页面展示原价及“官方立减”时，不能忽略这份优惠结果。交接只保留商品/SKU ID、价额、优惠说明和时间，不导出整个组件状态；要求当前商品、选中规格和结果键一致，不能把上一规格的优惠说明或空缓存当成本规格价格证据。完整逐 SKU 证据收齐前不生成可发布价格。

规格切换会触发独立的 `mtop.taobao.pcdetail.data.adjust` 请求；真实 `RGV587_ERROR` 即安全验证信号，哪怕主页面和账号仍可见，也立即停止后续规格/商品请求。保留已经核实的规格与原始材料，按用户约定冷却后最多进行一次正常重连；不能换账号、改风控字段或把旧价填入缺失规格。

来源详情链接的 `f=desc/...` 被 `URLSearchParams` 改写成 `f=desc%2F...` 时，旧详情页查询解析器会把编码文本原样传给接口，造成空白详情及 `FAIL_SYS_SERVICE_INNER_FAULT`。仅对已校验的 `desc/` 文件标识保留路径斜杠，其他参数继续白名单清洗；不能套用为任意 URL 全量解码。回归见 `tests/test_taobao_collector_guards.py`；真实修复以同商品详情图片恢复为依据，不能把其他空详情错误一概归因于编码。

1. 在创建商品前检查标题、销售属性文案、主图、SKU 图、全部详情图和视频关键帧；不能只依赖发布申请提交后的平台审核。
2. 至少检查第三方平台名称、标识和营销资产，外部联系方式或二维码，其他店铺/卖家水印，以及与商品不符或平台明确禁止的宣传内容。
3. 仅处理能够明确识别且可安全移除的违规内容。保留原素材归档，生成单独的合规副本，并重新检查修复区域；不得覆盖原文件。
4. 详情图仍全部保留并保持原顺序。违规图片使用合规副本原位替换，不得通过删图规避检查；主图、SKU 图和视频也必须保持原有款式归属与真实商品信息。
5. 本地预检通过后仍要调用京东合规校验。任一明确违规未修复、修复会改变商品真实性、或无法可靠确认修复结果时，停止发布并记录原因。

## SKU 超限自动拆分

- 读取实时类目 Schema 的 `skuBindThresholdNum`；真实 SKU 超限时自动拆成最少数量的子商品，并尽量均衡每件商品的款式数。
- 拆分边界只能位于款式之间，同一款式的全部真实尺寸必须留在同一子商品，禁止截断 SKU、删除尺寸或制造不存在的组合。
- 子商品使用 `源商品ID-P01`、`源商品ID-P02` 等唯一技术货号，标题增加 A组、B组等可辨识前缀，商家编码区间按子商品顺序连续分配。
- 用户已授权本次商品发布后，拆分子商品继承同一次发布授权，不逐件要求确认；任一子商品失败时停止后续未发布部分，保留已发布商品 ID 和所有状态。
- 第一件上传后，后续子商品对公共主图和详情图按文件哈希复用已上传素材，只上传各自独有款式图。全部子商品完成后统一汇报拆分原因、每件商品 ID、款式数、SKU 数、合规修复和审计结果。

## 停止条件

- 页面中不存在 `skuBase`/`skuCore`，或真实 SKU 无法一一关联价格。
- 可变销售属性不是已适配的款式+尺寸结构。
- 详情图顺序、款式图归属或视频文件无法确认。
- 内容合规预检未完成，存在尚未修复的明确违规，或修复会改变商品真实性。
- 标签页无法锁定到新建 page ID，或任何命令将指向用户原活动标签。
- 本地预检、逐 SKU 定价复算、图片或视频校验失败。
