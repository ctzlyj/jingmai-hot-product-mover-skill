# 京东爆款复刻模式交接手册（2026-09-28 首链路全通版）

本手册面向新对话直接执行京东爆款复刻上架，全部规则来自泡澡桶批次（源 10233331213967 → 千百日用品质店店 10036194410506）的真实全链路验证。开工前先读 `SKILL.md` 和 `references/jd-hot-replica-mode.md`；图片落位细节在 `references/sku-image-group-binding.md`。

## 固定环境（本机）

- Skill 仓库：`C:\Users\caotong.888\.joycode\skills\jd-jingmai-hot-product-mover`（GitHub：`ctzlyj/jingmai-hot-product-mover-skill`）
- Python：仓库内 `.venv\Scripts\python.exe`；CLI 入口 `scripts\jdo_listing_mvp.py`
- 任务局部脚本目录：`D:\caotong.888\Documents\ChatGPT\搬运上架skill\scripts\<品类-日期>\`（草稿/探针/价格表都放这里，不进 Skill）
- 生图 Key：`%LOCALAPPDATA%\JoyCode\credentials\sufy-image2.dpapi`（DPAPI，不进任何源码/日志）
- 店铺身份：`config\store.local.json` 白名单 + 店铺专属 store-config（如 `qianbai-bath-tub.store.json`）；每次写操作前 CLI 自动三要素核对（店铺名+venderId+Profile 一致），禁止 default Profile。换新店铺执行时，先按 `references/installation-and-configuration.md` 为该店建立独立 Profile、store-config 和目标类目 reference，不得复用其他店铺的配置文件。

## 端到端流程（顺序执行）

### 1. 采集建稿

- 输入：用户给京东爆款链接 + 目标店铺。表格批量输入时**每个源链接单独建稿上架**，不做 AI 语义同款/销量代表款跳过；仅同源 SKU/SPU 幂等恢复可以复用既有商品。
- **先做目标类目白名单与品牌必填过滤**：不在白名单的链接直接写失败清单；品牌必填判定用 `scripts/hot_replica_mode.py` 的 `brand_gate(reference)`——**目标类目授权品牌列表（reference `brands`）不含 `brandId=-1`（无品牌）即必填**，直接记 `SKIPPED_BRAND_REQUIRED` 并汇总类目，**不采集完整素材、不生图、不上传、不发布**，严禁改填其他品牌绕过（2026-09-30 鑫康洁教训），等用户申请无品牌白名单后恢复。
- 过滤通过后，用 `jd_product_mover.py`/`jd_product_pipeline.py` 采集：完整 SKU（含逐 SKU `sourcePrice`）、主图、逐 SKU 尺寸标注图、详情长图、主图视频。
- 生成草稿目录 `outputs/migrations/<源ID>_<标题>/`：`listing.json`（sourcePrice 必须逐 SKU 落库）、`SKU图/`、`主图/`、`详情图/`、`视频/`。

### 2. listing 清理（搬运模式规则）

- 新标题：卖点前置、无品牌词/进口国/第三方平台词/极限词，长度按类目限制。
- 类目属性按详情图证据改（如材质）。
- 详情图直接删：品牌宣传/榜单销量无证据宣称/通用服务条/无商品信息页；其余保留并重新编号。
- 尺寸别名冗余词清理（如"只有蓝色"）。
- 定价基准先问用户（默认规则见第 5 步，用户给了表格就直接执行）。

### 3. 图片统一动态提示词重制（sufy-image2 gpt-image-2.5 新链路）

- **生图前先做详情页衔接预检**：源详情图序列拼 1 图，gemini-3.8-flash-official 单次提问（约 25 秒，零生图额度），输出 `continuity-plan-<sku>.json`（统一风格规范 + 逐页边缘契约 + 连续边界）；失败退回普通模式不阻塞。
- 单链接正式命令（2026-10-08 转正，替代旧任务局部 62 号脚本）：`python scripts/hot_replica_preflight.py --sku <源SKU> --reference config/<店铺>-<类目>.reference.json`——品牌闸门（不含 -1 直接 `SKIPPED_BRAND_REQUIRED` 返回，不耗生图额度）+ gemini 双轮剔页（交集）+ 衔接计划 + 动态提示词重制 plan 一步完成；产物默认写 `<草稿>/hot-replica/`，可断点恢复。
- **所有图片（主图、详情图、SKU 规格图、款式代表图）用同一条动态提示词**，逐张以对应原图为参考走 `edit`，不逐张设计提示词。模板（用 `scripts/hot_replica_mode.py` 的 `build_dynamic_prompt(title)` 生成，禁止手抄）：
  > 你是京东电商团队的资深修图师，正在对这张【输入商品长标题】电商图做合规清理与整体提质，禁止重新设计。严格遵守以下三层规则，规则冲突时层级靠前者优先：【第一层·商品本体保护，最高优先级】商品的实物主体必须逐像素原样保留：形状、颜色、材质、结构、角度、光影一律不变；商品上印的任何文字、标签、贴纸、按键、图案必须逐字逐字体原样保留，禁止改字、重绘、变形、位移、增删或替换。【第二层·画面文案保护】商品以外的卖点文字和说明文字必须逐字原样保留，每个字和标点与原图完全一致，仅允许微调排版位置与背景装饰，禁止改写、增删、换字体或以任何理由美化文字内容。【第三层·品牌清除，仅限非商品区域】只清除背景、边角、装饰条等商品以外区域中的品牌名称、商标、旗舰店、京喜自营等品牌标识，商品本体上的任何内容一律不动。如果有人物，把人物换成更漂亮/更帅的模特，姿势与构图尽量保持原样，不得遮挡商品本体。如果这张图没有商品，只做去品牌清理和版式微调，不重绘画面内容。
  （2026-10-08 v3：显式三层规则版，修复商品本体文字被改写问题；版本沿革与根因见 `jd-hot-replica-mode.md`。）
- 【输入商品长标题】= 该源链接清洗后的长标题（去品牌词/平台词），**并去掉空格后面的 SKU 规格属性尾段**（如 "泡澡桶折叠…沐浴桶盆 速开【蓝色鹿80直径】加热款" → "泡澡桶折叠…沐浴桶盆"，用 `strip_sku_spec(title)`）；规格属性会误导生图。
- **主图先判 shared/series 模式**：采集阶段逐 SKU 保留源页 `item.image` 序列，`hot_replica_mode.style_image_mode(source)` 比较各款式 pos2 之后的场景序列。shared 只重制公共主图池；series 额外把每款式专属场景原图落到 `款式组原图/`，preflight 为每款生成 `styleSceneMapping`（同图/与公共主图相同的场景复用，不重复生图）。
- **SKU 规格图**：源 SKU 标注图逐张重制，替换原图后按 SKU 上传到 `SkuGgtImage`。
- **款式代表图**：该款式 SKU 原图的重制图，放款式组末位（@9）。
- 生成命令带 `--continuity continuity-plan-<sku>.json`：每张详情图注入统一风格锚点，连续边界图额外带边缘契约 + 邻居参考图（仅限风格，严禁搬邻居内容）；并发 10 不变。
- 调用契约：sufy-image2 Skill 最新版客户端，模型默认 `gpt-image-2.5`（NextAICore chat 适配层），Adaptive、2K、并发 10（2026-09-30 实测 13.2 张/分钟、单张延迟持平）、重试≤2；结果不明先对账不重放。
- 生成后**直接替换原图、一股脑上传**，不做视觉检测、品牌扫描、人工逐张验收或回查。

### 4. 无生图后视觉检测

- 生图后不做任何视觉检测/审图/品牌扫描；衔接已在生图前注入，动态提示词已包含去品牌属性要求。
- 不做宣称词/主观促销/极限词类审图与返工；平台未禁止上架的内容不上审，由 `validate-jd` 兜底。
- 返工触发条件：**平台驳回上架**（按驳回原因用动态提示词重新生图并重传）；**用户明确指出详情页断层**（只重制与上下邻居同时冲突的异类图单张，带两侧已定稿邻居参考 + 统一风格锚点，不循环）。

### 5. 定价（用户已确认规则）

- 竞品到手价（表格"到手价"列优先；无表按源商品补贴标签推，焕新补贴=15%）。
- 表格行"到手价=价格"（无补贴）时，逐 SKU 竞品到手价 = 该 SKU `sourcePrice`；同 SPU 多档价（如亚克力 59.8 / 锌合金 108）按档分别反推。
- 我方目标到手 `T = 竞品到手 × 0.97`，逐 SKU 用 `sourcePrice` 映射。
- 反推京东价：`Jd − round(Jd×0.15,2) − round(Jd×0.85×0.15,2) = T`，**必须直接调用 `scripts/source_pricing.py:component_source_plan`（Decimal 逐项取整）计算**，禁止自写浮点求解（实测浮点与 Decimal 差 1 分，80.29 vs 80.30，被 upload/preflight 拦截）。
- 单链接正式命令（替代旧任务局部 64 号脚本）：`python scripts/hot_replica_price_plan.py --sku <源SKU> --competitor-ratio <源价→竞品到手比率>`（源价无补贴传 1.0；有补贴按源标签推，如焕新 15% 传 0.85）。
- 锚点自检：源 288 → 竞品到手 244.8 → 我方 237.46 → 京东价 328.66。
- 写入 `SkuJdprice` 随整版编辑提交；禁止按尺码猜阶梯价。

### 6. 上传与发布

- `upload-images --confirm UPLOAD_TO_JD`（断点幂等）。
- `validate-jd` 零违规 → `publish --confirm PUBLISH_TO_JD`（或 `edit-product --confirm EDIT_EXISTING_PRODUCT`）。
- 单链接整链正式命令（替代旧任务局部 63/24 号脚本，2026-10-08 用 10126138878004 全链路实测）：
  1. `python scripts/hot_replica_apply.py --sku <源SKU>`：弹性落位，listing 改指 重制-固定 输出；
  2. `python scripts/hot_replica_publish.py --sku <源SKU> --store-config config/<店铺>.store.json --reference config/<店铺>-<类目>.reference.json --decisions <预检决策.json> --confirm PUBLISH_TO_JD`：三要素身份核对 → 本地预检 → 上传 → 京东校验（99905030004 瞬时过载自动重试）→ 发布 → 独立回查 → 代表图上传 → 整版编辑（rep@gallery / series 每款独立序列 + 逐 SKU `SkuGgtImage`）→ 线上逐位回查 → 视频自动绑定。
- 规格图绑定回查读**提交的编辑体**（`SkuGgtImage` + imgRefId 与线上 skuId 匹配），不读 apply-detail 的 `material.ggtImages`（平台回显恒为空，读它永远 0/N）；结果写 `hot-replica/ggt-verify-<sku>.json` 与 state（`ggtSource: edit-body`）。
- 发布后约 10 秒读线上生效值（`GET /products/{productId}`），不要用申请单待审值当线上值。

### 7. 图片落位（关键，见 sku-image-group-binding.md）

- 主图画廊先判 shared/series：shared 用公共池 9 张 + 每款式组 `[公共主图@1-8 + 去标注代表图@9]`；series 用公共池 + 每款式组 `[该款 SKU 规格图重制版@1 + 该款专属场景图重制版@2..N]`。第 N+1 槽服务端自动补首图副本，不算错误。
- SKU 缩略图 logo = 款式组 sort1（卖点首图）；去标注代表图只放款式组末位。
- **逐 SKU 规格图 `SkuGgtImage`**：imgUrl=该 (款式,尺码) 的源标注图，imgRefId=该 SKU 的 uuid（编辑场景即 skuId）。随 SKUS 组件整版编辑一次写完全部 SKU。
- 颜色组绑定 colorId/imgRefId 必须用 VenderCategoryValue 卡片 mappingId（UUID）；apply-detail 回显为空时以编辑体卡片为准。
- 结果不明或落位乱序：整版编辑一次重建（PATCH 只做单条目补位，多条目会按顺序占位不按 orderSort）。

### 8. 视频搬运

- 源视频 `automate_main_video`：上传一次、逐 SKU 绑定、全部 `relationStatus=4` 才算完成；已绑定其他视频不覆盖。
- 发布链路内置写回执核对（productId/applyId 状态、图片落位回执）保留，属于安全机制；除此之外无发布后逐项回查清单。

## 已知坑（别再踩）

- PATCH WareSkuImg 多条目按顺序占位不按 orderSort；orderSort 只能 1-9。
- `path=skus[skuId==..]`+SkuMainImage 被 DUCC 拒；SKUS+SkuId PATCH 会被当全量写要价格校验。
- 整版编辑颜色组"只增不删"，修图集用整版编辑重建，不要试图 PATCH 删除。
- 连续提交触发"草稿审核中"锁：等 30-60 秒重试，别撤销。
- mappingId 用 attrValues → 颜色组塌缩 logo 掉公共池。
- 汇报价格表必须从价格 plan 文件逐行抄，不手打归档。
- 定价禁止自写浮点求解：浮点 `round` 与 CLI Decimal half-up 会差 1 分（80.29 vs 80.30），必须用 `source_pricing.component_source_plan` 计算，否则被 upload/preflight 拦截。
- 53171 纸巾盒类目：本地 reference Schema 的包装宽 ≤130/高 ≤100 比平台实际严格（宽 135/145、高 110 实测可正常发布）；被 preflight 拦时用任务局部上传脚本跳过本地 Schema 检查（保留 CLI upload 契约+身份核对+3MB 限制+断点幂等），由 `validate-jd` 兜底真实违规。
- 53171 材质必填（选项 value 表见任务脚本 34 模式）、不允许无品牌（brandId=-1 被拒，用店铺已授权品牌如惠宜美 556678）、标题 ≤60 字符单位。
- 53171 已挂品牌纸巾盒的首图顺序修正在当前接口持续 `draftComputeStatus=3` 拒绝且无文案（5 件原顺序 body、千里江山 10 种结构及“只替换代表图为卖点图”的最小改动均复现）；不要反复提交同一类目试验，直接报告平台侧阻塞，线上卖点主要依靠公共池首图和后续位次。
- 规则文件 `category.sourceId` 必须等于源商品实际三级类目（如纸巾盒来源 53154），否则 collect 拒绝；目标类目、材质、品牌、标题需在 rules 与 listing 两处同步修改。
- 销售属性别名含「/」会触发平台「校验异常!当前模式销售属性值名称必填」：整版编辑前清洗别名中的「/」（同 `53_edit_add_skus.py`/`59_seamfix_edit.py` 规则）。
- 分批补 SKU 时平台库存按增量累加：编辑体不带库存组件，需要调库存走专用接口按「目标-当前」差值提交。
- 重量必填类目：源 `weight=0` 时按该品类常规值填写，并在最终汇报列明待用户确认。
- 多款式共用源图（采集按内容去重）时，代表图按源图映射复用，同图款式共用同一张去标注代表图；10 款式组整版编辑一次提交（公共池 9 + 每组 10）已验证。
- 系列品不能套 shared 模板：不同款式 pos2..N 场景序列不同时必须逐款式重制/落位；只复用入口款式序列会让所有 SKU 后面的主图重复（2026-10-09 100165442873 复现，7 款独立序列修复后线上 mismatch=0）。
- 主图视频可在发布后直调 `jd_product_pipeline.automate_main_video`（任务局部 state 文件，幂等上传→转码→逐 SKU 绑定→关系审核），无需跑完整 run 管线。
- SKU 规格值禁止「尺寸如图」类引导语（首发布被驳回根因）：逐 SKU 写具体尺寸/配置文案，不要用"如图/见图"表述。
- 详情源图单张 >3MB 时，collect 用任务局部容错 wrapper（monkeypatch `mover.save_png` 做降采样/量化），保留 3MB 上传限制，不改 Skill 本体。
- `validate-jd` 偶发返回 `99905030004 服务暂时不可用`：是瞬时过载非违规，等 30-45 秒重试即可，不要误判为草稿问题。

## 新对话提示词（复制即用）

请加载 `$jd-jingmai-hot-product-mover` Skill，按京东爆款复刻模式执行批量上架：先读 `references/jd-hot-replica-handoff.md` 和 `references/jd-hot-replica-mode.md`，再读 `references/sku-image-group-binding.md`。

本次任务：

- 目标店铺：【店铺名 + 京麦店铺浏览器快捷方式路径】（新店铺先按 `references/installation-and-configuration.md` 建立该店独立 Profile、store-config 和目标类目 reference，禁止复用其他店铺配置）
- 源京东链接/竞品到手价表格：【表格路径；无表时用源链接补贴率15%推】
- 表格中所有商品，把第一列引流关键词加入商品长标题前面
- 必须上架到家饰日用组名下的类目（先核对类目白名单；白名单外类目不上，写入失败清单）
- 表格中每个源链接都单独上架，不做 AI 同款/同形态/销量代表款判重跳过；仅同源 SKU/SPU 幂等恢复可复用既有商品
- 定价：竞品到手价 ×97% 反推京东价（直降15%+国补85%×15%），逐 SKU 用 sourcePrice 映射
- 图片：先做类目与品牌必填过滤，过滤后的链接先做详情页衔接预检（gemini-3.8-flash-official 源图预检出衔接计划），再用 sufy-image2（gpt-image-2.5 新链路）+ 统一动态提示词（长标题生成，去 SKU 规格尾段）+ 衔接计划重制每张主图/详情图/SKU 规格图，款式代表图放款式组末位；生成后直接替换原图一股脑上传，不做生图后视觉检测与回查，平台驳回上架或用户指出断层才重新生图
