# 京东爆款复刻模式交接手册（2026-09-28 首链路全通版）

本手册面向新对话直接执行京东爆款复刻上架，全部规则来自泡澡桶批次（源 10233331213967 → 千百日用品质店店 10036194410506）的真实全链路验证。开工前先读 `SKILL.md` 和 `references/jd-hot-replica-mode.md`；图片落位细节在 `references/sku-image-group-binding.md`。

## 固定环境（本机）

- Skill 仓库：`C:\Users\caotong.888\.joycode\skills\jd-jingmai-hot-product-mover`（GitHub：`ctzlyj/jingmai-hot-product-mover-skill`）
- Python：仓库内 `.venv\Scripts\python.exe`；CLI 入口 `scripts\jdo_listing_mvp.py`
- 任务局部脚本目录：`D:\caotong.888\Documents\ChatGPT\搬运上架skill\scripts\<品类-日期>\`（草稿/探针/价格表都放这里，不进 Skill）
- 生图 Key：`%LOCALAPPDATA%\JoyCode\credentials\sufy-image2.dpapi`（DPAPI，不进任何源码/日志）
- 店铺身份：`config\store.local.json` 白名单 + 店铺专属 store-config（如 `qianbai-bath-tub.store.json`）；每次写操作前 CLI 自动三要素核对（店铺名+venderId+Profile 一致），禁止 default Profile

## 端到端流程（顺序执行）

### 1. 采集建稿

- 输入：用户给京东爆款链接 + 目标店铺。
- 用 `jd_product_mover.py`/`jd_product_pipeline.py` 采集：完整 SKU（含逐 SKU `sourcePrice`）、主图、逐 SKU 尺寸标注图、详情长图、主图视频。
- 生成草稿目录 `outputs/migrations/<源ID>_<标题>/`：`listing.json`（sourcePrice 必须逐 SKU 落库）、`SKU图/`、`主图/`、`详情图/`、`视频/`。

### 2. listing 清理（搬运模式规则）

- 新标题：卖点前置、无品牌词/进口国/第三方平台词/极限词，长度按类目限制。
- 类目属性按详情图证据改（如材质）。
- 详情图直接删：品牌宣传/榜单销量无证据宣称/通用服务条/无商品信息页；其余保留并重新编号。
- 尺寸别名冗余词清理（如"只有蓝色"）。
- 定价基准先问用户（默认规则见第 5 步，用户给了表格就直接执行）。

### 3. 图片高级重制（sufy-image2 直连）

- **主图 9 张**：SPU 公共池，购买路径组织（首图卖点+场景+卖点+白底等），提示词 agent 自主设计，商品本体/颜色/结构不变。
- **SKU 图保留原样**（源图上的尺寸标注是销售信息，后续当规格图用）。
- **每款式 1 张"去标注代表图"**：sufy 编辑"移除全部文字/标题/角标/卖点列表/尺寸标注线/底部套餐栏，商品本体、款式（盖/无盖）、颜色、场景、构图完全不变，遮挡区自然补全"。人工逐张验收款式正确（注意源图池里盖/无盖混标，不能只按文件名）。
- 生图调用参考 image-studio 契约：`SF-gpt-image-2`、Adaptive、2K、并发 2、重试≤2；结果不明先对账不重放。

### 4. 门禁与用户审核

- `image_quality_gate.py --gate-mode remake`（productSame/violationsCleared/qualityOk/noHoles/textLegible 必查）。
- 视觉审图 lite 全量初筛 + flash 复核疑似，两档冲突不判干净。
- 用户审核重制结果后再上传。

### 5. 定价（用户已确认规则）

- 竞品到手价（表格"到手价"列优先；无表按源商品补贴标签推，焕新补贴=15%）。
- 我方目标到手 `T = 竞品到手 × 0.97`，逐 SKU 用 `sourcePrice` 映射。
- 反推京东价：`Jd − round(Jd×0.15,2) − round(Jd×0.85×0.15,2) = T`，在 `T/0.7225` 附近 ±0.60 搜分值精确解。
- 锚点自检：源 288 → 竞品到手 244.8 → 我方 237.46 → 京东价 328.66。
- 写入 `SkuJdprice` 随整版编辑提交；禁止按尺码猜阶梯价。

### 6. 上传与发布

- `upload-images --confirm UPLOAD_TO_JD`（断点幂等）。
- `validate-jd` 零违规 → `publish --confirm PUBLISH_TO_JD`（或 `edit-product --confirm EDIT_EXISTING_PRODUCT`）。
- 发布后约 10 秒读线上生效值（`GET /products/{productId}`），不要用申请单待审值当线上值。

### 7. 图片落位（关键，见 sku-image-group-binding.md）

- 主图画廊：公共池 9 张 + 每款式组 `[去标注代表图@1 + 公共主图@2-9]`（第 10 槽服务端自动补，不必控）。
- SKU 缩略图 logo = 款式代表图（颜色组 sort1 决定）。
- **逐 SKU 规格图 `SkuGgtImage`**：imgUrl=该 (款式,尺码) 的源标注图，imgRefId=该 SKU 的 uuid（编辑场景即 skuId）。随 SKUS 组件整版编辑一次写完全部 SKU。
- 颜色组绑定 colorId/imgRefId 必须用 VenderCategoryValue 卡片 mappingId（UUID）；apply-detail 回显为空时以编辑体卡片为准。
- 结果不明或落位乱序：整版编辑一次重建（PATCH 只做单条目补位，多条目会按顺序占位不按 orderSort）。

### 8. 视频搬运

- 源视频 `automate_main_video`：上传一次、逐 SKU 绑定、全部 `relationStatus=4` 才算完成；已绑定其他视频不覆盖。

### 9. 回查清单（全部通过才算上架完成）

- [ ] 商品在售，全部 SKU enable
- [ ] 主图组结构：公共池 9 + 各款式组 10，sort1=代表图
- [ ] logo 逐款式正确
- [ ] `material.ggtImages` 逐 SKU 对应正确尺码图
- [ ] 逐 SKU `jdPrice` 与价格表一致，反算到手价=T
- [ ] 视频关系全部审核通过

## 已知坑（别再踩）

- PATCH WareSkuImg 多条目按顺序占位不按 orderSort；orderSort 只能 1-9。
- `path=skus[skuId==..]`+SkuMainImage 被 DUCC 拒；SKUS+SkuId PATCH 会被当全量写要价格校验。
- 整版编辑颜色组"只增不删"，修图集用整版编辑重建，不要试图 PATCH 删除。
- 连续提交触发"草稿审核中"锁：等 30-60 秒重试，别撤销。
- mappingId 用 attrValues → 颜色组塌缩 logo 掉公共池。
- 汇报价格表必须从价格 plan 文件逐行抄，不手打归档。

## 新对话提示词（复制即用）

请加载 `$jd-jingmai-hot-product-mover` Skill，按京东爆款复刻模式执行上架：先读 `references/jd-hot-replica-handoff.md` 和 `references/jd-hot-replica-mode.md`，再读 `references/sku-image-group-binding.md`。

本次任务：

- 源京东链接：【粘贴链接】
- 目标店铺：【店铺名】
- 竞品到手价表格：【路径或"用源链接补贴率15%推"】
- 定价：竞品到手价 ×97% 反推京东价（直降15%+国补85%×15%）
- 图片：sufy-image2 重制主图+详情，SKU 标注图保留作规格图，每款式做去标注代表图
- 完成后按交接手册第 9 节回查清单逐项核验再汇报
