# 图片与主图视频

## 图片

1. 下载 SPU 主图、每个真实 SKU 的首图和全部详情图。
2. 校验文件存在、格式、尺寸和大小。
3. 按内容哈希复用已上传图片，避免同次任务重复上传。
4. 调用图片空间接口上传，记录图片 ID 与 URL。
5. 现代装饰画为每个款式构造完整图片组：专属首图加公共后续图。
6. 发布后按线上颜色 ID 检查每组图片数量、顺序和 URL。

合规校验接口的 `imgUrlList` 最多 20 项；非必填时应省略，不能把全部商品图片塞入该字段。

## 视频采集

源页面存在 `mainVideoId` 时：

1. 查询播放地址并优先选择高清版本。
2. 下载 MP4 与封面到商品目录的 `视频` 文件夹。
3. 校验视频文件存在、时长 5–90 秒、大小不超过 200MB。
4. 视频下载失败时停止，不得静默忽略。

## 视频上传与绑定

当前接口流程：

1. `GET /sp-product/v0/media-zones` 找到视频空间。
2. `GET /sp-product/v0/media-zones/{zoneId}/upload-urls` 获取预签名上传地址。
3. 直接向预签名地址上传 MP4。
4. `POST /sp-product/v0/media-zones/{zoneId}/videos` 保存视频记录。
5. 查询视频列表，等待素材可用。
6. `GET /sp-product/v0/sku-material-tags` 获取类目视频标签。
7. 调用 `GET /sp-product/v0/sku-materials` 查询已有视频素材时，每批最多传 50 个 SKU，并合并所有批次结果。
8. 对每个 SKU 单独调用 `POST /sp-product/v0/sku-materials/batch`。
9. 调用 `GET /sp-product/v0/media-zones/{zoneId}/video-sku-rels` 时按 `paginationData.totalItems` 翻页，汇总全部 SKU 后再判断是否全部为 `relationStatus=4`。
8. `GET /sp-product/v0/media-zones/{zoneId}/video-sku-rels` 轮询关系审核。

### 关键约束

- 业务语义是“一个 SPU 一个主图视频”，技术实现是“同一个视频 ID 逐 SKU 绑定”。视频文件只上传一次。
- 上传地址首次请求省略 `breakPointFlag`；不得发送 `false`，否则网关可能报参数类型错误。
- 绑定请求根节点必须是 `{"skuMaterials":[...]}`，不能使用 FAQ 中的数组根节点。
- 每次绑定调用只提交一个 SKU。
- 素材状态 `2`（转码完成）和 `4`（审核完成）都可直接复用。
- 关系状态 `4` 才表示绑定审核通过；任一 SKU 缺关系、待审或被驳回都不能报告完全成功。
- 查询到 SKU 已绑定其他视频时停止，禁止覆盖。
- 不重复提交审核中的关系。
- 解绑接口即使实际成功也可能返回 HTTP 503，应以关系查询结果为事实依据。

## 无视频商品

源商品没有主图视频时，视频阶段返回空，不影响图片和商品发布。不得伪造视频或把普通详情视频当主图视频。
