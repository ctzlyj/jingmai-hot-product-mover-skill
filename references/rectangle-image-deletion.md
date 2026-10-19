# 删除线上商品 3:4 主图（rectangleImages）已验证流程

适用：商品曾上传 3:4 主图（rectangleImages / 长图），需要全部删除让前台回落到 1:1 主图。2026-10-19 在南思暖品牌严选店 42 条下架商品全量验证通过。

## 可靠路径：商品编辑页 UI 保存链路

1. 打开商品编辑页 `https://wares-jdm.jd.com/popPublish/?categoryId=<cid>&productId=<pid>&locType=0&isGXproduct=0&publishPageType=4`。
2. 删除默认共享区的 3:4 图：循环点击 `li.long-img .jd-pro-image-close-btn`（每次点第一个，等重渲染）直到无按钮。
3. 点击「保存」，确认发出 `dsm.productbff.product.warePublishPageSaveProductService.saveProductNew` POST。
4. 保存报文中 `rectangleImageMap` 各颜色组传空数组即可清空全部颜色组的 3:4 图；多颜色商品的 3:4 图挂每个颜色组下，UI 共享区只显示一组，但保存会覆盖全部组。

## 不要走的路径

- 直接 `product-applies` 整版编辑提交 rectangleImages 空值不生效（试点实测 rect 数量不变），不要用它删 3:4 图。
- 后台标签页会被 Chrome 冻结导致 CDP 完全无响应（连 `document.readyState` 都超时）：用 `/json/new?<完全URL编码的编辑页URL>` 开新标签（URL 必须整体编码，否则 query 被 & 截断），再关闭冻结页。重新导航会丢弃未保存的表单状态。

## 历史属性缺陷商品的保存拦截与解锁

迁移期商品可能缺必填类目属性，UI 保存被拦且报错只给分类（商品属性/销售属性），需定位具体字段：

- 商品级必填（如材质/类型）：直接改 Pinia store `publish-product.product.productProp` 和 `ShopProductAttributesNew` 组件 `setupState.state.formData`，取同店同类目健康商品的现网值，页面会重新渲染出有效值。
- SKU 级必填（如功能，多选）：错误点保存后才显示（`.component-box-one.is-error`）；逐行点开 SKU 属性行弹窗（shop-sku-attr-drawer），在功能字段下拉多选后点确定。
- 全部清零后再删 3:4 图并保存。

## 保存后独立回查（必做）

用 `product-apply` API 回读：rectangleImages 各组为空；mainImages 分组键与 URL 不变；SKU 价格不变；desc 图片 URL 与顺序一致（平台会把 `<img>` 规范化为自闭合 `<img/>`，属正常）。3:4 删除走整版编辑会触发平台复审（104→105），通过后回落下架态，属预期副作用。
