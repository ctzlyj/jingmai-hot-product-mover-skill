import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bootstrap  # noqa: E402


class BootstrapTests(unittest.TestCase):
    def test_store_config_states(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.local.json"
            self.assertEqual("missing", bootstrap.store_config_status(path)["state"])
            path.write_text(json.dumps({"store": {"profile": "your-store-profile", "venderId": 0,
                                                   "shopName": "YOUR_SHOP_NAME"}}), encoding="utf-8")
            self.assertEqual("placeholder", bootstrap.store_config_status(path)["state"])
            path.write_text(json.dumps({"store": {"profile": "default", "venderId": 9, "shopName": "x"}}),
                            encoding="utf-8")
            self.assertEqual("invalid", bootstrap.store_config_status(path)["state"])
            path.write_text(json.dumps({"store": {"profile": "pop-a", "venderId": 9, "shopName": "店"},
                                        "business": {"freightTemplateId": 1, "freightTemplateLabel": "t"}}),
                            encoding="utf-8")
            status = bootstrap.store_config_status(path)
            self.assertEqual("ok", status["state"])
            self.assertEqual("pop-a", status["profile"])
            path.write_text(json.dumps({"store": {}, "stores": {"pop-b": {"profile": "pop-b", "venderId": 3,
                                                                          "shopName": "店B"}}}), encoding="utf-8")
            self.assertEqual(["pop-b"], bootstrap.store_config_status(path)["profiles"])

    def test_profiles_hide_default_and_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "pop-a.json").write_text(json.dumps({"profile": "pop-a", "shop_name": "店A", "vender_id": 1,
                                                           "app_key": "secret"}), encoding="utf-8")
            (folder / "default.json").write_text(json.dumps({"profile": "default"}), encoding="utf-8")
            (folder / "pop-b.json").write_text(json.dumps({"profile": "pop-b"}), encoding="utf-8")
            rows = bootstrap.jdo_profiles(folder)
            self.assertEqual([{"profile": "pop-a", "shopName": "店A", "venderId": 1},
                              {"profile": "pop-b", "shopName": "", "venderId": None}], rows)
            self.assertNotIn("secret", json.dumps(rows))

    def test_sibling_jdo_cli_is_found_in_skill_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            exe = home / ".codex" / "skills" / "jd-jingmai-batch-image-refresh" / "vendor" / "jdo-cli" / "bin" / "jdo-cli.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"MZ")
            self.assertEqual(exe, bootstrap.sibling_jdo_cli(home=home, skill_root=home / "x" / "mover"))

    def test_pip_falls_back_to_mirror_after_timeout(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                raise bootstrap.subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            return bootstrap.subprocess.CompletedProcess(command, 0, "", "")

        ok, index, detail = bootstrap.pip_install(Path("python.exe"), ["-r", "requirements.txt"], runner=runner)
        self.assertTrue(ok)
        self.assertEqual(bootstrap.PIP_INDEXES[1], index)
        self.assertNotIn("-i", calls[0])
        self.assertIn(bootstrap.PIP_INDEXES[1], calls[1])

    def test_pip_respects_user_configured_index(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return bootstrap.subprocess.CompletedProcess(command, 0, "", "")

        with patch.dict(bootstrap.os.environ, {"PIP_INDEX_URL": "https://corp/simple"}):
            ok, index, _ = bootstrap.pip_install(Path("python.exe"), ["x"], runner=runner)
        self.assertTrue(ok)
        self.assertEqual("", index)
        self.assertEqual(1, len(calls))

    def test_platform_errors_become_report_items_not_tracebacks(self):
        def boom(*args, **kwargs):
            raise RuntimeError("10403003001 所选类目不是三级类目")

        with patch.object(bootstrap, "refresh_category", boom):
            result = bootstrap.safe_step("reference", bootstrap.refresh_category, "pop-a", 1, "x", None)
        self.assertEqual({"ok": False, "step": "reference", "error": "10403003001 所选类目不是三级类目"}, result)

    def test_python312_persisted_only_when_outside_default_location(self):
        written = []
        default = Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe"
        self.assertFalse(bootstrap.persist_python312(default, writer=lambda *a: written.append(a)))
        self.assertTrue(bootstrap.persist_python312(Path("D:/py312/python.exe"), writer=lambda *a: written.append(a)))
        self.assertEqual([("JDO_PYTHON312", str(Path("D:/py312/python.exe")))], written)

    def test_write_store_config_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.local.json"
            identity = {"profile": "pop-a", "venderId": 9, "shopName": "店"}
            self.assertTrue(bootstrap.write_store_config(path, identity))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(identity, data["store"])
            self.assertFalse(bootstrap.write_store_config(path, {"profile": "pop-b", "venderId": 1, "shopName": "x"}))
            self.assertEqual("pop-a", json.loads(path.read_text(encoding="utf-8"))["store"]["profile"])

    def test_report_never_contains_key_value(self):
        with patch.dict(bootstrap.os.environ, {"JD_LLM_API_KEY": "sk-should-not-leak"}):
            info = bootstrap.describe_key()
        self.assertTrue(info["present"])
        self.assertNotIn("sk-should-not-leak", json.dumps(info))

    def test_next_actions_only_ask_user_for_identity_and_login(self):
        report = {
            "deps": {"missing": []},
            "jdoCli": {"binary": "", "python312": "", "runtime": ""},
            "profiles": [],
            "store": {"state": "missing"},
            "apiKey": {"present": False},
            "sufyImage2": {"found": False},
            "hiOffice": {"listening": False},
        }
        actions = bootstrap.next_actions(report)
        user = [item for item in actions if item["owner"] == "user"]
        self.assertTrue(user)
        self.assertTrue(all(item["kind"] in {"store-identity", "store-login"} for item in user))
        self.assertTrue(any(item["kind"] == "jdo-cli" and item["owner"] == "agent" for item in actions))


if __name__ == "__main__":
    unittest.main()
