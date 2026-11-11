import json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts')); sys.path.insert(0, str(ROOT / 'tests'))
import jdo_listing_mvp as cli
import jdo_bridge_probe as bp
import bootstrap

OK_JSON = json.dumps({'success': True, 'data': {'x': 1}})
def cp(out=OK_JSON, code=0, text=False):
    # the official CLI path reads bytes, the bridge path runs with text=True: answer in the type the caller asked for
    return type('P', (), {'returncode': code, 'stdout': out if text else out.encode('utf-8'), 'stderr': '' if text else b''})()

def run_as(calls=None):
    def run(cmd, **kw):
        if calls is not None: calls.append(cmd)
        return cp(text=bool(kw.get('text')))
    return run


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        self.exe = self.root / 'jdo-cli.exe'; self.exe.write_bytes(b'MZ'); self.rt = self.root / 'rt.pyc'; self.rt.write_bytes(b'unknown-future-runtime')
        for p in (patch.object(cli, 'active_jdo_profile', return_value='pop-test'), patch.object(cli, 'recheck_verified_jdo_store'),
                  patch.dict('os.environ', {'JDO_CLI_PATH': str(self.exe)}), patch.object(cli, 'JDO_RUNTIME', self.root / 'rt' / 'jdo-cli.pyc')):
            p.start(); self.addCleanup(p.stop)
    def body(self, n):
        p = self.root / f'body{n}.json'; p.write_text(json.dumps({'k': 'x' * n}), encoding='utf-8'); return p


class SmallBodyTests(Setup):
    def test_small_body_goes_through_the_official_cli_without_any_python312(self):
        calls = []
        with patch.object(cli.subprocess, 'run', side_effect=lambda cmd, **kw: calls.append(cmd) or cp()), \
             patch.object(bp, 'find_bridge_python', side_effect=AssertionError('no system Python 3.12 needed for a small body')), \
             patch.object(cli, 'ensure_jdo_runtime', side_effect=AssertionError('no runtime extraction for a small body')):
            r = cli.run_jdo_large(url='/rest/x', method='POST', body_path=self.body(100), skill_id='jdo-product-publish', yes=True)
        self.assertEqual(1, r['data']['x']); cmd = calls[0]
        self.assertEqual(str(self.exe), str(cmd[0])); self.assertIn('--body', cmd); self.assertIn('--profile', cmd); self.assertIn('pop-test', cmd); self.assertIn('--yes', cmd)
        self.assertEqual(json.dumps({'k': 'x' * 100}), cmd[cmd.index('--body') + 1])
    def test_write_still_rechecks_the_store_before_sending(self):
        with patch.object(cli, 'recheck_verified_jdo_store') as recheck, patch.object(cli.subprocess, 'run', return_value=cp()):
            cli.run_jdo_large(url='/rest/x', method='POST', body_path=self.body(10), skill_id='s', yes=True)
        recheck.assert_called_once()
    def test_get_does_not_recheck_the_store(self):
        with patch.object(cli, 'recheck_verified_jdo_store') as recheck, patch.object(cli.subprocess, 'run', return_value=cp()):
            cli.run_jdo_large(url='/rest/x', method='GET', body_path=self.body(10), skill_id='s'); recheck.assert_not_called()


class LargeBodyTests(Setup):
    def large(self): return self.body(bp.COMMAND_LINE_BODY_LIMIT + 5000)
    def test_a_body_over_the_command_line_limit_uses_the_bridge_on_a_312_host(self):
        calls = []
        with patch.object(cli.subprocess, 'run', side_effect=run_as(calls)), patch.object(bp, 'find_bridge_python', return_value=Path('py312.exe')), \
             patch.object(cli, 'ensure_jdo_runtime', return_value=self.rt), \
             patch.object(bp, 'probe_runtime', return_value={'ok': True, 'sha256': 'new-unknown-version'}):
            cli.run_jdo_large(url='/rest/x', method='POST', body_path=self.large(), skill_id='s', yes=True)
        self.assertEqual('py312.exe', str(calls[0][0])); self.assertTrue(any('jdo_large_body_bridge' in str(c) for c in calls[0]))
        self.assertNotIn('--body', calls[0])
    def test_unknown_cli_version_is_accepted_when_the_interface_matches(self):
        with patch.object(cli.subprocess, 'run', side_effect=run_as()), patch.object(bp, 'find_bridge_python', return_value=Path('py312.exe')), \
             patch.object(cli, 'ensure_jdo_runtime', return_value=self.rt), \
             patch.object(bp, 'probe_runtime', return_value={'ok': True, 'sha256': 'ffffffffffff-not-the-old-one'}):
            self.assertEqual(1, cli.run_jdo_large(url='/rest/x', method='POST', body_path=self.large(), skill_id='s', yes=True)['data']['x'])
    def test_no_312_host_gives_a_plain_message_and_sends_nothing(self):
        with patch.object(cli.subprocess, 'run', side_effect=AssertionError('nothing may be sent')), patch.object(bp, 'find_bridge_python', return_value=None):
            with self.assertRaises(cli.JdoError) as c: cli.run_jdo_large(url='/rest/x', method='POST', body_path=self.large(), skill_id='s', yes=True)
        text = str(c.exception)
        for word in ('发品', '整版编辑', '查询', 'Python 3.12'): self.assertIn(word, text)
        for jargon in ('sha', 'hash', 'probe', 'cmd_invoke', 'bridge'): self.assertNotIn(jargon, text.lower())
    def test_changed_interface_gives_the_plain_message_and_sends_nothing(self):
        with patch.object(cli.subprocess, 'run', side_effect=AssertionError('nothing may be sent')), patch.object(bp, 'find_bridge_python', return_value=Path('py312.exe')), \
             patch.object(cli, 'ensure_jdo_runtime', return_value=self.rt), \
             patch.object(bp, 'probe_runtime', return_value={'ok': False, 'userMessage': bp.interface_changed_message(), 'sha256': 'x'}):
            with self.assertRaises(cli.JdoError) as c: cli.run_jdo_large(url='/rest/x', method='POST', body_path=self.large(), skill_id='s', yes=True)
        self.assertIn('大请求', str(c.exception)); self.assertNotIn('cmd_invoke', str(c.exception))
    def test_write_store_check_happens_before_the_bridge_is_even_looked_up(self):
        order = []
        with patch.object(cli, 'recheck_verified_jdo_store', side_effect=lambda: order.append('store')), patch.object(bp, 'find_bridge_python', side_effect=lambda **k: order.append('host') or None):
            with self.assertRaises(cli.JdoError): cli.run_jdo_large(url='/rest/x', method='POST', body_path=self.large(), skill_id='s', yes=True)
        self.assertEqual(['store', 'host'], order)
    def test_a_32kb_boundary_body_is_not_forced_through_the_cli_argument(self):
        self.assertTrue(bp.needs_bridge('x' * (bp.COMMAND_LINE_BODY_LIMIT + 1)))


class BridgeScriptTests(unittest.TestCase):
    def test_bridge_script_has_no_hard_version_or_hash_gate(self):
        text = (ROOT / 'scripts' / 'jdo_large_body_bridge.py').read_text(encoding='utf-8')
        for forbidden in ('0.3.4', 'e1871f6a', 'sha256'): self.assertNotIn(forbidden, text)
    def test_bridge_script_still_refuses_default_profile(self):
        text = (ROOT / 'scripts' / 'jdo_large_body_bridge.py').read_text(encoding='utf-8')
        self.assertIn('explicit non-default', text)


class SelfCheckTests(unittest.TestCase):
    def report(self, **jdo):
        base = {'deps': {'missing': []}, 'permissions': {'writable': True}, 'jdoCli': {'binary': 'x', **jdo}, 'profiles': [], 'store': {}, 'apiKey': {'present': False}, 'sufyImage2': {'found': False}, 'hiOffice': {'listening': True}, 'browser': {}}
        return base
    def test_missing_bridge_never_makes_environment_not_ready(self):
        r = self.report(python312='', runtime='')
        self.assertTrue(bootstrap.readiness(r)['environmentReady'])
    def test_only_the_cli_binary_is_required_for_environment_ready(self):
        self.assertFalse(bootstrap.readiness(self.report(binary=''))['environmentReady']) if False else None
        r = self.report(); r['jdoCli']['binary'] = ''
        self.assertFalse(bootstrap.readiness(r)['environmentReady'])
    def test_bridge_status_is_reported_in_plain_words_in_next_actions(self):
        r = self.report(python312='', runtime=''); r['jdoCli']['bridge'] = bp.bridge_status(exe=Path('x'), python=None, runtime_ok=None)
        actions = bootstrap.next_actions({**r, 'store': {'state': 'ok'}, 'profiles': [{'profile': 'a'}], 'apiKey': {'present': True}, 'sufyImage2': {'found': True}})
        text = ' '.join(a['action'] for a in actions if a['kind'] == 'large-requests')
        for word in ('发品', '整版编辑', '查询', 'Python 3.12'): self.assertIn(word, text)
        for jargon in ('sha', 'hash', 'probe', 'cmd_invoke', 'python312', 'jdo-runtime'): self.assertNotIn(jargon, text.lower())
        self.assertFalse(any(a['kind'] in ('python312', 'jdo-runtime') for a in actions))
    def test_capability_lists_large_requests_separately(self):
        r = self.report(); r['jdoCli']['bridge'] = bp.bridge_status(exe=Path('x'), python=None, runtime_ok=None)
        caps = bootstrap.capability_status(r)
        self.assertFalse(caps['largeRequests']['available']); self.assertTrue(caps['localDraft']['available'])
        for word in ('发品', '整版编辑'): self.assertIn(word, caps['largeRequests']['requires'])

if __name__ == '__main__': unittest.main()
