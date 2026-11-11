import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import jdo_bridge_probe as bp

RUNTIME_V34 = b'runtime-0.3.4'
RUNTIME_V37 = b'runtime-0.3.7'
NEEDED = ('url', 'method', 'params', 'body', 'path_params', 'timeout', 'dry_run')

def completed(code=0, out=''):
    return type('P', (), {'returncode': code, 'stdout': out, 'stderr': ''})()

def probe_json(callable_=True, attrs=NEEDED, version='3.12.9'):
    return json.dumps({'host': version, 'cmd_invoke': callable_, 'argcount': 1, 'attrs_read': list(attrs)})


class RuntimeVersionTests(unittest.TestCase):
    """CLI 0.3.4 and 0.3.7 (and later) must both pass; the hash is never a gate."""
    def run_probe(self, runtime, **kw):
        with tempfile.TemporaryDirectory() as d, patch.object(bp.subprocess, 'run', return_value=completed(0, probe_json(**kw))):
            return bp.probe_runtime(Path(d) / 'rt.pyc', runtime, python=Path('py312'), cache=Path(d) / 'cache.json')
    def test_known_and_unknown_runtime_versions_are_accepted_when_the_interface_matches(self):
        for runtime in (RUNTIME_V34, RUNTIME_V37, b'runtime-0.4.0-future'):
            r = self.run_probe(runtime); self.assertTrue(r['ok'], runtime); self.assertIn('sha256', r)
    def test_changed_interface_is_reported_in_plain_words_not_hashes(self):
        r = self.run_probe(RUNTIME_V37, attrs=('url',))  # a future CLI that stopped reading body etc.
        self.assertFalse(r['ok']); text = r['userMessage']
        for word in ('大请求', '发品', '整版编辑'): self.assertIn(word, text)
        for jargon in ('sha', 'hash', 'probe', 'cmd_invoke', 'Namespace'): self.assertNotIn(jargon.lower(), text.lower())
    def test_missing_cmd_invoke_is_an_interface_change(self):
        self.assertFalse(self.run_probe(RUNTIME_V37, callable_=False)['ok'])
    def test_extra_unknown_attributes_are_fine_missing_needed_ones_are_not(self):
        self.assertTrue(self.run_probe(RUNTIME_V37, attrs=NEEDED + ('new_flag', 'body_file'))['ok'])
        self.assertFalse(self.run_probe(RUNTIME_V37, attrs=tuple(a for a in NEEDED if a != 'body'))['ok'])
    def test_probe_runs_in_a_child_process_and_never_loads_code_in_the_host(self):
        with tempfile.TemporaryDirectory() as d, patch.object(bp.subprocess, 'run', return_value=completed(0, probe_json())) as run:
            bp.probe_runtime(Path(d) / 'rt.pyc', RUNTIME_V37, python=Path('py312'), cache=Path(d) / 'c.json')
        self.assertTrue(run.called); self.assertTrue(str(run.call_args[0][0][0]).endswith('py312'))
    def test_probe_crash_of_the_host_is_a_clean_negative_not_an_exception(self):
        with tempfile.TemporaryDirectory() as d, patch.object(bp.subprocess, 'run', return_value=completed(-1073741819, '')):
            r = bp.probe_runtime(Path(d) / 'rt.pyc', RUNTIME_V37, python=Path('py313'), cache=Path(d) / 'c.json')
        self.assertFalse(r['ok'])
    def test_result_is_cached_per_runtime_sha_so_two_versions_do_not_mix(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / 'cache.json'
            with patch.object(bp.subprocess, 'run', return_value=completed(0, probe_json())) as run:
                bp.probe_runtime(Path(d) / 'a.pyc', RUNTIME_V34, python=Path('p'), cache=cache); bp.probe_runtime(Path(d) / 'a.pyc', RUNTIME_V34, python=Path('p'), cache=cache)
                self.assertEqual(1, run.call_count)
                bp.probe_runtime(Path(d) / 'b.pyc', RUNTIME_V37, python=Path('p'), cache=cache)
                self.assertEqual(2, run.call_count)


class NoSystemPython312Tests(unittest.TestCase):
    def test_a_313_host_is_not_accepted_as_the_bridge_host(self):
        def fake_version(path): return (3, 13)
        with patch.object(bp, 'host_version', side_effect=fake_version), tempfile.TemporaryDirectory() as d:
            exe = Path(d) / 'python.exe'; exe.write_text('x')
            self.assertIsNone(bp.find_bridge_python(env={'JDO_PYTHON312': str(exe)}, home=Path(d), extra=[]))
    def test_a_real_312_host_is_found_from_env_default_or_launcher(self):
        with tempfile.TemporaryDirectory() as d, patch.object(bp, 'host_version', return_value=(3, 12)):
            exe = Path(d) / 'python.exe'; exe.write_text('x')
            self.assertEqual(exe, bp.find_bridge_python(env={'JDO_PYTHON312': str(exe)}, home=Path(d), extra=[]))
            default = Path(d) / 'AppData/Local/Programs/Python/Python312/python.exe'; default.parent.mkdir(parents=True); default.write_text('x')
            self.assertEqual(default, bp.find_bridge_python(env={}, home=Path(d), extra=[]))
    def test_nothing_installed_returns_none_and_status_is_plain(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(bp.find_bridge_python(env={}, home=Path(d), extra=[]))
            cases = {'no-cli': bp.bridge_status(exe=None, python=None, runtime_ok=None), 'no-python': bp.bridge_status(exe=Path('jdo-cli.exe'), python=None, runtime_ok=None),
                     'changed': bp.bridge_status(exe=Path('jdo-cli.exe'), python=Path('py312'), runtime_ok={'ok': False})}
        for name, s in cases.items():
            self.assertFalse(s['available'], name); text = s['userMessage']
            for word in ('发品', '整版编辑', '属性补全', '素材绑定', '查询', '审计', '授权', '本地生成'): self.assertIn(word, text, name)
            for jargon in ('sha', 'hash', 'probe', 'cmd_invoke', 'bridge', 'runtime', 'namespace'): self.assertNotIn(jargon, text.lower(), name)
        self.assertIn('Python 3.12', cases['no-python']['userMessage']); self.assertIn('IT', cases['no-python']['userMessage'])
    def test_status_affected_and_unaffected_lists_are_explicit(self):
        s = bp.bridge_status(exe=None, python=None, runtime_ok=None)
        self.assertEqual(['发品', '整版编辑', '属性补全', '素材绑定'], s['affected']); self.assertIn('查询', s['unaffected']); self.assertIn('本地生成', s['unaffected'])
    def test_available_when_host_and_probe_pass(self):
        s = bp.bridge_status(exe=Path('jdo-cli.exe'), python=Path('py312'), runtime_ok={'ok': True, 'sha256': 'x'})
        self.assertTrue(s['available']); self.assertEqual('', s['userMessage'])


class BigBodyTests(unittest.TestCase):
    LIMIT = 32000
    def test_threshold_helper_separates_small_and_large_bodies(self):
        self.assertFalse(bp.needs_bridge('{"a":"' + 'x' * 100 + '"}')); self.assertTrue(bp.needs_bridge('{"a":"' + 'x' * 40000 + '"}'))
        self.assertTrue(bp.needs_bridge('x' * (bp.COMMAND_LINE_BODY_LIMIT + 1))); self.assertFalse(bp.needs_bridge('x' * bp.COMMAND_LINE_BODY_LIMIT))
    def test_limit_is_below_the_measured_windows_failure_point(self):
        self.assertLessEqual(bp.COMMAND_LINE_BODY_LIMIT, 32000)
    def test_chinese_characters_count_by_utf16_units_like_the_command_line(self):
        self.assertTrue(bp.needs_bridge('中' * 33000)); self.assertFalse(bp.needs_bridge('中' * 20000))

if __name__ == '__main__': unittest.main()
