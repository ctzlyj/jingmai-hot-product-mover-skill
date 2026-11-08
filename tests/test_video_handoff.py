import hashlib, json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import photo_video as pv
import video_handoff as vh
from photo_generation import atomic_json

def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
QC = {'automatic': {'pass': True}, 'manual': {'pass': True}}

def make_package(root, *, voice_required=True, mutate=None):
    """A handoff the way 11190@1.1.2 writes it: video + cover with SHA256, runId, mode/label/taskId, qc, bindable."""
    root = Path(root); (root / '视频').mkdir(parents=True, exist_ok=True)
    video = root / '视频' / '主图视频_AI带货_run1.mp4'; cover = root / '视频' / '主图视频_AI带货_run1_封面.jpg'
    video.write_bytes(b'video-bytes'); cover.write_bytes(b'cover-bytes')
    v = {'localPath': '视频/' + video.name, 'sha256': digest(video), 'coverLocalPath': '视频/' + cover.name, 'coverSha256': digest(cover), 'runId': 'run1',
         'mode': 'ai', 'label': '灵境 AI 带货', 'taskId': '123', 'bindable': True, 'qc': QC, 'durationSeconds': 18, 'voiceReviewRequired': voice_required}
    if mutate: mutate(v)
    m = {'schemaVersion': 'listing-package/v1', 'status': 'VERIFIED', 'store': {'profile': 'pop-test', 'venderId': 1, 'shopName': '店'}, 'productId': 10, 'skuIds': [11, 12], 'category': {'id': 1}, 'title': 'test', 'video': v}
    atomic_json(root / 'manifest.json', m)
    return root / 'manifest.json'

def completed(code=0, out=None):
    return type('P', (), {'returncode': code, 'stdout': json.dumps(out or {'ok': code == 0, 'errors': []}), 'stderr': ''})()

def ok_handoff(): return patch.object(vh.subprocess, 'run', return_value=completed(0))
ACCEPT = '已试听，确认绑定'


class HandoffCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        self.file = make_package(self.root)
    def check(self, **kw):
        with ok_handoff(), patch.object(vh, 'find_video_skill', return_value=self.root / 'fake-skill'):
            return vh.check_handoff(self.file, **kw)
    def test_valid_handoff_returns_sha_and_requires_listening(self):
        r = self.check(); self.assertEqual(digest(self.root / '视频' / '主图视频_AI带货_run1.mp4'), r['sha256']); self.assertTrue(r['voiceReviewRequired'])
        self.assertEqual('ai', r['mode']); self.assertEqual('run1', r['runId'])
    def test_verify_handoff_nonzero_blocks(self):
        with patch.object(vh.subprocess, 'run', return_value=completed(3, {'ok': False, 'errors': ['localPath SHA256不一致或缺失']})), patch.object(vh, 'find_video_skill', return_value=self.root / 'x'):
            with self.assertRaisesRegex(vh.HandoffError, 'verify-handoff'): vh.check_handoff(self.file)
    def test_calls_the_installed_skill_with_the_package_dir_and_hidden_window(self):
        with patch.object(vh.subprocess, 'run', return_value=completed(0)) as run, patch.object(vh, 'find_video_skill', return_value=Path('S')):
            vh.check_handoff(self.file)
        cmd = run.call_args[0][0]; self.assertIn('verify-handoff', cmd); self.assertTrue(str(cmd[1]).replace('\\', '/').endswith('scripts/pop_video.py')); self.assertEqual(str(self.root), cmd[-1])
    def test_missing_video_skill_blocks_with_install_hint(self):
        with patch.object(vh, 'find_video_skill', return_value=None):
            with self.assertRaisesRegex(vh.HandoffError, '11190'): vh.check_handoff(self.file)
    def test_bindable_false_blocks(self):
        m = json.loads(self.file.read_text(encoding='utf-8')); m['video']['bindable'] = False; atomic_json(self.file, m)
        with self.assertRaisesRegex(vh.HandoffError, 'bindable'): self.check()
    def test_missing_mode_label_or_task_id_for_ai_blocks(self):
        for key in ('mode', 'label', 'taskId'):
            m = json.loads(self.file.read_text(encoding='utf-8')); m['video'].pop(key, None); atomic_json(self.file, m)
            with self.assertRaises(vh.HandoffError, msg=key): self.check()
            self.setUp()
    def test_material_mode_does_not_need_task_id(self):
        m = json.loads(self.file.read_text(encoding='utf-8')); m['video'].update(mode='material', label='素材拼接，不是灵境 AI'); m['video'].pop('taskId'); atomic_json(self.file, m)
        self.assertEqual('material', self.check()['mode'])
    def test_qc_must_show_both_automatic_and_visual_review(self):
        for qc in ({}, {'automatic': {'pass': True}}, {'automatic': {'pass': True}, 'manual': {'pass': False}}, {'automatic': {'pass': False}, 'manual': {'pass': True}}):
            m = json.loads(self.file.read_text(encoding='utf-8')); m['video']['qc'] = qc; atomic_json(self.file, m)
            with self.assertRaisesRegex(vh.HandoffError, 'qc', msg=str(qc)): self.check()
    def test_video_sha_mismatch_blocks_even_if_verify_handoff_were_skipped(self):
        (self.root / '视频' / '主图视频_AI带货_run1.mp4').write_bytes(b'swapped')
        with self.assertRaisesRegex(vh.HandoffError, 'SHA'): self.check()
    def test_cover_sha_mismatch_blocks(self):
        (self.root / '视频' / '主图视频_AI带货_run1_封面.jpg').write_bytes(b'swapped')
        with self.assertRaisesRegex(vh.HandoffError, 'SHA'): self.check()
    def test_path_escape_blocks(self):
        m = json.loads(self.file.read_text(encoding='utf-8')); m['video']['localPath'] = '../outside.mp4'; atomic_json(self.file, m)
        with self.assertRaises(Exception): self.check()
    def test_directory_is_never_scanned_for_the_latest_video(self):
        (self.root / '视频' / 'newer.mp4').write_bytes(b'newer'); r = self.check()
        self.assertTrue(r['localPath'].endswith('run1.mp4'))
    def test_explicit_mp4_must_equal_the_manifest_video(self):
        other = self.root / 'other.mp4'; other.write_bytes(b'other')
        with self.assertRaisesRegex(vh.HandoffError, 'manifest'): self.check(mp4=other)
        r = self.check(mp4=self.root / '视频' / '主图视频_AI带货_run1.mp4'); self.assertEqual('manifest', r['source'])


class LegacyManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        self.file = make_package(self.root, mutate=lambda v: [v.pop(k) for k in ('sha256', 'coverSha256', 'runId', 'voiceReviewRequired')])
    def test_old_1_1_0_manifest_is_blocked_with_migration_hint_and_missing_fields(self):
        with patch.object(vh.subprocess, 'run') as run, patch.object(vh, 'find_video_skill', return_value=Path('S')):
            with self.assertRaises(vh.HandoffError) as c: vh.check_handoff(self.file)
            run.assert_not_called()
        text = str(c.exception)
        for word in ('sha256', 'coverSha256', 'runId', '11190', '迁移'): self.assertIn(word, text)
    def test_diagnosis_is_read_only_and_lists_missing_fields(self):
        before = self.file.read_bytes(); d = vh.diagnose(self.file)
        self.assertEqual(['sha256', 'coverSha256', 'runId', 'voiceReviewRequired'], d['missing']); self.assertFalse(d['bindable_by_handoff']); self.assertEqual(before, self.file.read_bytes())
    def test_there_is_no_function_that_writes_the_current_file_sha_into_the_manifest(self):
        text = (ROOT / 'scripts' / 'video_handoff.py').read_text(encoding='utf-8')
        self.assertNotIn("'sha256'] =", text.replace(' ', '')); self.assertNotIn('migrate(', text)


class VoiceReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name); self.file = make_package(self.root)
    def info(self):
        with ok_handoff(), patch.object(vh, 'find_video_skill', return_value=Path('S')): return vh.check_handoff(self.file)
    def record(self, phrase):
        # never depend on a locally installed 11190: the handoff subprocess and the skill lookup are always faked here
        with ok_handoff(), patch.object(vh, 'find_video_skill', return_value=Path('S')): return vh.record_voice_review(self.file, phrase)
    def test_unconfirmed_listening_blocks_binding_and_prompt_has_the_facts(self):
        info = self.info(); self.assertFalse(info['voiceConfirmed'])
        with self.assertRaises(vh.HandoffError) as c: vh.require_voice_review(info)
        text = str(c.exception)
        for word in ('试听', '主图视频_AI带货_run1.mp4', 'ai', '18', '口播', ACCEPT, '接受口播风险，确认绑定'): self.assertIn(word, text)
    def test_exact_acceptance_phrases_record_raw_time_and_sha(self):
        for phrase in (ACCEPT, '接受口播风险，确认绑定'):
            self.setUp(); info = self.info(); rec = self.record(phrase)
            self.assertEqual(phrase, rec['confirmationRaw']); self.assertEqual(info['sha256'], rec['videoSha256']); self.assertTrue(rec['at'])
            self.assertEqual(rec, json.loads(self.file.read_text(encoding='utf-8'))['video']['voiceReview'])
            self.assertTrue(self.info()['voiceConfirmed']); vh.require_voice_review(self.info())
    def test_vague_or_generic_replies_are_not_confirmation(self):
        for phrase in ('', '确认', '好的', '可以', '已试听', '确认绑定', 'ok'):
            with self.assertRaises(vh.HandoffError, msg=phrase): self.record(phrase)
        self.assertNotIn('voiceReview', json.loads(self.file.read_text(encoding='utf-8'))['video'])
    def test_changed_video_sha_invalidates_the_confirmation(self):
        self.record(ACCEPT)
        m = json.loads(self.file.read_text(encoding='utf-8')); new = self.root / '视频' / 'run2.mp4'; new.write_bytes(b'new-video'); m['video'].update(localPath='视频/run2.mp4', sha256=digest(new)); atomic_json(self.file, m)
        info = self.info(); self.assertFalse(info['voiceConfirmed'])
        with self.assertRaises(vh.HandoffError): vh.require_voice_review(info)
    def test_not_required_means_no_prompt(self):
        make_package(self.root, voice_required=False); info = self.info(); self.assertFalse(info['voiceReviewRequired']); vh.require_voice_review(info)
    def test_confirmation_cannot_be_recorded_when_handoff_fails(self):
        (self.root / '视频' / '主图视频_AI带货_run1.mp4').write_bytes(b'swapped')
        with self.assertRaises(vh.HandoffError): self.info()
        with patch.object(vh.subprocess, 'run', return_value=completed(3)), patch.object(vh, 'find_video_skill', return_value=Path('S')):
            with self.assertRaises(vh.HandoffError): vh.record_voice_review(self.file, ACCEPT)


class LocateSkillTests(unittest.TestCase):
    def test_finds_named_or_skill_id_install_without_personal_paths(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d); (home / '.agents/skills/skill-11190/scripts').mkdir(parents=True); (home / '.agents/skills/skill-11190/scripts/pop_video.py').write_text('', encoding='utf-8')
            (home / '.agents/skills/skill-11190/SKILL.md').write_text('---\nname: jd-main-video-batch\nversion: 1.1.2\n---\n', encoding='utf-8')
            found = vh.find_video_skill(home=home, skill_root=home / 'nowhere' / 'mover')
            self.assertEqual(home / '.agents/skills/skill-11190', found)
    def test_env_override_and_none_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(vh.find_video_skill(home=Path(d), skill_root=Path(d) / 'a' / 'b', env={}))
            (Path(d) / 'custom/scripts').mkdir(parents=True); (Path(d) / 'custom/scripts/pop_video.py').write_text('', encoding='utf-8')
            self.assertEqual(Path(d) / 'custom', vh.find_video_skill(home=Path(d), skill_root=Path(d) / 'a' / 'b', env={'JD_MAIN_VIDEO_SKILL': str(Path(d) / 'custom')}))

if __name__ == '__main__': unittest.main()
