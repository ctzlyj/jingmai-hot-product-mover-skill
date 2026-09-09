# 状态机、幂等与恢复

## 状态文件

每个源 SKU 对应：

```text
outputs/migrations/state/<sourceSku>.json
```

商品目录保存 `manifest.json`、`listing.json`、审核 Excel、素材与 `.pipeline` 中间证据。状态文件必须在服务端返回关键 ID 后立即写入。

## 主要阶段

```text
RECEIVED
STORE_VERIFIED
LOCAL_DRAFT_READY
LOCAL_VALIDATED
MEDIA_UPLOADED
LOCAL_DRAFT_BUILT
JD_VALIDATED
PUBLISHED
PRODUCT_PROPAGATION_PENDING
PRODUCT_APPLY_REJECTED
PRODUCT_VERIFIED
VIDEO_SAVED
VIDEO_TRANSCODED
VIDEO_BINDING
VIDEO_REVIEW_PENDING | VIDEO_VERIFIED
VERIFIED
```

批次级可继续的终止状态：

```text
SKIPPED_OUT_OF_SCOPE_CATEGORY
SKIPPED_AI_DUPLICATE
```

`SKIPPED_AI_DUPLICATE` 表示 AI 对完整商品语义作出的重复上品结论，不代表标题字符串匹配。两种跳过状态都记录原因并继续下一条，不进入素材上传或商品创建。

用户随后明确改判或授权类目映射时，任务可从跳过状态恢复；进入 `RECEIVED` 后必须清除顶层旧 `categoryDecision`、`preflightDecision` 和 `duplicateReview`，避免最终状态仍携带过期跳过结论。原阶段继续保留在 `history` 中。

普通失败记录 `FAILED` 和错误文本，但保留此前的 `productId`、`applyId`、`videoId` 与 `jdVideoId`。新品接口已返回 `productId`、但详情仍返回“未查询到商品信息”时，必须先查申请单详情：`draftComputeStatus=3` 记录 `PRODUCT_APPLY_REJECTED`；申请未驳回且有限轮询耗尽才记录 `PRODUCT_PROPAGATION_PENDING`。两个阶段都不得被外层通用异常处理覆盖成 `FAILED`。

## 幂等键

- 商品：源 SPU 写入商品货号；创建前使用商品列表接口按货号查重。
- 业务判重：目标类目过滤后，由 AI 比较店铺已有商品和同批更早候选；标准化标题、哈希或字符串规则不得直接产生重复结论。
- 视频：状态文件保存视频空间、商家视频 ID 与通用视频 ID；复跑先查询已有视频。
- 视频关系：先查询每个 SKU 已有素材；相同视频跳过，其他视频停止，无视频才提交。
- 图片：同一执行中按内容哈希复用上传结果。
- 并发：每个源 SKU 使用独占锁文件，防止两个进程同时处理。

## 恢复规则

- 新品接口已返回 `productId`：只轮询该商品，不得再次创建。
- 商品详情返回“未查询到商品信息”：先查 `/product-apply/{applyId}`；`draftComputeStatus=3` 立即停止并保留 `PRODUCT_APPLY_REJECTED`，其他状态才进入有限传播轮询。达到上限后保留 `productId` 并写入 `PRODUCT_PROPAGATION_PENDING`，后续复跑只回查该 ID。
- 视频素材状态为 `2` 或 `4`：继续关系检查，不重复上传。
- 视频关系待审：轮询；达到上限后保存 `VIDEO_REVIEW_PENDING`，后续复跑继续查询而不是重复提交。
- 最终状态只有在商品审计与视频审计都满足时才写 `VERIFIED`。

## 批量行为

批量文件每行一个 URL。按顺序处理；`SKIPPED_OUT_OF_SCOPE_CATEGORY` 和 `SKIPPED_AI_DUPLICATE` 记录后继续，其他失败停止后续商品。批次结束统一输出已完成数量、失败原因和跳过清单；AI 重复项包含源 SKU/标题、匹配对象及理由。已完成商品可凭状态文件安全复跑。
