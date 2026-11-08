# 视频绑定前的交接检查（1.2.4）

`photo_video.py` 绑定主图视频前必须先通过本检查，**通过之前不探测文件、不核对店铺、不访问任何平台接口**。

## 绑定的是哪个视频

- 默认只读 `manifest.video.localPath`（主图视频 Skill 11190 发布的那一个），**不扫描目录选“最新视频”**。
- `--mp4` 只能等于 manifest 里的那个文件；包内已有 `manifest.video` 时，不同路径一律停止。外部视频（不是 11190 生成的）要用户另行确认，不能和已有 `manifest.video` 的包混用，也不能冒充黑灯生成视频。
- 开始时记录被验证的 SHA256；上传地址申请前、传输后、保存视频前、逐 SKU 绑定前都复核源文件仍是同一个 SHA，变化即停止（不保存、不绑定）。

## 检查内容

1. 调用已安装的 11190（≥1.1.2，目录名 `jd-main-video-batch` 或 `skill-<id>`，也可用环境变量 `JD_MAIN_VIDEO_SKILL` 指定）：
   `python <11190目录>/scripts/pop_video.py verify-handoff "<listing-package>"`。退出码非 0 → 停止（退出码 3 返回给调用方）。找不到 11190 → 提示先安装。
2. 本 Skill 独立再核：`bindable == true`；`mode`、`label` 存在，`mode=ai` 时必须有 `taskId`；`localPath` 与 `sha256`、`coverLocalPath` 与 `coverSha256` 对应；`qc.automatic.pass` 与 `qc.manual.pass` 同时为 true；路径不得越出 listing-package。

## 试听确认

`voiceReviewRequired=true` 时，绑定前展示：视频路径、模式、时长、口播风险（ASR 对齐、无真人听审）。用户**必须原话**回复其一：

- 「已试听，确认绑定」
- 「接受口播风险，确认绑定」

“确认”“好的”“可以”等不算。记录到 `manifest.video.voiceReview = {confirmationRaw, at, videoSha256}`；视频 SHA 一旦变化，这次确认失效，需重新试听。命令：

```text
python scripts/photo_video.py --manifest <PACKAGE/manifest.json> --confirm BIND_MAIN_VIDEO --voice-confirmation "已试听，确认绑定" --record-only   # 只记录，不绑定
python scripts/photo_video.py --manifest <PACKAGE/manifest.json> --confirm BIND_MAIN_VIDEO                                                  # 绑定
python scripts/photo_video.py --manifest <PACKAGE/manifest.json> --confirm x --diagnose                                                     # 只读诊断，不访问平台
```

## 旧版交接包（如 11190@1.1.0）

缺少 `sha256`、`coverSha256`、`runId`、`voiceReviewRequired` 的视频不会绑定。`--diagnose` 只读列出缺哪些字段。补齐只能走 11190 的正式发布路径（用 ≥1.1.2 重新出片/`review-video` 发布候选）；**不得手工把当前文件的 SHA 写进 manifest**，本 Skill 也没有这类写入。11190@1.1.2 目前没有“只读校验后迁移旧包”的命令，需要 11190 补一个。

## 防重复

保留原有逻辑：上传/保存/绑定前后都写检查点；结果不明只回查原视频记录（`--resume-video-id` 需匹配原 `jdVideoId`），不重复上传；已有其他视频的 SKU 不覆盖。
