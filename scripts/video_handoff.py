"""Pre-binding handoff gate for the main-image video (11190 jd-main-video-batch >= 1.1.2).

Binding must use exactly the video the manifest names, byte-for-byte as 11190 published it:
  1. run the installed 11190 `pop_video.py verify-handoff <package>` (non-zero => stop)
  2. independently re-check bindable / mode / label / taskId / qc / SHA256 of video and cover
  3. require the user's explicit listening confirmation when voiceReviewRequired is true
Legacy manifests (1.1.0: no sha256/runId/voiceReviewRequired) are diagnosed read-only and never bound;
nothing here writes a file SHA into manifest.video - only 11190's publication path may do that.
"""
from __future__ import annotations
import hashlib, json, os, re, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import win_no_window  # noqa: F401
from photo_generation import atomic_json, load
from skill_paths import AGENT_SKILL_ROOTS, SKILL_ROOT

VIDEO_SKILL_NAME = 'jd-main-video-batch'
ENV_OVERRIDE = 'JD_MAIN_VIDEO_SKILL'
MIGRATION_HINT = ('该视频是旧版 11190（如 1.1.0）生成的交接包，缺少完整性字段。请用 11190@1.1.2 或更新版本通过正式路径重新发布/迁移视频'
                  '（review-video 重新发布候选，或重新出片），不要手工把当前文件的 SHA 写进 manifest 来绕过检查。'
                  '若 11190 暂无迁移命令，需要 11190 增加“只读校验后迁移”命令。')
ACCEPT_PHRASES = ('已试听，确认绑定', '接受口播风险，确认绑定')
REQUIRED_FIELDS = ('sha256', 'coverSha256', 'runId', 'voiceReviewRequired')


class HandoffError(RuntimeError):
    pass


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def find_video_skill(*, home=None, skill_root=None, env=None):
    """Locate the installed 11190 skill: env override, sibling folder, per-agent roots, skill-<id> folders (by frontmatter)."""
    env = os.environ if env is None else env
    override = str(env.get(ENV_OVERRIDE) or '').strip()
    if override and (Path(override) / 'scripts' / 'pop_video.py').is_file():
        return Path(override)
    root = Path(skill_root or SKILL_ROOT)
    user = Path(home or Path.home())
    bases = [root.parent] + [user / b for b in AGENT_SKILL_ROOTS]
    for base in dict.fromkeys(bases):
        if not base.is_dir():
            continue
        direct = base / VIDEO_SKILL_NAME
        if (direct / 'scripts' / 'pop_video.py').is_file():
            return direct
        for cand in sorted(base.glob('skill-*')):
            entry = cand / 'SKILL.md'
            if entry.is_file() and (cand / 'scripts' / 'pop_video.py').is_file():
                head = entry.read_text(encoding='utf-8-sig', errors='replace').split('---', 2)
                if len(head) >= 3 and re.search(r'^name:\s*["\']?' + re.escape(VIDEO_SKILL_NAME) + r'["\']?\s*$', head[1], re.M):
                    return cand
    return None


def _video(manifest_path):
    m = load(Path(manifest_path)) or {}
    return m, (m.get('video') or {})


def diagnose(manifest_path):
    """Read-only: which handoff fields are missing/invalid. Never writes."""
    _, v = _video(manifest_path)
    missing = [k for k in REQUIRED_FIELDS if k not in v or v.get(k) in (None, '')]
    problems = []
    if v.get('bindable') is not True:
        problems.append('bindable 不是 true')
    mode = v.get('mode')
    if mode not in ('ai', 'material'):
        problems.append('mode 缺失或未知')
    if not v.get('label'):
        problems.append('label 缺失')
    if mode == 'ai' and not v.get('taskId'):
        problems.append('AI 视频缺少 taskId')
    qc = v.get('qc') or {}
    if (qc.get('automatic') or {}).get('pass') is not True or (qc.get('manual') or {}).get('pass') is not True:
        problems.append('qc 未显示自动质检和视觉复核都通过')
    return {'missing': missing, 'problems': problems, 'bindable_by_handoff': not missing and not problems,
            'migration': MIGRATION_HINT if missing else '', 'video': {k: v.get(k) for k in ('localPath', 'mode', 'label', 'runId')}}


def _safe(root, rel):
    p = (Path(root) / rel).resolve()
    if Path(root).resolve() not in p.parents:
        raise HandoffError('视频/封面路径越出 listing-package：' + str(rel))
    return p


def check_handoff(manifest_path, *, mp4=None, runner=None):
    """Returns the verified facts used for binding. Raises HandoffError on any doubt (no platform access here)."""
    manifest_path = Path(manifest_path).resolve()
    root = manifest_path.parent
    m, v = _video(manifest_path)
    if not v or not v.get('localPath'):
        raise HandoffError('manifest 没有 video.localPath；默认只从 manifest 取视频，不扫描目录。请先让主图视频 Skill（11190）出片并发布到 manifest。')
    d = diagnose(manifest_path)
    if d['missing']:
        raise HandoffError('视频交接字段缺失：' + '、'.join(d['missing']) + '。' + MIGRATION_HINT)
    if d['problems']:
        raise HandoffError('视频交接检查未通过：' + '；'.join(d['problems']))
    video = _safe(root, v['localPath'])
    cover = _safe(root, v['coverLocalPath']) if v.get('coverLocalPath') else None
    if mp4 is not None and Path(mp4).resolve() != video:
        raise HandoffError('--mp4 与 manifest.video.localPath 不一致：包内已有 manifest.video 时不能用其他文件覆盖。'
                           '外部视频需用户另行确认，且不能与已有 manifest.video 的包混用。')
    skill = find_video_skill()
    if skill is None:
        raise HandoffError('未找到已安装的主图视频 Skill（SkillHub 11190，jd-main-video-batch ≥1.1.2）。请先安装，再运行绑定前检查。')
    run = runner or subprocess.run
    cmd = [sys.executable, str(skill / 'scripts' / 'pop_video.py'), 'verify-handoff', str(root)]
    p = run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    if p.returncode != 0:
        detail = (p.stdout or p.stderr or '')[:400]
        raise HandoffError('11190 verify-handoff 未通过（退出码 %s），不上传、不绑定：%s' % (p.returncode, detail))
    # independent re-check: the file we are about to upload is the file 11190 hashed
    if not video.is_file() or file_sha(video) != v['sha256']:
        raise HandoffError('视频文件 SHA256 与 manifest 不一致：文件可能被替换，停止')
    if cover is not None:
        if not cover.is_file() or file_sha(cover) != v['coverSha256']:
            raise HandoffError('封面文件 SHA256 与 manifest 不一致，停止')
    review = v.get('voiceReview') or {}
    confirmed = bool(review.get('videoSha256') == v['sha256'] and review.get('confirmationRaw') in ACCEPT_PHRASES)
    return {'localPath': v['localPath'], 'path': str(video), 'sha256': v['sha256'], 'coverSha256': v.get('coverSha256'), 'runId': v['runId'],
            'mode': v['mode'], 'label': v['label'], 'taskId': v.get('taskId'), 'durationSeconds': v.get('durationSeconds'),
            'voiceReviewRequired': bool(v['voiceReviewRequired']), 'voiceConfirmed': confirmed, 'source': 'manifest'}


def listening_prompt(info):
    return ('绑定前请先试听主图视频。\n'
            f"视频：{info['localPath']}（{info['path']}）\n模式：{info['mode']}（{info['label']}），时长：{info.get('durationSeconds')} 秒\n"
            '口播风险：口播/字幕只做了 ASR 对齐，没有真人听审，可能有读错、口误或不合规表述。\n'
            '听完后请明确回复其中一句：「' + ACCEPT_PHRASES[0] + '」或「' + ACCEPT_PHRASES[1] + '」。其他回复（如“确认”“好的”）不算。视频文件一旦改变，这次确认失效。')


def require_voice_review(info):
    if info['voiceReviewRequired'] and not info['voiceConfirmed']:
        raise HandoffError(listening_prompt(info))
    return True


def record_voice_review(manifest_path, confirmation, *, runner=None):
    """Record the user's own words, the time and the video SHA. Needs a passing handoff first."""
    text = str(confirmation or '').strip()
    if text not in ACCEPT_PHRASES:
        raise HandoffError('需要用户明确回复「' + ACCEPT_PHRASES[0] + '」或「' + ACCEPT_PHRASES[1] + '」，收到：' + (text or '空'))
    info = check_handoff(manifest_path, runner=runner)
    manifest_path = Path(manifest_path)
    m = load(manifest_path)
    rec = {'confirmationRaw': text, 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'videoSha256': info['sha256']}
    m['video']['voiceReview'] = rec
    atomic_json(manifest_path, m)
    return rec
