# 实拍图上新：公司 GPT 三模型规则

状态：已提供实拍图上新执行入口，当前真实样例停在预览/缺项确认阶段，尚待真实商品完整上架验收。不得把预览、mock 测试或旧矩阵模式称为实拍上新验收成功。

## 仅允许以下三款

2026-11-05 已只读核对公司网关 /v1/models，准确名称为：

- `GPT-Image-2.5-Flare-joybuilder`
- `GPT-Image-2.5-Sunburst-joybuilder`
- `GPT-image-2-joybuilder`

仅通过公司网关调用。暂时禁止调用白名单以外的生图模型：不转 Oxygen-Product-Pro、Oxygen-Imagen、灵境其他生图模型或 sufy-image2 外部付费生图。第三款是 GPT-image-2，不要写成 Oxygen-Product-Pro，也不要猜大小写或别名。

这条选择是用户对实拍图上新模式的明确要求，不改变旧矩阵模式仅用 Flare/Sunburst 的配置，不改变旧爆款复刻动态提示词或衔接 suffix。

## 最快可用并发，不串行降级

- 三款独立并发：分别维护模型队列、并发槽和启动限速；有空闲槽即分配任务，不按“三款依次跑完”处理，也不共用一个总共 4 并发的队列。
- 当前已有公司客户端配置为每款最多 4 并发、每款每秒启动 1 个请求。可据此安排三款合计最多 12 个在途任务；这是客户端上限，不代表服务端一定允许 12 并发。执行器必须按当前有效配置和网关许可使用最大可用吞吐，不能以无限并发或无效重试追速度。
- 任务可按可用槽动态调度，成功图缓存；同一任务不可重复提交，只重做用户选中的单张。为追速度不得降低用户要求的画质、商品一致性和文字正确性。
- 429 尊重 Retry-After 并调整对应队列；不绕过网关限流。每图记录实际模型、排队/生成耗时及失败原因（不含凭据）。

## 失败仅在三款内部切换

- 不得直接沿用 SDK 默认降级链：现有默认链可能包含 Oxygen-Product-Pro。实拍执行器必须显式设置并校验这三款白名单。
- 明确失败且确定未产出的任务，可转另一个白名单模型；向用户说明原模型、实际切换模型和原因。
- 超时、断流或结果不明先核对任务结果/回执/计费，不立即换模型重复扣费。
- 三款均不可用则保留已完成图片、报告暂不可用并等待恢复，不自行启用其他模型；用户以后重新授权其他模型时再修改规则。

## 后续实现验收

须补运行时测试验证：拒绝第四款模型、三模型真正重叠执行、失败切换不越白名单、结果未知不重放、单图重做不影响成功图。已补对应执行器回归；通过 mock 不代表真实商品已上架。


## JoyCode 操作顺序

助手定位实际 Skill 安装目录（支持 .joycode/.codex/.agents/skills、skill-<id>），先运行 bootstrap 自检。用户发来的实拍附件要保存成本批文件，不可把其他商品或参考图混入实拍输入。

1. 问一次有没有参考京东链接，记录回答；用户已给链接就不再问。确认测试店，调用“京麦 CLI 店铺连接”确保授权，不复制账号凭据。
2. init 建包；reference 只读采集参考风格，助手查看缓存图总结版式、配色、卖点结构（仅文字 styleSummary）。原图仅在 .reference-cache 分析，绝不作为生成输入或发布素材。无参考时由助手设计统一风格。
3. 识图与实时模板：展示商品名、候选类目和照片可证实的卖点。运行 refresh 获取当前店铺类目、品牌、材质等必填字段；questions 把缺项集中问一次。实际品牌名称解析为本店授权 brandId；无品牌仅在模板允许 -1 时可用。尺寸/重量/材质等不能从参考商品复制。看不清或归属不明的配件需要补拍/确认。
4. 用户回答后 set-facts；facts.confirmed=true 仅可在用户确实给出/确认事实时设置。库存与价格逐 SKU 指定，不应用竞品折扣公式。SKU 个数来自用户真实清单，不做笛卡尔积、猜缺项或丢弃 SKU。saleDimensions 最多两维（复用已有发布 Schema 能力）；更多维度明确停止，不截断。
5. make-plan→generate：三模型独立并发、动态派空闲槽；生成白底首图、其他主图、每个真实 SKU 图、3 张竖版详情模块，按顺序拼成长图。make-plan --preview 可先做不含未确认参数的三张外观草稿；previewOnly=true 时不能上架，不生成不确定的 SKU/参数图。
6. review 输出 preview.html 和确认清单.md；助手逐图核对商品结构、颜色、文字和配件，告知用户实际模型切换情况。已知问题登记在 manifest.qualityProblems，未修正不允许 accept-images。用户可指定一张 generate --redo <任务ID>，不重跑已成功图；资料或图片变更会使确认摘要失效。
7. 用户接受图片后 accept-images；prepare 上传一次素材、构建 Schema、调用平台 validations，遇到强违规停止。prepare 会产生图片空间写入，但不创建商品。给用户正式发布确认清单和当前 reviewDigest，再确认后 publish。没有用户确认不能为了实测而填写确认字符串。
8. verify 分别读取申请、正式商品和库存专用接口，核对商品/SKU/价/库存/主图组/详情；draftComputeStatus=3 停止并报驳回。VERIFIED 才表示上架回查通过，且此时 manifest 填真实 productId/skuIds、商家码、图片路径/线上 URL。

命令前缀为实际安装目录下 `.venv/Scripts/python.exe`：

```text
scripts/photo_listing.py init --package <RUN/listing-package> --shop <店名或真实快捷方式.lnk> --photos <实拍1> <实拍2> <实拍3> --title <草稿标题> --reference <京东链接>
# 无参考用 --no-reference，二者不能同时给
scripts/photo_listing.py reference --package <PACKAGE>
scripts/photo_listing.py refresh --package <PACKAGE> --category-id <叶子类目> --category-name <类目名> [--brand-category-id <品牌查询三级类目>]
scripts/photo_listing.py questions --package <PACKAGE>
scripts/photo_listing.py set-facts --package <PACKAGE> --facts <已确认facts.json> [--style <style.json>]
scripts/photo_listing.py make-plan --package <PACKAGE> [--preview]
scripts/photo_listing.py generate --package <PACKAGE> [--redo <单张任务ID>]
scripts/photo_listing.py review --package <PACKAGE>
scripts/photo_listing.py accept-images --package <PACKAGE> --confirm PHOTOS_REVIEWED --review-digest <当前摘要>
scripts/photo_listing.py prepare --package <PACKAGE> --confirm PREPARE_PHOTO_LISTING --review-digest <当前摘要>
scripts/photo_listing.py publish --package <PACKAGE> --confirm PUBLISH_TO_JD --review-digest <当前摘要>
scripts/photo_listing.py verify --package <PACKAGE>
scripts/photo_listing.py reconcile --package <PACKAGE>
```

## 确认数据 facts.json

助手根据用户真实回答整理，不提交到 Skill 仓库。至少包括 confirmed、title、categoryId、brandId、sellingPoints、packageLengthMm/packageWidthMm/packageHeightMm、weightKg、freightTemplateId、sevenDayReturnCode、categoryAttributes（实时 componentCode/valueIds）以及 skus 数组（key/spec/jdPrice/stock/skuAttributes）。可以提供 saleDimensions、每 SKU 的 attributes、sourcePhotos（本包的实拍相对路径）和自有 vendorSku。缺项以 null 标记，不能用示例参数填真实商品。

## listing-package 交接契约

manifest 的 schemaVersion 为 listing-package/v1，顶层含 store/Profile/venderId/shopName、productId、skuIds、category、title、sellingPoints、mainImages、detailImages、variants、video、timings、verification。图片为 {localPath, sha256, model, onlineUrl/imgUrl}，variants 含 key/spec/vendorSku/jdPrice/stock/skuId/skuImage。状态 INFORMATION_PENDING/MATERIAL_PREVIEW/PREPARED_AWAITING_CONFIRMATION/PUBLISHED_UNVERIFIED/VERIFIED 等明确区分；ID 未产生时用 null/空数组，不伪造。

后续视频与信息分 Skill 只读 manifest 和本地图片，不再问价格/规格/卖点。商品发布后可以直接出片；图片未通过/ID 尚未核验时，不得对店铺做信息分或视频绑定。视频生成 Skill 可追加 manifest.video，不能覆盖本包其他字段。

## 本地视频绑定入口

```text
scripts/photo_video.py --manifest <PACKAGE/manifest.json> --mp4 <本地.mp4> --confirm BIND_MAIN_VIDEO
# --mp4 可省略，读取 manifest.video.localPath
# 未知视频保存结果：先只读核对原 jdVideoId，再用匹配的 --resume-video-id 恢复，绝不再上传
```

复用现有视频上传参数、保存视频、标签、逐SKU绑定请求体和审核函数。要求包状态 VERIFIED、商品确属当前授权店铺且 SKU 集合完全相同；已有其他视频在上传前就停止。转码/审核/关系未知只读取原视频记录；每项 POST 前记提交状态，避免超时后重放。输出写 manifest.video.binding，只有所有 relationStatus=4 才 VIDEO_VERIFIED。

## 验证与限制

回归：test_photo_generation.py、test_photo_contract.py、test_photo_listing.py、test_photo_video.py；再运行全量 unittest 和 compileall。
目前已实测公司三款模型生成外观预览和单图重做；完整真实上架、详情整套与真实 MP4 绑定仍需要用户补齐参数、确认图片/发布和提供视频，不能据此宣称完成业务验收。
