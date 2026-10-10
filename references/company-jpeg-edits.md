# 公司生图 JPEG 恢复与复用

适用：用户选择公司 Flare/Sunburst 改已有链接主图，且 PNG 生图虽收到 HTTP 200，却发生响应截断（如 ChunkedEncodingError/IncompleteRead）。2026-10-11 同参考图、同定稿提示词、同客户端对照，PNG 失败、JPEG 成功；另一次单商品 5 张 JPEG 全成功，并完成上传和正式体/编辑体/SKU首图/列表封面独立回查。具体传输截断节点尚未确认，不应归因为 VPN、预算或提示词。

- 保持 `hot_replica_mode.build_dynamic_prompt(title)` 原文，不能为了恢复服务自行改提示词。
- 公司 `GPT-Image-2.5-Flare-joybuilder` 与 `GPT-Image-2.5-Sunburst-joybuilder` 可交替分配，共享最多 4 并发、每秒最多启动 1 请求；JSON `/images/edits` 明确指定 `output_format=jpeg`，输出使用 `.jpg`。参考图按真实格式发送，保留原字节。
- `scripts/company_jpeg_edits.py` 是独立公司生成适配器，不上传、不改品、不自动用外部平台。Key 只从当前进程 `JD_LLM_API_KEY` 读取。不要修改官方安装包。
- plan 含 `title`、用户定稿 `prompt` 与去重后的 `sources[]`：每项 `sha`（稳定素材标识）、`source`（已授权本地原图路径）、`model`（上述公司模型）。先 `--plan <plan.json> --output <任务输出目录> --dry-run`；再不带 `--dry-run` 实际生成。工具拒绝非定稿 prompt、外部模型、重复标识和不同计划复用旧输出目录。
- 一种款式不同尺寸共用同一结果；不同款式首图分别映射，重复后续图复用，不改变图序或 SKU 归属。用户只要求主图时不要改详情。
- `generation.json` 成功结果按 SHA256 校验复用。已尝试失败、请求开始后未取得结果都停止，不自动重放或换平台；先核对产出及计费。凭证/原始错误体不落日志。
- 上传仍走京麦官方 CLI，每次写前三要素身份核对。只提交图片组件，保持详情、标题、价格、库存、编码、类目、品牌和状态；一次 PATCH 后用正式体/编辑体核对全部图序与 SKU 首图，列表封面作附加确认。结果未知先回查，绝不重发。

验证：`python -m unittest discover -s tests -p test_company_jpeg_edits.py -v`，再 `py_compile` 与上述 dry-run；不因 HTTP 200 或本地生成成功声称店铺已替换。不做事后视觉返工，除非用户指出问题或平台驳回。
