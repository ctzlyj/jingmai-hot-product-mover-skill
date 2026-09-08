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
PRODUCT_VERIFIED
VIDEO_SAVED
VIDEO_TRANSCODED
VIDEO_BINDING
VIDEO_REVIEW_PENDING | VIDEO_VERIFIED
VERIFIED
```

失败时记录 `FAILED` 和错误文本，但保留此前的 `productId`、`applyId`、`videoId` 与 `jdVideoId`。

## 幂等键

- 商品：源 SPU 写入商品货号；创建前使用商品列表接口按货号查重。
- 视频：状态文件保存视频空间、商家视频 ID 与通用视频 ID；复跑先查询已有视频。
- 视频关系：先查询每个 SKU 已有素材；相同视频跳过，其他视频停止，无视频才提交。
- 图片：同一执行中按内容哈希复用上传结果。
- 并发：每个源 SKU 使用独占锁文件，防止两个进程同时处理。

## 恢复规则

- 新品接口已返回 `productId`：只轮询该商品，不得再次创建。
- 商品详情短暂返回“未查询到商品信息”：视为索引传播延迟，继续有限轮询。
- 视频素材状态为 `2` 或 `4`：继续关系检查，不重复上传。
- 视频关系待审：轮询；达到上限后保存 `VIDEO_REVIEW_PENDING`，后续复跑继续查询而不是重复提交。
- 最终状态只有在商品审计与视频审计都满足时才写 `VERIFIED`。

## 批量行为

批量文件每行一个 URL。按顺序处理，任一商品失败立即停止后续商品，并输出已完成数量和失败原因。已完成商品可凭状态文件安全复跑。
