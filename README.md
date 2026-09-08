# 站内爆品自动搬运上架京麦

一个面向京东 POP 商家的 Codex Skill：输入已获授权的京东商品链接，生成可审核的标准 JSON/Excel 与本地素材，并通过 `jdo-cli` 完成图片上传、合规校验、商品发布、主图视频上传、逐 SKU 绑定和发布后审计。

> 当前生产验证范围：末级类目 **现代装饰画（35381）**。其他类目需要先刷新实时 Schema，再建立并验证类目适配规则，不能直接复用装饰画字段。

## 为什么值得做

- **直接复用现有能力**：`jdo-product-material`、`jdo-product-publish`、`jdo-product-status` 已覆盖核心接口，避免重造签名和鉴权。
- **轻量增强（当前方案）**：在官方 CLI 上增加采集、映射、状态机、幂等和审计，建设成本与维护成本最低。
- **另建服务**：只有需要多人队列、定时任务、审批台和集中数据库时才值得；当前阶段不推荐。
- **停止扩张条件**：新类目样本无法稳定提取逐 SKU 价格、Schema 频繁变化或授权边界不清时，停止自动发布，仅保留采集审核。

## 已验证能力

- 从 `newColorSize` 获取真实 SKU，并逐 SKU 请求移动商品页价格。
- 下载主图、SKU 首图、全部详情图、主图视频和视频封面。
- 生成 `manifest.json`、`listing.json`、审核 Excel 和可恢复状态文件。
- 图片空间上传、发布 Schema 构建、京东合规检测、正式发布。
- 现代装饰画固定多套主图，每款式“专属首图 + 公共后续图”。
- 主图视频上传一次、等待转码、选择类目标签、逐 SKU 绑定并等待全部审核通过。
- 发布后核对 SKU、价格、库存、商家编码、销售属性、主图和详情图。
- 以源 SPU 查重；重复执行从状态文件恢复，不重复创建商品或上传已存在视频。

## 安装

```powershell
git clone https://github.com/ctzlyj/jingmai-hot-product-mover-skill.git
cd jingmai-hot-product-mover-skill
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config\store.example.json config\store.local.json
```

按 `references/installation-and-configuration.md` 填写本地店铺配置、完成 `jdo-cli` 登录并刷新类目参考文件。`store.local.json`、实时 Schema、运行产物和凭据都已被 `.gitignore` 排除。

## 快速开始

```powershell
# 1. 当前登录店铺下刷新实时发品 Schema 与图片空间信息
.\.venv\Scripts\python.exe scripts\jdo_listing_mvp.py refresh-reference `
  --category-id 35381 `
  --category-name "现代装饰画" `
  --output config\modern-decorative-painting.reference.json

# 2. 只生成本地审核件并通过京东合规校验，不发布
.\.venv\Scripts\python.exe scripts\jd_product_pipeline.py run `
  --url "https://item.jd.com/源SKU.html"

# 3. 用户确认后正式发布
.\.venv\Scripts\python.exe scripts\jd_product_pipeline.py run `
  --url "https://item.jd.com/源SKU.html" `
  --publish --confirm PUBLISH_TO_JD

# 4. 批量发布；links.txt 每行一个链接，遇到失败立即停止
.\.venv\Scripts\python.exe scripts\jd_product_pipeline.py batch `
  --input links.txt `
  --publish --confirm PUBLISH_TO_JD
```

如需指定其他路径，可传 `--store-config`、`--rules`、`--reference` 和 `--output`。

## 现代装饰画默认规则

- 品牌：无品牌。
- 工艺：印刷。
- 售后：支持 7 天无理由退货。
- 运费模板：由每个店铺自己的 `store.local.json` 提供。
- 库存：每个 SKU 默认 `10000`。
- 包装尺寸、商品毛重：当前实时 Schema 非必填，不采集、不提交。
- 主图：固定多套主图；每款式首图不同，后续公共图一致。
- 详情图：默认全部按源顺序保留，不做主观筛选。
- 编码：挂轴/卷轴使用 `GZH`，粘贴/自粘使用 `ZTH`，后接五位款式号和尺寸字母。

### 价格公式

默认让计划活动后的预计到手价比源商品逐 SKU 当前价低 `0.10` 元：

```text
K = ROUND((F-G+J)/((1-H)*(1-I)), 2)
L = ROUND(K*(1-H)*(1-I)-J, 2)

F = 源商品逐SKU价格
G = 0.10
H = 15% 国补
I = 15% 活动直降
J = 0 元优惠券
K = 京麦发品京东价
L = 预计到手价
```

程序必须同时验证 K、L 公式和 `L=F-G`。本工具不自动报名营销活动；商品发布后由商家批量报名。

## 主图视频说明

京麦页面称“SPU 主图视频”，但当前开放接口保存的是视频与 SKU 的关系：

1. 一个视频文件只上传一次；
2. 保存后等待素材转码完成；
3. 获取类目视频标签；
4. 将同一个视频 ID 分别绑定到该 SPU 的每个 SKU；
5. 查询关系，直到每个 SKU 的 `relationStatus=4`。

审核后的素材状态可能由转码完成 `2` 变为审核完成 `4`，两者都可直接复用。重复运行不得重新上传或重复绑定。

## 安全边界

- 发布前必须核对当前登录店铺与本地白名单配置。
- 正式发布必须显式传入 `--publish --confirm PUBLISH_TO_JD`。
- 任一 SKU 缺价、价格复算失败、销售属性维度不明或类目字段无有效映射时停止。
- 已绑定其他主图视频时停止，禁止自动覆盖。
- 新品接口返回 `productId` 后立即落盘；详情暂时查不到时只轮询，绝不重新发品。
- 本项目不保存 Token、Cookie、App Secret，也不提供绕过平台鉴权的能力。

## 文档

- `references/handoff-manual.md`：完整交接手册
- `references/installation-and-configuration.md`：安装、登录、店铺配置
- `references/modern-decorative-painting.md`：装饰画业务规则
- `references/media-and-video.md`：图片和视频接口流程
- `references/state-machine.md`：状态机、幂等与恢复
- `references/troubleshooting-and-stop-conditions.md`：故障与停止条件
- `references/adapting-new-category.md`：新类目适配方法
- `references/new-conversation-prompt.md`：后续优化可直接复制的提示词

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile scripts\*.py
```

## 许可

MIT License。业务使用仍须遵守京东平台规则以及商品素材、品牌和版权授权要求。
