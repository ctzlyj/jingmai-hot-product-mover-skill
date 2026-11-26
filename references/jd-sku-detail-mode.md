# SKU 维度详情模式（京麦客户端人工设置）

## 结论

“详情随 SKU 切换”当前只有 **京麦客户端** 的人工编辑路径可用，REST API、开放 schema 和网页端都没有支持；不要尝试通过接口补字段或浏览器自动化绕过。

## 三条通道取证

1. **REST 接口**
   - 创建/编辑接口没有 `descModel` 可选参数；类目 schema 中也不存在对应组件。
   - 提交 `descModel=2` 时，接口会返回 `success`，但服务端会 **静默丢弃** 该字段。
   - 回读结果仍是 `productInfo.productDetailDesc` 中的 SPU 详情，`skuList[].productDetailDesc.desc` 全部为空。
2. **京麦网页**
   - 网页创建/编辑页没有“SKU 模式”选项。
   - 102 个 JS chunk 和实时类目 schema 均未出现 `descModel` 或 SKU 详情组件。
3. **官方手册**
   - 官方手册明确说明 SKU 维度详情仅京麦客户端支持，ISV 侧未跟进。
   - 因此网页自动化、客户端 CDP、Playwright、浏览器插件或远程调试都不能替代人工客户端操作。

## `descModel` 假成功风险

- 不要因为接口返回 `success` 就认为 SKU 详情已生效。
- 必须回读：
  - `productInfo.productDetailDesc`
  - `skuList[].productDetailDesc.desc`
- 只有当 **商品详情** 与 **逐 SKU 详情** 都满足预期时，才能认为详情模式已按目标生效；否则一律视为未生效。

## 客户端人工步骤

1. 在京麦客户端打开目标商品编辑页。
2. 进入详情编辑器，切换到 **详情随 SKU 切换** 模式。
3. 按 `skuDetailPendingKeys` 中的 SKU 清单，逐个选择对应详情内容。
4. 保存后回读 `productInfo.productDetailDesc` 与 `skuList[].productDetailDesc.desc`，确认逐 SKU 详情已生效。
5. 只有回读全部通过后，才允许把 `skuDetailManualPending` 改为 `false`。

## 10 个 SKU 的映射要求

- 每个 SKU 必须维护 `skuKey -> skuId -> 详情内容` 的唯一映射。
- 不得把 SPU 详情误当成 SKU 详情，也不得只给部分 SKU 补详情。
- 10 个 SKU 全部设置完成后，才可视为 SKU 详情闭环完成。
