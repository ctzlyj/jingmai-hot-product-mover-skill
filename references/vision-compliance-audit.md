# 图片视觉合规检测（两档模型）

## 触发条件

- 淘宝/天猫/拼多多来源商品完成修图或合成后、正式发布前的本地内容合规预检；京东来源复用他人素材或用户明确要求图片合规检测时同样适用。
- 文字类违规（品牌/平台词/极限词/价格促销/榜单水印/联系方式）以视觉模型检测为准；文件名、OCR 单独结论不能作为通过依据。

## 固定两档模型策略

1. **初筛全量**：`gemini-2.5-flash-lite` 审核所有图片（主图、SKU 图、详情图），并发默认 10。
2. **复核只做两类图**：初筛命中违规、质量疑点、初筛失败或 JSON 不可解析（“看不清/不确定”）的图，用 `gemini-2.5-flash` 复核一遍，其余图片不再二次调用。
3. 终态规则：复核成功仍保留初筛专利/平台专属/价格说明等强制删除证据；两档冲突不可直接记 `CLEAN`；复核仍失败记 `UNVERIFIED`，不得当作通过，进入失败/待处理清单。初筛干净记 `CLEAN`。

选择依据（2026-09-23 本机实测，样本见会话记录）：`gemini-2.5-flash-lite` 单张 2~4 秒、价格约为 `gemini-2.5-flash` 输入的 1/3、输出的 1/6，中文图上文字识别与初筛能力未观察到下降；`glm-5.3-flash` 虽更便宜但 14~77 秒/张，`doubao-seed-2-1-turbo` 出现 500/超时，`deepseek-v4-flash-vision-exp`、`gpt-5-mini` 输出被 900 token 截断导致 JSON 解析失败，均不采用。

## 执行命令

```powershell
# Key 只放在进程环境变量，通过 DPAPI 读取，不写入任何文件/日志/源码
$env:LTS4AI_API_KEY = & powershell -NoProfile -ExecutionPolicy Bypass -File `
  "$env:USERPROFILE\.joycode\skills\sufy-image2\scripts\read_dpapi_credential.ps1" `
  -CredentialPath "$env:LOCALAPPDATA\JoyCode\credentials\sufy-image2.dpapi"

python scripts/vision_compliance_audit.py `
  --list images.txt `
  --out vision-audit-primary.jsonl `
  --review-out vision-audit-review.jsonl `
  --summary vision-audit-summary.json
```

脚本：`scripts/vision_compliance_audit.py`（依赖 requirements.txt 里的 Pillow）。`--list` 每行一个图片绝对路径；输出 JSONL 按 `path` 幂等续跑，重跑只补初筛未成功的图片（成功图片不重复计费，网络抖动失败的会自动重试）；退出码非 0 表示存在 `UNVERIFIED`。

## 停止与回退条件

- 无可用 LTS4AI Key 且用户未授权配置：不检测放行；回退为生成 contact sheet 由助手人工目检，并在交付说明中标注“未经视觉模型审核”。
- API 连续 5xx/超时：停止本档调用，保留已写入记录，向用户报告失败比例，不把失败图片记为干净。
- 检出 `VIOLATION`：只对违规文字做局部修复/重绘并生成合规副本后复检原图路径对应的新文件；不直接跳过、不静默放行。
- 凭证、图片路径或计费归属变更时重新小样本（≥5 张已知违规 + ≥5 张干净）验证一次再批量。

来源首图保真、删页和详情近重复另见 `references/image-quality-gate.md`。缓存按提示词版本与图像 SHA-256 失效重审；单图 CLEAN 不能代替来源双图验收。
