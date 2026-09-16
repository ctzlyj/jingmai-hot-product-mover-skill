# 修正版按商品表格补发

## 何时切换

用户明确要求把已修图商品全部新增上架，并指定以每个商品文件夹表格中的商家SKU和PDD源价为准时，缺少旧链接或历史普通搬运判重不再是排除理由。使用 `scripts/pdd_repaired_supplement.py`，不要强行套用必须存在A链接的克隆器。此模式不证明新旧链接价格、规格一一相同，不能把没有A基准的商品称为严格配对的A/B结果。

普通搬运仍遵守原判重流程；此前类目驳回的申请不得混入补发或悄悄换类目重建。明确新增、价格口径和失败跳过授权只覆盖当前清单，不扩展到其他商品或修改旧链接。

## 输入与价格

- `preflight.json.records` 选择当前明确授权的 `action=UNPAIRED_REQUIRES_BASELINE` 项，含 `sourceSku`、`repairedFolder`。修图清单保留真实行序、文件名，v2修图审计保留每张修正版的哈希。
- 每个原商品目录只有一个工作簿，表头为 `上架ID、标题、款式、套餐、价格/同行售价、库存、商品编码、是否上架、标价、优惠券、实际售价`。本次两倍口径的源价来自第五列，商家SKU来自第七列；不要把后续旧标价/券/公式列当作PDD源价。
- 商家SKU逐字保留，单商品缺码、部分缺码或重复码即记录跳过，不自动生成、重编或照搬相似商品编码；不同商品间重复编码允许用于明确授权的重复上架。
- 本实现限于目标价=PDD逐SKU源价×2、直降15%、国补15%、券0、差额0。复用 `jd-competitor-price-adjustment-skill` 的分位求解器，不复制公式实现；线上回读后再用独立整数分公式核对精确目标。不同公式不能直接复用。
- 技术货号为 `ABFIX-<来源ID>`，不改变商家SKU；独立状态目录，所有提交均无旧商品ID或旧平台SKU ID。

## 最小执行闭环

1. `prepare` 仅只读店铺并生成草稿。校验真实表格行、图片行序、修图审计与文件哈希；同款式图片只有哈希一致才复用同一首图路径。源工作簿和图片不修改。
2. 审阅主图、详情与标题，在输出目录写 `content-review.json`，按来源ID记录 `decision=PASS/SKIP`、具体原因和证据。复用历史合规副本时必须同时匹配来源商品ID和源图/副本哈希：相同通用模板哈希不能授权把其他商品的场景图当作替换图。没有验证过的可用副本时，不自动改图。
3. `seal` 将内容结论加入计划并生成摘要绑定的 `dry-run.json`。用户已确认当前清单、定价和新增上架后，使用 `publish --confirm PUBLISH_TO_JD`；脚本按已授权的失败跳过模式执行，身份/鉴权错误仍停止全局写入。
4. 先单条、再十条、再全量。限量运行标为 `PARTIAL`，不得声称全量完成。旧链接不编辑，不报名活动；库存沿用本链路10000默认值，含视频或其他库存策略不得直接使用。
5. 创建响应立即落盘ID；`SUBMITTING`且无ID、已驳回或验证仍待传播的项保留状态，不清空后重发。恢复时沿用同一输出目录和计划；不得为绕过状态另建目录。已有ID只回读，不再次新增。
6. 核验完整SKU、表格编码、规格、京东价、逐项取整目标、多套主图首图和公共图、详情图数量、启用/上下架及专用库存接口；最终按成功、失败/跳过、未执行分别交付。

```powershell
python scripts/pdd_repaired_supplement.py prepare --output <补发目录> --preflight <24项等当前清单.json> --inventory <修图清单.json> --profile <白名单Profile> --store-config <店铺配置.json> --reference <实时类目reference.json> --rules <定价及类目规则.json> --compliance-reviews <同商品历史合规审核.json>
python scripts/pdd_repaired_supplement.py seal --output <补发目录> --profile <白名单Profile> --store-config <店铺配置.json> --reference <实时类目reference.json>
python scripts/pdd_repaired_supplement.py publish --output <补发目录> --profile <白名单Profile> --store-config <店铺配置.json> --reference <实时类目reference.json> --confirm PUBLISH_TO_JD --limit 1
```

## 验证与保留

`python -m unittest discover -s tests -p 'test_pdd*.py'` 覆盖表格解析、原编码、精确价格、缺码/重码、禁止旧平台ID、提交不明禁止重发、同商品替换约束及限量不冒充全量。真实发品后还须保留线上详情及库存回查。

安装层回归曾复现倍率未传入普通草稿价格函数、共享图片缓存函数缺失的问题。现补齐 `jd_product_mover.target_price` 和 `taobao_product_mvp.pricing_audit` 的可选倍率（默认仍为1），并同步 `publish_local_draft` 的店铺隔离哈希缓存辅助函数。严格逐项取整仍使用独立求解器，不把普通草稿除法当成严格验收。

新增脚本、解析器、测试和本参考文件属于个人覆盖层；上游更新保留新增文件。`SKILL.md`、`scripts/jd_product_mover.py`、`scripts/taobao_product_mvp.py`、`scripts/publish_local_draft.py` 按 `image-upload-names.md` 的既有三方合并基线非破坏式合并，不覆盖本地冲突，不修改官方安装目录。合并后运行上述回归和Skill校验。
