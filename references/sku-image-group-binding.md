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

## 推荐的款式代表图修法（已验证）

整版编辑一次提交：WareSkuImg = 公共池 9 张 + 每款式组 `[款式代表图@1, 公共主图@2-9]`；
40 个 SKU 的 SkuMainImage sort1 同写款式代表图（colorId/imgRefId=款式 mappingId）。
提交后 apply-detail 出现 5 组，logo=款式代表图，线上约 10 秒生效。
款式代表图建议做成**无尺寸标注的中性图**（sufy-image2 去文字），配合按 SKU 的规格图展示尺码信息。
