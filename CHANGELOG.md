# Changelog

## 1.2.6 (2026-11-11)
大请求桥去掉对 CLI 版本/运行时指纹的整体拒绝，小请求不再依赖系统 Python（P0-1）。
- 小请求（命令行放得下）直接走官方 CLI，不需要系统 Python 3.12，也不提取 CLI 内部运行时；只有超过命令行上限的大请求才走桥。
- 桥不再按 sha256/CLI 0.3.4 拒绝：遇到没见过的运行时，子进程探测 `cmd_invoke` 与所需字段，通过即放行，按指纹缓存；不通过才提示“CLI 改变了大请求的发送方式”。已在 CLI 0.3.4 与 0.3.7 上验证。
- 自检：大请求桥不可用不再让 `environmentReady=false`；改为 `capabilities.largeRequests` 与 `nextActions`(large-requests)，只用人话说明受影响（发品、整版编辑、属性补全、素材绑定）、不受影响（查询、审计、授权、本地生成）和下一步。
- 结论与取舍：方案甲（CLI 自带环境/拆分 body）不可行；方案乙（自签名）需读取 AppSecret/Token，未经负责人确认不做。3.12 宿主仍是大请求前提，见 `references/large-requests-and-cli-versions.md`。
- vendor/jdo-cli 仍是 0.3.4，属于 jingmai-cli-linker 的改动，本仓库不改，建议见上述文档。

## 1.2.5 (2026-11-08)
- 仅测试修复：`test_video_handoff` 的试听确认用例不再依赖本机是否装有 11190（1.2.4 包内在未安装 11190 的机器上有 2 个用例失败）。功能与 1.2.4 相同。

## 1.2.4 (2026-11-08)
接入主图视频 11190@1.1.2 的安全交接检查。
- `photo_video.py` 绑定前强制：只读 `manifest.video.localPath`（不扫目录），`--mp4` 必须等于它；调用 11190 `pop_video.py verify-handoff`（非 0 即停）；再独立核 `bindable`/`mode`/`label`/`taskId`/`qc` 与视频、封面 SHA256。检查通过之前不探测文件、不核店、不访问平台。
- 试听确认：`voiceReviewRequired=true` 时须用户原话「已试听，确认绑定」或「接受口播风险，确认绑定」，记录 `manifest.video.voiceReview`（原话、时间、视频 SHA）；SHA 变化即失效。
- 旧版（1.1.0）视频包缺 `sha256`/`runId`/`voiceReviewRequired`：不绑定，`--diagnose` 只读诊断并给迁移提示；不提供手工写 SHA 的路径。
- 上传前、传输后、保存前、绑定前复核源文件 SHA；传输中被替换则标记传输不明并停止，不保存、不绑定。

## 1.2.3 (2026-11-07)
真实验收（千百日用品质店店，商品 10036442406541）暴露的缺陷修复。
- 在架判断：接受真实在架值 `productStatusNew=103`（和 `1`）；104/105 等不算在架。1.2.2 把 103 误判为未在架。
- 结构化销售属性：支持 `templateSkuProps`（`styleTemplateSkuProps`/`sizeTemplateSkuProps`）；新增 `discover-sale-templates` / `apply-sale-templates`；官方接口无权限时从同店同末级类目在架商品只读发现框结构；无法确定则发布前停止并一次提问；缺 `templateSkuProps` 时 `build_listing` 阻止发布。
- 默认主图/详情限制：只画实拍商品、不加非售卖物、不写未确认宣传语/承重/认证/功效/价格促销、规格层数颜色与 SKU 一致；白底首图明确“不要任何场景”；`facts.includedItems` 可放行用户确认的赠品。
- 发布结果防重复：平台返回错误码记为 `REJECTED`（保存原文）；`REJECTED`/`UNKNOWN` 必须先 `confirm-not-created` 只读回查（货号+同标题）才允许再次提交；回查到商品则接管。
- 文档：平台预校验通过不代表结构化销售属性正确。

## 1.2.2 (2026-11-07)
- 首发前自动生成 6–8 个竖版详情模块，覆盖全部 SKU；发布前强制门槛；发布后 PC/mobile 回读；`detail-repair`。

## 1.2.1 / 1.2.0
- 实拍图上新三款公司 GPT 模型生成与评审发布流程。

