# 已有商品批量设置品牌（brand full-edit）

## 触发条件

用户要求给一家或多店铺的已有商品设置/修改品牌（例如「把这两家店的商品链接全部加上品牌 鸿运鸟[1886203]」）。使用 `scripts/set_product_brand.py`，不要重新探索接口。

## 命令

```powershell
python C:/Users/caotong.888/.joycode/skills/jd-jingmai-hot-product-mover/scripts/set_product_brand.py `
  --profile jianxitang --shop-name 见喜堂装饰画店 `
  --brand-id 1886203 --brand-name 鸿运鸟 `
  --run-dir .\brand-run-1886203
```

模式：默认全量执行；`--dry-run` 只做 schema 校验不提交；`--audit-only` 只回查品牌与图片完整性；`--product-ids 1,2` 限定范围；`--limit N` 先试前 N 个。每店铺一次调用，`--profile`/`--shop-name` 必须与 venderinfo 回查一致；执行和 dry-run 模式必须给 `--brand-name`（品牌预检用 `brandName` 精确搜索，全量列表有 2000+ 条不可翻页扫）。`--product-ids` 模式不依赖商品列表接口（其分页不稳定，可能只回前 ~89 条），直接按 ID 构建。

## 为什么必须整版编辑（已验证结论）

- 原子更新 `PATCH /rest/sp-product/v0/product-applies/{applyId}` **不支持 WareBrand 组件**（报 `10103001002 参数校验失败`），不要再尝试。
- 唯一可行路径：`POST /rest/sp-product/v0/product-applies` 整版编辑，body 顶层 `{"scene":"pop","schema":"<字符串化组件数组>","productApplyDTO":{"productId":<pid>,"saveActionType":0,"categoryDTO":{"lastCategoryId":<cid>}}}`，无 request 包装层；大 body 走 `cli.run_jdo_large`。

## 整版编辑保真不变量（2026-10-03 线上事故换来的，禁止简化）

1. **sale-prop 的 `mappingId` 必须填该销售属性的 `attrValues` 绑定值**。apply-detail 返回的 `mappingId` 是空串，原样提交空值会让平台**删除所有颜色专属图片组**（曾致 19 个商品颜色主图丢失，靠 before 备份恢复）。
2. 每个 SKU 必须带 `SkuUUid = str(skuId)`，再带 `SkuId`。
3. 已有商品整版编辑**不得提交 `SkuStockNum`**（库存走专用接口）。
4. 图片条目须带 `isPrimary`/`isGgt` 和原 `orderSort`。
5. `WareSkuImg` 只提交共享组（uuid=0000000000）；颜色组通过逐 SKU 的 `SkuMainImage`（colorId=颜色绑定值）重建。

## 已知平台行为与失败分类

- **平台会给标题加品牌名前缀**（productName/skuName 都会变）：设置品牌后的平台强制行为，属预期，不是缺陷，不要尝试规避或当作校验失败。
- 销售属性别名带 `xxx:` 冒号前缀 → `1010300506 销售属性值名称必填`：脚本自动去前缀提交并记入 `aliasNormalized`。
- 商品参加「便宜包邮」等活动 → `1010300506 禁止修改商品信息`：分类 `activity-blocked`，活动期间无法处理，结束后重跑即可补上。
  注意：`product-schemas/validations` 校验接口对活动商品也会放行，活动拦截只在真实提交时出现，所以 dry-run 通过不等于能提交。
- SSL EOF / WinError 32 / `系统异常!更新SKU维度价格验证失败`：瞬态错误，脚本自动退避重试；失败提交不改动数据，重试幂等。
- 商品已有其他非空品牌：脚本拒绝覆盖并记失败，不询问。

## 断点续跑与产物

- `run-dir/ledger.jsonl`：逐商品记录（ok/skipped/error），重跑自动跳过已 ok 项。
- `run-dir/before_<pid>.json`：提交前完整备份（审计比对基线，勿删）。
- `run-dir/failures.json`：失败清单（含分类）。
- `--audit-only` 模式产出 `final_audit.json`：品牌命中数 + 图片组/URL 逐组比对结果。

## 完成判据

全店 `--audit-only` 通过：品牌命中数 = 商品总数（活动保护项除外），且每个有 before 备份的商品图片组 uuid 集合与 URL 顺序完全一致、SKU 集合/价格/销售属性别名/详情/白底图不变。列表接口的 brandId 可能滞后，最终以 apply-detail 回查为准。
