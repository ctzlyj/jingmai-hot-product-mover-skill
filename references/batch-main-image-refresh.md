# 整店多套主图批量换新（2026-10-20/21 两店实测）

> 独立版：整店大批量优先用 jd-jingmai-batch-image-refresh（配置驱动、脚本已参数化、含回归测试，另含详情页换图）。本文件保留为搬运流程内的改图路径——「改完图再上架」与「上架后改图」仍按这里的口径执行，接口细节与坑两边一致。

适用：用户给出一家已有店铺的京麦店铺浏览器快捷方式，要求“把这家店的图全改掉”，店铺为多套主图模式（公共组 + 各颜色组），商品多为下架中(103)/审核中(104)。
实测范围：英达齐齐店 23 条（224 槽位，100% 换新）、齐齐店 37 条（1264 槽位，1247 换新、17 保留），两次均全量终验通过。

## 流水线（每店一份任务目录，中间件全部落盘可续跑）

1. 解析用户给的 .lnk（`WScript.Shell CreateShortcut`）拿精确 profile 路径；先按 `references/store-browser-profile-pitfalls.md` 处理登录态。
2. `jdo-cli login --profile <店铺专属 profile> --no-browser --timeout 600`，从 stderr 取授权 URL，用该 profile 启 Chrome 打开（若该 profile 浏览器已在运行，URL 会被现有实例接管），轮询 stdout 出现 `\"ok\": true`。
3. 身份三要素：`venderinfo` 取 venderId/shopName，写入 `identity.json`，`mark_jdo_store_verified`；后续每个写脚本开头再核一次。
4. 全量列表 → 逐商品 `product-apply` 快照（`products/<pid>/before-apply.json`）→ 生成 plan。
5. 下载原图（按 URL 去重后只下唯一图，JPEG/PNG 原字节保留，`PIL.verify()` 校验）。
6. 双模型并发生成（Flare/Sunburst 各 4 并发，约 900 张/h，`size=1024x1024`，参考图按真实格式上传）。
7. 上传到素材中心（>3MB 转 JPEG，单图 <3MB 后再传），记录 imgUrl 与 sha256。
8. PATCH 主图（`image_patch_update.build_image_patch_payload`，按 URL 映射覆盖，组与位号取自线上快照）。
9. 严格终验（见下）+ 逐链接前后并排对比图 + Excel + 总览图。

## 口径

- 提示词必须用户定稿，不自拟卖点；同一商品内所有图共用同一条提示词（标题内嵌）。
- 公共组 0000000000 与各颜色组里相同 URL 的图只生成一次、全组复用（省额度约 60%）。
- 只换主图；详情页、标题、价格、品牌、库存、商品状态不动；无 3:4 矩形图时不触发删除链路。
- 每组位号上限 9：第 10 位提交会被拒，plan 阶段就把位号 >9 的槽位排除，终验时按“保留旧图”计。

## 已验证的坑

1. **生图模型对真人露肤图直接 400 invalid_request**（如沐浴露背、浴刷演示）：Flare/Sunburst 同时拒绝，`Oxygen-Product-Pro` 兜底多数可过；仍被拒时该图槽位保留原图，逐商品登记 `retainedKeys`，不伪造、不本地贴图。
2. **PATCH 重试必须先去对账**：PATCH 已生效但当场校验失败时，下一次重试读到的“旧图 URL”已不存在，映射校验会报 `planned images not fully replaced`，从而把成功误判为失败（齐齐店 10036319609910）。正确做法：重试前重新读取线上图，若目标槽位已全为新图集合，直接判成功，不重发。
3. **组 uuid 会在 PATCH 后被平台重建**：终验不要用 plan 里的 uuid 对齐，用「组数不变 + 槽位数不变 + 每个 URL 归属（新图/允许的旧图）+ 每组至少一张新图」的口径。
4. **子进程会逐商品弹黑窗**：`jdo_listing_mvp` 用 `subprocess.run` 调 `jdo-cli`，批量脚本不处理控制台标志就会持续闪黑框、干扰用户工作。批量脚本开头必须 `import win_no_window`（见 `scripts/win_no_window.py`），把所有子进程加上 `CREATE_NO_WINDOW` + `STARTF_USESHOWWINDOW/SW_HIDE`。
5. **驱动脚本一律 Python**：PowerShell 只做 `Start-Process` 起后台任务；含中文字面量的 .ps1 必须 UTF-8 BOM。
6. **apply 体的 productStatus 是对象**：{"productStatus":8,"yn":1}（8=草稿态），与列表接口的 103/104 不同源。终验状态一律以 /rest/sp-product/v0/products 列表为准，apply 体只比对改前改后是否变化。
7. **原图下载要双 CDN 回退**：jfs/t1/... 在 img10.360buyimg.com/image/ 不一定可读，imgzone/ 更稳；两个候选依次重试并做 PIL.verify()。
8. **任务 key 按「商品+URL」作用域**：两个商品复用同一张源图时必须各生成一张（提示词内嵌各自标题），只按 URL 做 key 会让后者覆盖前者；原图文件仍按 URL 去重。

## 终验门槛（全部满足才可报完成）

- 组数不变、每组槽位数不变；计划内不再残留源图 URL；
- 新图去重数量 = 计划图数 - 模型拒图数；每组至少一张新图；
- 保留旧图仅限「位号 >9」与「模型拒图」两类，且数量与 plan/台账一致；
- SKU 价格零变更、无矩形图、商品状态与改前一致；
- CDN 抽检：`imgzone` 返回 1024×1024，与本地成品像素均差 ≈1~2（仅重编码差异），不是字节哈希相等。
