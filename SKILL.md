---
name: jd-jingmai-hot-product-mover
description: 将京东站内商品链接采集为可审核素材和标准 JSON/Excel，并通过京麦 jdo-cli 幂等发布、回查图片价格库存及自动上传主图视频。适用于 POP 商家搬运已获授权的商品、批量上架、失败恢复和新增类目适配；不用于绕过登录、平台权限、版权授权或营销活动报名。
---

# 站内爆品自动搬运上架京麦

把“采集、审核、发品、视频绑定、发布后审计”视为一个可恢复事务，而不是一次盲目提交。

## 使用前

1. 仅处理用户明确授权使用的商品、图片、详情和视频。
2. 若环境提供 O2，先运行 `o2 search "京麦 商品发布 素材 视频" --json`，阅读命中的 jdo 能力说明；没有完整搬运能力时使用本 Skill 脚本。
3. 阅读 `references/installation-and-configuration.md`，准备 `config/store.local.json` 并刷新当前店铺、当前类目的实时参考文件。
4. 发布前调用店铺信息接口，必须与本地白名单配置同时匹配 `venderId` 和 `shopName`；不匹配立即停止。
5. 新类目不得直接套用现代装饰画 Schema。先按 `references/adapting-new-category.md` 做小规模 MVP。

## 执行路由

- 用户要评估或准备：只采集并生成本地审核件，不传 `--publish`。
- 用户明确要求正式上架：完成本地预检、京东合规校验后，必须要求显式确认字符串 `PUBLISH_TO_JD`。
- 用户提供多条链接：使用 `batch`，任一链接失败即停止后续链接。
- 源商品存在视频：视频只上传一次，但同一个视频必须逐 SKU 建立关系；详见 `references/media-and-video.md`。
- 已有状态文件：从已完成阶段恢复，禁止因详情传播延迟或视频审核等待而重复创建商品。

## 标准命令

```powershell
python scripts/jdo_listing_mvp.py refresh-reference `
  --category-id 35381 `
  --category-name "现代装饰画" `
  --output config/modern-decorative-painting.reference.json

python scripts/jd_product_pipeline.py run `
  --url "https://item.jd.com/源SKU.html"

python scripts/jd_product_pipeline.py run `
  --url "https://item.jd.com/源SKU.html" `
  --publish --confirm PUBLISH_TO_JD

python scripts/jd_product_pipeline.py batch `
  --input links.txt `
  --publish --confirm PUBLISH_TO_JD
```

## 不可变规则

- 源价逐 SKU 读取，任一缺价即停止；默认公式和校验见 `references/modern-decorative-painting.md`。
- 用户提供的详情图默认全部按顺序选入；疑点只告警，不自动筛图。
- 现代装饰画固定使用多套主图；每款式首图不同，后续公共图相同。
- 保持源商品销售属性维度数量，不能从规格文字擅自新增“尺寸”维度。
- 视频上传地址首次请求不得发送 `breakPointFlag=false`；绑定体根节点必须是对象，每次只提交一个 SKU。
- 已有 SKU 绑定其他视频时停止，绝不自动覆盖。
- 已有商品整版编辑不得携带库存；库存修改只能走库存专用接口。

## 完成判据

只有以下证据全部满足才报告成功：商品详情可查、SKU 数量和组合一致、价格公式一致、商家编码唯一、库存专用接口一致、多套主图逐款式完整、详情图数量一致；若有视频，还必须全部 SKU 的视频关系均为审核通过状态。

状态机、幂等恢复与停止条件见 `references/state-machine.md` 和 `references/troubleshooting-and-stop-conditions.md`。完整业务交接见 `references/handoff-manual.md`。
