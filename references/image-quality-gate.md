# 来源首图与整商品图片质量门禁（2026-09-24）

适用于无货源表格批次和有淘宝素材的后续新链接。当前实测只覆盖本批已确认的故障样本及离线回归；**尚未据此宣称线上图片全部修好**。来源是商家 xlsx 的“主图1”（若实时淘宝首图与表格不一致，先核对来源版本，不能宣称两边实时一致）。不得动用户工作标签。

## 已复现的漏点

- 旧审核只问单张图上的违规词，漏“专利”，没有“删页/信息价值/字体/排版/近重复/来源首图保真”维度。lite 已报问题时，flash 报干净可覆盖成 `CLEAN`；旧成功记录按路径复用，提示词或图片变更也不会失效。
- 案例：专利证书被重绘保留，文字乱码图被判干净，两张近重复详情各自被判干净，一张来源首图的正常文案被全删，另有信息页出现大面积空洞。来源和审核路径保存在任务批次证据中，不将店铺 ID、个人路径和素材搬进通用 Skill。
- 本批已知五个缺陷的只读小样本双图测试，lite 与 flash 均识别 5/5；这不等于总体准确率或线上全量通过。故先修流程与验收，再评估换模型。

## 路由与验收

1. 按来源表“主图1”固定原图，建立**每一张拟用图**的 `role/source/final` 配对和文件哈希。主图第一槽 `main1` 必须来自该来源首图，不许使用别的场景图替代；合规处理只针对小块违规，未违规文案、排版、字体、构图、商品本体尽量保持一致。小面积违规请生图模型局部编辑（不能本地 PS 掩码），修后重验；未违规的主图不要删文案。
2. `gemini-2.5-flash-lite` 对来源图（已修改时）和拟用图全量初筛：原图若本就是专利证书/平台专属/价格活动页，不能靠重绘把它伪装成有价值详情页，仍应直接删除。对拟用图全量初筛：专利图、淘金币、价格/活动说明、天猫明日达等平台专属图、品牌信息、公司介绍、源头工厂、纯低价值图直接 `DELETE`；违规过密非首图也 `DELETE`。**首图命中删除条件则 BLOCK 整件并准备同源可用修复方案**，不拿其它图顶首图。`flash` 只复核命中和看不清的图；lite/flash 有冲突不得直接改为干净。正常详情页若小范围违规才 `EDIT`。
3. 对保留图做来源与结果双图比对：商品相同、违规点消除、正常文字与字体/布局保留、无乱码错字/裁断/留白空洞；任一失败 `BLOCK`。对整商品详情做哈希/近重复检查，含可疑项不自动删，审定后从候选中剔除；已删项不计入待上架数量。若产生删除，保持保留页原有相对顺序及 SKU 归属，**不强制原图数量一致**。
4. `scripts/ni/image_quality_gate.py` 输出按源图/结果图 SHA-256 绑定的 `reports/image-quality-acceptance.json`。`listing_batch.py prepare`、`validate`、`publish` 检查每张候选图的覆盖、源首图、配对结果、近重复与哈希；缺报告、结果不合格或修改后报告过期均先拦截，不能因单图 `CLEAN` 放行。`imageQualityGate` 新批次默认 `strict`；只有此前已建立且明确注明历史兼容的批次才可显式标 `legacy`，不能给新链接关闭门禁。

清单样例（使用批次根目录下相对路径；`source` 必须是当前来源表落盘原图，`final` 是 **最终会上传** 的 candidate 实际图片路径（如 `candidates/<item>/normalized/`，未需归一化时可能是 `images/compliant/`）；若详情删页则清单只包含保留页）：

```json
{"items":{"<source-item>":{"sourceFirst":"images/raw/<source-item>/main-01.jpg","images":[
  {"role":"main1","source":"images/raw/<source-item>/main-01.jpg","final":"candidates/<source-item>/normalized/main-01.png"},
  {"role":"detail","source":"images/raw/<source-item>/detail-01.jpg","final":"candidates/<source-item>/normalized/detail-001.png"}
]}}}
```

`main`（其它主图）和 `sku`（SKU 实际图，fallback 到首主图时不用重复一份）也需逐张列入。图片删减或重排后必须根据真正来源原图重配，不按剩余位置猜编号。运行顺序：先 `listing_batch.py prepare` 生成候选（无质量报告时本地校验失败是预期结果），整理上述来源映射，调用 `python scripts/ni/image_quality_gate.py --root <batch> --manifest <json> --out <batch>/reports/image-quality-acceptance.json`，处理 `BLOCK/EDIT/DELETE` 并重新审核，随后运行 `listing_batch.py recheck --root <batch>` 复检现有候选，达标后再 `validate/publish`。**删图以后禁止重跑 prepare**：它会按旧素材映射重新构建，可能把已删详情图带回。视觉模型密钥仅用 DPAPI 凭据；脚本不上传、不改图、不触碰线上或浏览器。

## 线上已存在图片的边界

先对当前店铺与商品做只读身份/状态回查，再对每一张线上主图和详情配来源图建立差异单；用户自行下架的不代上架。仅有源表首图与上线首图配对并逐 SKU 独立回读，才可声称线上已修正。已实测 `WareSkuImg` PATCH 减少 value **不会真正删除尾部槽**；`--allow-count-change` 只放开本地差异，不提供删除能力。用户不接受用干净图补尾部制造重复，真正删除接口未验证时停止该商品的线上删除，列待办，不把替图或 PATCH_SUBMITTED 当成功。详情删页需要单独回读远端详情图序列并确认无重复。写操作前仍需 A.identity() 三要素核对、dry-run/回执/reconcile；未经线上写授权不得执行。
## 淘宝新链接

淘宝/天猫新链接由 `taobao_product_mvp.py` 标记 `imageQualityGate=strict`，素材落盘时将首图、详情和 SKU 图保留在草稿 `source-original/` 以免在编辑同一路径后丢失原件。对草稿运行 `python scripts/ni/image_quality_gate.py --root <draft-folder> --from-listing --out <draft-folder>/image-quality-acceptance.json`；`publish_local_draft.py` 在验店/上传前逐图校验，不通过即停止。删页后从 listing 的 images 中移除该页并重新审计；若编辑后的文件换了路径，先在 `manifest.json.sourceImageSnapshot` 中明确映射新路径到原件，再重审，不允许按剩余序号猜测。老草稿未带该标记时不追溯为通过，应依原任务范围另行检查。
