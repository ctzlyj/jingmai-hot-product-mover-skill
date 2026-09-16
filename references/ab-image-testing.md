# 已授权的换图A/B链接

仅用于用户明确要求“保留A原链接，新增B换图链接，商家SKU、京东价和规格一致”的测试。不是已有商品编辑，不是普通搬运去重，也不隐含授权修改A链接、营销活动、预算或价格。

## 已验证的实现

`scripts/pdd_ab_image_publish.py` 复用官方CLI上传、合规校验、发品与回查。普通 `publish_local_draft.py` 会重分配商家编码且按源货号恢复已有商品，不可直接用于该模式。B使用独立状态目录及独立技术货号 `ABIMG-<原商品ID>`，但商家SKU逐字保留；平台商品ID及平台SKU ID必须是新的。

输入来自PDD修图批次审计和原商品发布状态，不能凭图片文件名推测原价或商家编码。`preflight.json` 的 `records` 中只有 `action=NEW_B_LINK_SAME_MERCHANT_SKUS` 且能实时回查原商品的项目可执行；需含 `sourceSku`、`originalProductId`、`sourceSkuCount`、`repairedFolder`。还需完整 `store` 身份和空 `failures`。修图清单的 `products[].rows[]` 保留真实来源序号与文件名，修图 `audit.json.outputs[]` 保留每张图片的SHA-256。

## 执行顺序

1. 从 `--source-root` 读取当前商品文件夹的表，按完整来源规格匹配商家已填编码，不能用旧归档空码推断当前表。实时回查A，编码与当前表冲突时停止该项，另行授权纠码，不能把旧自动码继续复制到B。表码和A一致后锁定京东价、销售属性和原图片；未配对、类目驳回、缺码、重复码或规格数量不同即停止该项。
2. 使用 `prepare` 生成B草稿和 `plan.json`、`dry-run.json`；校验全部修正版图片哈希，复用原公共主图和详情图，不覆盖任何A状态。价格直接取A线上值，不重新套用反推公式。
3. 用户确认 `PUBLISH_TO_JD` 后运行 `publish`。执行前核对plan摘要、草稿和所有文件哈希，再检查A仍与快照一致；每次写操作实时核验店铺。
4. 先一条、再十条、再全批。B创建响应立即保存ID；恢复时复用该ID。若提交结果不明，停止并保留 `SUBMITTING`，不得再次新增。
5. 验收B所有商家SKU、京东价、规格、新平台ID、款式首图、公共图、详情、上架状态和库存，并独立复查A未改变。当前实现要求标准10000库存；含视频或其他库存策略不得直接使用。

```powershell
python scripts/pdd_ab_image_publish.py prepare --output <AB目录> --preflight <预检JSON> --inventory <修图清单JSON> --state-root <原发布state目录> --source-root <当前商品文件夹父目录> --store-config <店铺配置> --profile <独立Profile> --reference <类目reference> --mvp-source-sku <试点来源ID>
python scripts/pdd_ab_image_publish.py publish --output <AB目录> --store-config <店铺配置> --profile <独立Profile> --reference <类目reference> --confirm PUBLISH_TO_JD --limit 1
```

试点通过后 `--limit 10`，十条全部通过后省略 `--limit`。重复运行验证既有B，不清空状态或生成第三条链接。不自动删除A/B，不自动改码绕过平台拒绝。

用户明确授权“失败统一跳过、最后给失败清单”时，加 `--skip-failures`：已驳回项保留申请ID并直接跳过；其他商品级异常保存 `failure.json` 后继续下一条，不逐条请求确认，也不花时间调查单个失败后再停整批。`SUBMITTING` 等提交结果不明状态保持原样，禁止清空状态或自动重复创建。每次失败后复核店铺身份；身份/鉴权错误仍全局停止，不能以跳过为由向未知店铺写入。最终完整汇总成功、失败、未执行数量及原始原因；`COMPLETED_WITH_FAILURES` 表示已处理完，不表示所有商品上架成功。未获跳过授权时保留默认遇错停止行为。

## 验证与维护

`python -m unittest discover -s tests -p test_pdd_ab_image_publish.py -v` 覆盖克隆保持A、实时价与原码、不完整映射停止、确认摘要变化、原链接指纹，以及单项失败后继续下一项且保留提交状态和商品ID。已通过真实重复商家SKU新发、完整详情回查、同一B状态恢复和跳过驳回后继续发布验证。

这是用户级Skill的独立新增脚本/测试/参考文件。上游更新保留新增文件；对 `SKILL.md` 和公共上传器按 `image-upload-names.md` 记录的Git基线进行三方语义合并，禁止覆盖整个目录。未通过回归和真实或等价回查前不得替换本地增强。
