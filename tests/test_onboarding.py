import ast
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import bootstrap as boot
import skill_paths

class OnboardingTests(unittest.TestCase):
    def test_install_guide_uses_skillhub_not_a_single_agent_directory(self):
        text=(ROOT/'references/installation-and-configuration.md').read_text(encoding='utf-8')
        self.assertIn('11153',text)
        self.assertIn('install-skill.md',text)
        self.assertNotIn('%USERPROFILE%\\.joycode\\skills\\jd-jingmai-hot-product-mover',text)
        for word in ('skill-<id>', '.codex', '.agents', '.joycode', '无 Key', '京麦 CLI 店铺连接'):
            self.assertIn(word,text)
    def test_skill_starts_with_version_prompt_and_selfcheck(self):
        text=(ROOT/'SKILL.md').read_text(encoding='utf-8')
        self.assertRegex(text.split('---')[1], r'version: 1\.1\.[012]')
        top=text.split('## 使用前')[0]
        for word in ('11153@最新版本','bootstrap.py','environmentReady','businessReady','结果目录'):
            self.assertIn(word,top)
    def test_skill_id_directory_resolution_uses_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            root=home/'.agents/skills/skill-55'; root.mkdir(parents=True)
            (root/'SKILL.md').write_text('---\nname: sufy-image2\n---\n',encoding='utf-8')
            (root/'scripts').mkdir(); (root/'scripts/sufy_image2.py').touch()
            found=skill_paths.sufy_scripts_dir(skill_root=home/'mover',home=home,env={})
            self.assertEqual(root/'scripts',found)
    def test_each_supported_skill_root_resolves_sibling_cli_by_name(self):
        for base in (".joycode/skills", ".codex/skills", ".agents/skills"):
            with self.subTest(base=base), tempfile.TemporaryDirectory() as tmp:
                home=Path(tmp)
                sibling=home/base/"skill-10833"
                sibling.mkdir(parents=True)
                (sibling/"SKILL.md").write_text("---\nname: jd-jingmai-batch-image-refresh\n---\n", encoding="utf-8")
                exe=sibling/"vendor/jdo-cli/bin/jdo-cli.exe"
                exe.parent.mkdir(parents=True); exe.touch()
                self.assertEqual(exe, boot.sibling_jdo_cli(home=home,skill_root=home/"mover"))

    def test_empty_home_browser_smoke_skips_all_instead_of_error(self):
        sys.path.insert(0,str(ROOT/'tests'))
        spec=importlib.util.spec_from_file_location('optional_smoke',ROOT/'tests/browser_collector_smoke.py')
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path,'home',return_value=Path(tmp)), \
             patch.object(skill_paths,'SKILL_ROOT',Path(tmp)/'mover'):
            suite=unittest.defaultTestLoader.loadTestsFromModule(module)
            result=unittest.TestResult(); suite.run(result)
        self.assertEqual([],result.errors)
        self.assertEqual([],result.failures)
        self.assertEqual(3,len(result.skipped))
    def test_key_check_never_reads_registry_value(self):
        with patch.dict(os.environ,{'JD_LLM_API_KEY':'','GPT_IMAGE2_API_KEY':''}), \
             patch.object(boot,'user_env',side_effect=AssertionError('secret value read')):
            boot.describe_key()
    def test_no_key_does_not_block_non_generation_tasks(self):
        report={'deps':{'missing':[]},'permissions':{'writable':True},
                'jdoCli':{'binary':'exe','python312':'py','runtime':'rt'},
                'store':{'state':'ok'},'verifiedIdentity':{'profile':'pop-test','venderId':9,'shopName':'店'},
                'apiKey':{'present':False},'sufyImage2':{'found':False,'credential':False},
                'browser':{'found':False,'router':False},'errors':[]}
        state=boot.readiness(report)
        self.assertTrue(state['environmentReady'])
        self.assertTrue(state['businessReady'])
        caps=boot.capability_status(report)
        self.assertTrue(caps['localDraft']['available'])
        self.assertTrue(caps['prePublishValidation']['available'])
        self.assertFalse(caps['companyGeneration']['available'])
        self.assertFalse(caps['externalGeneration']['available'])
        self.assertFalse(caps['browserCollection']['available'])
    def test_platform_errors_cannot_report_business_ready(self):
        state=boot.readiness({'deps':{'missing':[]},'permissions':{'writable':True},
                              'jdoCli':{'binary':'exe','python312':'py','runtime':'rt'},
                              'store':{'state':'ok'},'verifiedIdentity':{'profile':'pop-test'},
                              'errors':[{'step':'reference','error':'blocked'}]})
        self.assertFalse(state['businessReady'])
    def test_incomplete_or_mismatched_store_identity_is_not_ready(self):
        report={'deps':{'missing':[]},'permissions':{'writable':True},
                'jdoCli':{'binary':'exe','python312':'py','runtime':'rt'},
                'store':{'state':'ok'},'errors':[],
                'verifiedIdentity':{'profile':'pop-test','venderId':0,'shopName':''}}
        self.assertFalse(boot.readiness(report)['businessReady'])
        report['verifiedIdentity'].update(venderId=9,shopName='店')
        report['storeIdentityMatches']=False
        self.assertFalse(boot.readiness(report)['businessReady'])

    def test_permission_probe_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            self.assertTrue(boot.directory_writable(folder))
            self.assertEqual([],list(folder.iterdir()))
    def test_bootstrap_contains_no_fixed_local_port_probe(self):
        text=(ROOT/'scripts/bootstrap.py').read_text(encoding='utf-8')
        self.assertNotIn('HIOFFICE_PORTS',text)
        self.assertNotIn('connect_ex',text)
    def test_no_auto_oauth_instructions(self):
        actions=boot.next_actions({'deps':{'missing':[]},'jdoCli':{},'profiles':[],
                   'store':{'state':'missing'},'apiKey':{'present':False},
                   'sufyImage2':{'found':False},'hiOffice':{'listening':False}})
        login=[x for x in actions if x['kind']=='store-login'][0]['action']
        self.assertIn('京麦 CLI 店铺连接',login)
        self.assertNotIn('jdo-cli login',login)

if __name__=='__main__': unittest.main()
