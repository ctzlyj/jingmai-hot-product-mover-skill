# Changelog

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

