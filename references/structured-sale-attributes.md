# 结构化销售属性与发布结果（1.2.3）

## 为什么平台预校验通过不代表属性正确

类目的销售属性若 `extra.isStructure=true`（例如类目 35057 的“尺寸”），平台预校验（`product-schemas/validations`）**不检查**别名格式，只有真实提交才报 `1010300506 …别名[xxx]格式错误`。结构化属性必须随 SKU 提交 `templateSkuProps`（每个填写框的 `id/value/valueId/unit`），别名 = 各框取值拼接。因此：**发布前必须已知模板，不能靠预校验判断。**

## 模板获取顺序（`photo_listing.py discover-sale-templates`）

1. 官方 `GET /rest/sp-product/v0/categories/{categoryId}/sale-attr-templates`。
2. 无权限（如 `99904030008`）或无结果：只读读取**同店、同末级类目、列表状态在架（1/103）**的商品销售属性，取出同一属性的 `templateSkuProps` 结构。只借“填写框结构”（框 id、单位），**不借样本取值**；所有样本必须一致（较短的样本须是最长样本的前缀，多出的尾部框记为可选），否则不猜。
3. 仍无法确定：发布前停止，`questions` 一次性提示用户提供该属性在京麦后台的填写框；不得凭空猜模板 ID，不得使用其他店或无关类目的结构。

结果写 `sale-attr-templates.json` 与 `manifest.saleAttributeTemplates`（来源商品 ID、类目、属性名、框 ID、依据，不含凭据）。

## 使用

1. `discover-sale-templates` → 看 `missing`；`missing` 非空则按 `ask` 问用户。
2. 用户给每个 SKU 的取值（`facts.skus[].templateValues`，每个框一个值），运行 `apply-sale-templates`：生成 `styleTemplateSkuProps`、规格别名。
3. `build_listing` 对结构化属性缺 `templateSkuProps` 的 SKU **直接报错并阻止发布**。图片/SKU 规格文字变化后摘要变化，需重新确认。

## 发布结果状态与防重复

| 状态 | 含义 | 能否再次提交 |
| --- | --- | --- |
| `SUBMITTING`/`UNKNOWN` | 请求发出但结果不明（超时/断流） | 否：只回查（技术货号 + 同标题），找到则接管 |
| `REJECTED` | 平台返回错误码，明确拒绝（原文存 `.publish/rejection.json`） | 否：先修因，再 `confirm-not-created` |
| `REJECTED_CONFIRMED` | `confirm-not-created` 只读证明货号和同标题都无商品 | 是：允许再提交，证据在 `.publish/no-product-evidence.json` |
| `SUCCEEDED` | 已拿到商品 ID | 否 |

`confirm-not-created --reason "<平台原文或原因>"` 只做只读查询；若找到匹配商品则拒绝并提示用 `reconcile` 接管。

## 在架判断

商品列表/详情中真实在架值是 `productStatusNew=103`（`1` 也接受）。`104`、`105`、`0`、`2`、`8` 等一律不算在架。
