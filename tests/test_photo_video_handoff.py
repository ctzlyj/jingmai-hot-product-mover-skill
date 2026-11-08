import hashlib, json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts')); sys.path.insert(0, str(ROOT / 'tests'))
import photo_video as pv
import video_handoff as vh
from photo_generation import atomic_json
from test_video_handoff import make_package, completed, digest, ACCEPT

VIDEO = '视频/主图视频_AI带货_run1.mp4'

def video_meta(): return {'durationSeconds': 18, 'width': 768, 'height': 1024, 'bytes': 11}

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name); self.file = make_package(self.root)
        self.mp4 = self.root / VIDEO
    def handoff_ok(self): return patch.object(vh.subprocess, 'run', return_value=completed(0))
    def skill(self): return patch.object(vh, 'find_video_skill', return_value=Path('S'))
    def platform(self):
        """Every platform entry point; each must stay untouched when the handoff gate stops the binding."""
        names = [(pv, 'verify_store'), (pv, 'read_product'), (pv.cli, 'get'), (pv.cli, 'run_jdo_large'), (pv.pipeline, 'existing_video_materials'), (pv.pipeline, 'video_zone_id'), (pv.requests, 'post')]
        return [patch.object(o, n, side_effect=AssertionError('platform call before handoff passed: ' + n)) for o, n in names]
    def bind(self, **kw):
        kw.setdefault('confirm', 'BIND_MAIN_VIDEO')
        return pv.bind_video(self.file, kw.pop('mp4', None), **kw)
    def blocked(self, expected=vh.HandoffError, **kw):
        stack = self.platform()
        for p in stack: p.start()
        self.addCleanup(lambda: [p.stop() for p in stack])
        with patch.object(pv, 'probe_video', side_effect=AssertionError('probe before handoff')):
            with self.assertRaises(expected): self.bind(**kw)


class GateBeforePlatformTests(Base):
    def test_verify_handoff_failure_stops_before_any_platform_call(self):
        with patch.object(vh.subprocess, 'run', return_value=completed(3, {'ok': False, 'errors': ['x']})), self.skill(): self.blocked()
    def test_bindable_false_stops(self):
        m = json.loads(self.file.read_text(encoding='utf-8')); m['video']['bindable'] = False; atomic_json(self.file, m)
        with self.handoff_ok(), self.skill(): self.blocked()
    def test_video_or_cover_sha_mismatch_stops(self):
        (self.root / '视频/主图视频_AI带货_run1_封面.jpg').write_bytes(b'swapped')
        with self.handoff_ok(), self.skill(): self.blocked()
    def test_legacy_manifest_stops_with_no_subprocess_and_no_platform(self):
        self.file = make_package(self.root, mutate=lambda v: [v.pop(k) for k in ('sha256', 'coverSha256', 'runId', 'voiceReviewRequired')])
        with patch.object(vh.subprocess, 'run') as run, self.skill():
            self.blocked(); run.assert_not_called()
    def test_voice_review_required_but_unconfirmed_stops_before_platform(self):
        with self.handoff_ok(), self.skill(): self.blocked()
    def test_different_mp4_than_manifest_stops(self):
        other = self.root / 'other.mp4'; other.write_bytes(b'other')
        with self.handoff_ok(), self.skill(): self.blocked(mp4=other)
    def test_default_reads_manifest_and_never_scans_for_newest_video(self):
        (self.root / '视频/zzz-newest.mp4').write_bytes(b'newest')
        info = None
        with self.handoff_ok(), self.skill(): info = vh.check_handoff(self.file)
        self.assertTrue(info['path'].endswith('主图视频_AI带货_run1.mp4'))
    def test_missing_confirm_word_still_wins_first(self):
        with self.assertRaises(ValueError): self.bind(confirm='')


class ConfirmedFlowTests(Base):
    def run_bind(self, *, mutate_during=None, state=None):
        vh.record_voice_review.__wrapped__ if False else None
        with self.handoff_ok(), self.skill(): vh.record_voice_review(self.file, ACCEPT)
        video = {'videoId': 77, 'jdVideoId': 5, 'status': 4, 'indexImage': 'c', 'videoName': 't', 'duration': 18}; writes = []; gets = []; count = {'m': 0}
        def get(skill, url, params=None):
            gets.append(url)
            if url.endswith('/upload-urls'): return {'data': {'httpsUploadUrl': 'https://example.test/u', 'jdVideoId': 5}}
            if url.endswith('/sku-material-tags'): return {'data': [{'tagId': 7, 'tagName': '产品展示', 'sort': 1}]}
            raise AssertionError(url)
        def post(**kw):
            writes.append(kw['url'])
            return {'data': {'videoId': 77}} if kw['url'].endswith('/videos') else {'data': {}}
        def materials(ids):
            count['m'] += 1
            return {} if count['m'] <= 2 else {i: [{'videoMaterial': {'videoId': 77}}] for i in ids}
        def transfer(url, files=None, timeout=None):
            if mutate_during: mutate_during()
            return type('R', (), {'status_code': 200})()
        with self.handoff_ok(), self.skill(), patch.object(pv, 'verify_store'), patch.object(pv.cli, 'recheck_verified_jdo_store'), patch.object(pv, 'probe_video', return_value=video_meta()), patch.object(pv, 'read_product', return_value={'skuList': [{'skuId': 11}, {'skuId': 12}]}), patch.object(pv.pipeline, 'existing_video_materials', side_effect=materials), patch.object(pv.pipeline, 'video_zone_id', return_value='v'), patch.object(pv.pipeline, 'query_video', return_value=video), patch.object(pv.pipeline, 'query_video_relations', return_value=[{'skuId': i, 'videoId': 77, 'status': 4} for i in (11, 12)]), patch.object(pv.cli, 'get', side_effect=get), patch.object(pv.cli, 'run_jdo_large', side_effect=post), patch.object(pv.requests, 'post', side_effect=transfer) as t:
            try: r = self.bind(interval=0)
            except Exception as e: r = e
        return r, writes, t
    def test_confirmed_handoff_binds_and_records_the_verified_sha(self):
        r, writes, t = self.run_bind()
        self.assertEqual('VIDEO_VERIFIED', r['stage']); self.assertEqual(1, t.call_count); self.assertEqual(3, len(writes))
        m = json.loads(self.file.read_text(encoding='utf-8')); sha = digest(self.mp4)
        self.assertEqual(sha, m['video']['binding']['sourceSha256']); self.assertEqual(sha, m['video']['sha256']); self.assertEqual(sha, m['video']['voiceReview']['videoSha256'])
        self.assertEqual(sha, json.loads((self.root / '.video-binding/state.json').read_text(encoding='utf-8'))['sourceSha256'])
    def test_original_manifest_fields_are_preserved_after_binding(self):
        before = json.loads(self.file.read_text(encoding='utf-8'))['video']; self.run_bind(); after = json.loads(self.file.read_text(encoding='utf-8'))['video']
        for k in ('runId', 'mode', 'label', 'taskId', 'sha256', 'coverSha256', 'qc'): self.assertEqual(before[k], after[k], k)
    def test_file_replaced_during_transfer_stops_and_never_saves_or_binds(self):
        r, writes, t = self.run_bind(mutate_during=lambda: self.mp4.write_bytes(b'replaced-while-uploading'))
        self.assertIsInstance(r, Exception); self.assertEqual([], writes)
        state = json.loads((self.root / '.video-binding/state.json').read_text(encoding='utf-8')); self.assertNotEqual('VIDEO_VERIFIED', state['stage'])
    def test_source_changed_before_upload_stops_before_upload_urls(self):
        with self.handoff_ok(), self.skill(): vh.record_voice_review(self.file, ACCEPT)
        calls = []
        orig = pv.probe_video
        def swap(path): self.mp4.write_bytes(b'swapped-after-check'); return video_meta()
        with self.handoff_ok(), self.skill(), patch.object(pv, 'verify_store'), patch.object(pv, 'probe_video', side_effect=swap), patch.object(pv, 'read_product', return_value={'skuList': [{'skuId': 11}, {'skuId': 12}]}), patch.object(pv.pipeline, 'existing_video_materials', return_value={}), patch.object(pv.pipeline, 'video_zone_id', return_value='v'), patch.object(pv.cli, 'get', side_effect=lambda *a, **k: calls.append(a) or {}), patch.object(pv.cli, 'run_jdo_large', side_effect=lambda **k: calls.append(k)), patch.object(pv.requests, 'post', side_effect=lambda *a, **k: calls.append('post')):
            with self.assertRaises(vh.HandoffError): self.bind(interval=0)
        self.assertEqual([], calls)
    def test_unknown_transfer_result_is_not_repeated_with_the_gate_in_place(self):
        atomic_json(self.root / '.video-binding/state.json', {'stage': 'SAVE_UNKNOWN', 'sourceSha256': digest(self.mp4), 'jdVideoId': 5})
        with self.handoff_ok(), self.skill(): vh.record_voice_review(self.file, ACCEPT)
        with self.handoff_ok(), self.skill(), patch.object(pv, 'verify_store'), patch.object(pv, 'probe_video', return_value=video_meta()), patch.object(pv, 'read_product', return_value={'skuList': [{'skuId': 11}, {'skuId': 12}]}), patch.object(pv.pipeline, 'existing_video_materials', return_value={}), patch.object(pv.pipeline, 'video_zone_id', return_value='v'), patch.object(pv.cli, 'run_jdo_large') as post, patch.object(pv.requests, 'post') as t:
            with self.assertRaises(pv.ReconcileRequired): self.bind(interval=0)
            post.assert_not_called(); t.assert_not_called()
    def test_changed_video_after_confirmation_requires_new_listening(self):
        with self.handoff_ok(), self.skill(): vh.record_voice_review(self.file, ACCEPT)
        m = json.loads(self.file.read_text(encoding='utf-8')); new = self.root / '视频/run2.mp4'; new.write_bytes(b'v2'); m['video'].update(localPath='视频/run2.mp4', sha256=digest(new)); atomic_json(self.file, m)
        stack = self.platform()
        for p in stack: p.start()
        self.addCleanup(lambda: [p.stop() for p in stack])
        with self.handoff_ok(), self.skill():
            with self.assertRaises(vh.HandoffError) as c: self.bind(interval=0)
        self.assertIn('试听', str(c.exception))

class CliTests(Base):
    def test_cli_exposes_diagnose_and_listening_confirmation(self):
        text = (ROOT / 'scripts' / 'photo_video.py').read_text(encoding='utf-8')
        for word in ('--diagnose', '--voice-confirmation'): self.assertIn(word, text)
    def test_cli_diagnose_never_touches_the_platform(self):
        out = []
        with patch.object(pv, 'verify_store', side_effect=AssertionError), patch.object(pv.cli, 'get', side_effect=AssertionError), patch('builtins.print', side_effect=lambda *a, **k: out.append(a[0])):
            code = pv.main(['--manifest', str(self.file), '--confirm', 'x', '--diagnose'])
        self.assertEqual(0, code); self.assertIn('bindable_by_handoff', out[0])
    def test_cli_records_confirmation_then_stops_without_binding(self):
        with self.handoff_ok(), self.skill(), patch.object(pv, 'verify_store', side_effect=AssertionError), patch('builtins.print'):
            code = pv.main(['--manifest', str(self.file), '--confirm', 'BIND_MAIN_VIDEO', '--voice-confirmation', ACCEPT, '--record-only'])
        self.assertEqual(0, code); self.assertEqual(ACCEPT, json.loads(self.file.read_text(encoding='utf-8'))['video']['voiceReview']['confirmationRaw'])

if __name__ == '__main__': unittest.main()
