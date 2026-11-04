# 同款矩阵换主图上架

适用：用户给出本店一条已在售链接，要求「同款多链接 / 矩阵铺货 / 换主图再上几条」。新链接沿用原链接的全部商品数据，只把主图整组重新设计。2026-11-03 已实测：坐便椅类目、20 SKU、无品牌，5 条矩阵链接全部在售并通过逐 SKU 回查与视频审核。

不适用：跨店搬运、外部来源采集（走主流程）；只改已有链接的图（走 `jd-jingmai-batch-image-refresh`）。

## 流程（`scripts/matrix_listing.py`，一个原链接一个 run 目录）

店铺参数 `S` = `--profile pop-xxx --vender-id <venderId> --shop-name "<店铺名>"`（或只给 `--profile`，用 `config/store.local.json` 核对）。

```text
matrix_listing.py snapshot --run <RUN> --source-product-id <原商品ID> S
  （复制 references/matrix-variants.example.json 为 <RUN>/variants.json 并按类目改写 suffix）
matrix_listing.py generate --run <RUN> --variant m1
matrix_listing.py upload   --run <RUN> --variant m1 S
matrix_listing.py build    --run <RUN> --variant m1
matrix_listing.py validate --run <RUN> --variant m1 S
matrix_listing.py publish  --run <RUN> --variant m1 S --confirm PUBLISH_TO_JD
matrix_listing.py verify   --run <RUN> --variant m1 --profile pop-xxx
matrix_listing.py video    --run <RUN> --variant m1 S --confirm BIND_SOURCE_VIDEO
```

多个风格可并行 generate；京麦写操作（upload/validate/publish/video）按风格串行。

1. **snapshot**：先核店铺三要素，再读原链接 `product-apply/{applyId}`（不是 `products` 接口，后者缺主图组/白底图细节）。主图从 `https://img10.360buyimg.com/imgzone/` + `imgUrl` 下载上传原件；禁止 `/n0/`（带京东水印）。
2. **generate**：只用公司 `GPT-Image-2.5-Flare-joybuilder` / `GPT-Image-2.5-Sunburst-joybuilder`，按图交替首选、失败换另一款，8 并发；不用 2.0、不用 Oxygen-Product-Pro，不擅自外部回退。提示词 = `hot_replica_mode.build_dynamic_prompt(title)`（用户定稿，禁止改写）+ 本链接风格 suffix + 通用硬性要求（suffix 已自带时不重复）。`keep` 中的资质证书图保留原图，不重做。
3. **upload**：生成图统一 JPEG 后上传图片空间；`uploads.json` 记 sha256，同字节不重复上传，替换上传时把旧 URL 记入 `previous`（供线上补丁用）。
4. **build**：沿用原链接标题、详情、类目属性、包装长宽高重、七天无理由、运费模板，以及每个 SKU 的京东价、商家编码、库存、短标题、SKU 属性、白底图（默认 `--white-background copy`）。颜色组 `mappingId` 用 `sha256(原商品ID|风格|原attrValues)` 前 32 位，不复用原 attrValues；技术货号 `MX<风格>-<原商品ID>` 做幂等。
5. **validate**：合规校验，强违规（detectResult=1）停止；同时按货号查重。
6. **publish**：先确认 `--confirm`，再查 state 与货号；提交前写 `SUBMITTING`，回执立即落盘。已有 `SUBMITTING`/`productId` 时只回查对账，绝不重提。
7. **verify**：apply 刚建好时 `draftComputeStatus=1`、`products` 接口可能暂报「未查询到商品信息」，按间隔轮询；`=3` 立即报驳回。逐 SKU 校验价格、规格名、库存、主图组、白底图和详情，并确认原链接主图与价格未变。
8. **video**：只复用原链接上**唯一且审核通过**的主图视频与原标签（实测标签 1383「产品展示」），逐 SKU 单独绑定；已绑其他视频即停；全部 `relationStatus=4` 才算完成。

## 风格要求（用户确认）

- 整张重新设计：背景、构图、版式、卖点标签全部换，与原图及其他矩阵链接一眼可辨；只换背景被用户判为太保守。
- 商品结构、颜色、材质、配件、数量、比例必须与原图一致。

## 已踩坑与处理

| 现象 | 根因 | 处理 |
| --- | --- | --- |
| 局部特写图被补画出座板/靠背，颜色错 | 模型补全画面外部件 | 通用硬性要求已限制「只画原图出现的部分」；仍出错的单张在 `imageNotes` 写明裁切范围后重生成 |
| 「免安装」→「免费安装」、「五档」→「两档高度」、乱码「陪高」 | 模型改字；原图日文小字干扰 | 生成后必须人工/视觉抽检文字；问题单张在 `textLocks` 写「只允许出现的文字」，`generate --only <stem> --redo` |
| 已上线的图有错字 | — | `upload` 后 `patch-fix --stem <stem> --confirm PATCH_PRODUCT_IMAGES`：按 URL 只换该槽位（`image_patch_update.patch_product`），PATCH 后回查；已是新图时只对账不重发 |
| ledger 写盘 WinError 13 整批崩 | Windows 杀毒/备份锁文件 | `save_json_atomic`：tmp + `os.replace` + 重试；重跑时 `rebuild_ledger` 按磁盘已有图恢复，不重复生成、不重复扣额度 |
| 新链接缺白底图 | 早期脚本未复制 | 默认复制原链接已审核白底图（status=4），verify 逐 SKU 回查 |

## 风险提醒（交付时告知用户）

标题与价格与原链接完全一致，存在被判重复铺货的风险。是否给每条链接改写标题、错开价格，由用户决定；未决定前不要擅自改。
