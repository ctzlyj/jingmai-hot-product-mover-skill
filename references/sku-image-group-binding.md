# 新结构化模型 SKU 主图/规格图写入实测（泡澡桶 10036194410506，2026-09-28）

适用于发布后修正 SKU 图片、按 SKU 上传规格图的场景，全部实测验证。

## 平台模型（官方文档 + 实测）

- 主图画廊只有三种绑定：整商品（colorId=0000000000）、颜色销售属性（第一维）、SKU 直挂。
  **主图画廊不支持按第二维（如尺码）区分**——同颜色不同尺码共用同一套主图，逐尺码"主图"不存在；
  老链接的逐 SKU 首图是旧模型遗产，主图 API 写不回去。
- **SKU 规格图（SkuGgtImage，1:1 一张）是按 SKU 直挂、逐 SKU 独立的**，全部类目 schema 已带该组件
  （config/*.reference.json 可查，"类目没有 SkuGgtImage"的旧结论作废）。结构：
  ```json
  {"componentCode":"SkuGgtImage","value":[{"componentCode":"img","properties":[
    {"key":"imgUrl","value":"jfs/..."},{"key":"imgRefId","value":"<该SKU的uuid（=skuId）>"}]}]}
  ```
  校验：1:1、480–1500px、单张。读回在 `products[].material.ggtImages=[{imageUrl,uuid}]`，1:1 落库即生效。
  随整版编辑 SKUS 组件一起提交即可（编辑场景 imgRefId 用 skuId 值本身）。
- 想通过"规格图"按尺码展示正确尺码图：直接把每个 (款式,尺码) 对应的标注图写进对应 SKU 的 SkuGgtImage 即可，40 SKU 一次整版编辑搞定。
- 探针记录（别再试）：`path=skus[skuId==..]` + `SkuMainImage` 被 DUCC 白名单拒；`SKUS+SkuId` PATCH
  被当 sku 全量写（要求京东价+销售属性组合校验）。

## 颜色组写入规则（编辑场景）

- colorId/imgRefId 必须等于该 SKU 组 **VenderCategoryValue 卡片里的 mappingId（32位UUID）**；
  用 attrValues（101314…）会导致颜色组塌缩、logo 全部掉到公共池首图（v14 实测复现）。
  整版编辑体里同一个 mappingId 必须三处一致：vcv 属性卡、SkuMainImage sort1、WareSkuImg 组条目。
  apply-detail 的 saleAttrs.mappingId 可能因草稿态显示为空；以编辑体 vcv 卡片的 mappingId 为准复用。

## 原子更新 PATCH（WareSkuImg, path=product, opType=3）的坑

- 一次 PATCH 多个 img 条目时，落位**按条目顺序依次占用 1,2,3… 号位，不按提交的 orderSort**；
  修复类 PATCH 必须一次只放 1 个条目。
- orderSort 可提交范围 1–9；提交 10 报"图片位置号不合法"。第 10 槽由服务端自行补位（可能复制组首图），
  编辑 API 无法直接控制；把公共主图1放组内 slot2 让它循环即可。
- 连续 PATCH 触发"草稿信息正在审核中"临时锁，等 30–60 秒重试。

## 当前落位规则（2026-09-29 更新）

整版编辑一次提交：公共池 9 张保持公共购买顺序且 sort1=卖点首图；每款式组用

```text
[公共主图@1-8, 去标注款式代表图@9]
```

**不要把去标注代表图放在款式组首图**。SKU 缩略图 logo 由款式组 sort1 决定，因此当前应显示卖点首图；去标注代表图只承担款式组的末位参考图。第 10 槽是服务端自动补位，可能复制款式组首图，不主动写。

爆款复刻正式链路（`hot_replica_publish.py`，源自 24_publish 实战版）的实际落位与上段 53207 批次不同：每款式组提交
`[去标注代表图@1, 公共主图(除卖点首图)@2..N]`，公共池（colorId=0000000000）保留全部公共主图且 sort1=卖点首图。
2026-10-09 双商品实证（100165442873 提交5张/线上6张；锚点 10036254103035 提交9张/线上10张）：
整版编辑生效后服务端会在 N+1 槽自动补位并复制组首图（rep），该尾槽不主动写、也不算错误；
`shared_image_audit` 已接受该形态（`serverFilledTailStyles` 字段），其他多余或缺张图仍报错。

### 系列品模式（2026-10-09，100165442873 实测）

- 判定：`hot_replica_mode.style_image_mode(source)` 逐 SKU 读取源页 `item.image`，比较各款式 pos2 之后的场景序列；至少两个款式序列不同即为 series。
- 落位：公共池保留重制公共主图；每款式组提交 `[该款 SKU 规格图重制版@1, 该款专属场景图重制版@2..N]`。同源场景图按 URL 去重，入口款与公共主图相同的场景复用 `SPU-MAIN-*`。
- 回查：`series_image_binding_report(listing, uploaded, plan, apply_data)` 用 `saleAttrs.attrValues` 定位线上款式组，逐位比对 `material.mainImages`；允许服务端在 N+1 复制首图的补位尾槽，但要求各款序列互不相同。实测 7 款：mismatch=0、distinctSequences=7、7 组均有补位尾槽。

实测基线：4 件无品牌 53207 坐便椅按上述结构整版编辑通过并在线生效（款式组首图=公共池卖点首图，末图=去标注代表图）。5 件已挂品牌 53171 纸巾盒的首图顺序修正在当前接口持续 `draftComputeStatus=3` 拒绝且无文案；千里江山的最小文本/图片替换也两次复现拒绝。遇到同状态即停止重复提交并报告平台侧阻塞，不为继续试探消耗调用。
