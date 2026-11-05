from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_pdd_pricing_works_without_other_installed_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
            result = subprocess.run(
                [sys.executable, "-I", "-c", (
                    "from pathlib import Path; import sys; "
                    "root = Path(sys.argv[1]); "
                    "Path.home = classmethod(lambda cls: root / 'empty-home'); "
                    "sys.path.insert(0, str(root / 'scripts')); "
                    "from pdd_repaired_supplement import solve_price; "
                    "plan = solve_price('39.53'); "
                    "assert str(plan.jd_price) == '109.42'; "
                    "assert str(plan.final_price) == '79.06'"
                ), str(root)],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
        self.assertEqual(0, result.returncode, result.stderr)


class PortablePathTests(unittest.TestCase):
    def test_scripts_do_not_hardcode_a_personal_home_directory(self):
        offenders = []
        for path in sorted((ROOT / "scripts").rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for marker in ("C:\\Users\\", "C:/Users/"):
                if marker in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {marker}")
        self.assertEqual([], offenders)

    def test_tracked_files_do_not_name_local_stores(self):
        """Shop names/venderIds of the stores linked on this machine must not ship."""
        import json

        profiles = Path.home() / ".jdo-cli" / "profiles"
        terms = set()
        for path in profiles.glob("*.json") if profiles.is_dir() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            for key in ("shop_name", "vender_id"):
                value = str(data.get(key) or "").strip()
                if len(value) >= 4:
                    terms.add(value)
        if not terms:
            self.skipTest("no local jdo-cli profiles to compare against")
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, encoding="utf-8")
        if listed.returncode:
            self.skipTest("not a git checkout")
        offenders = []
        for rel in listed.stdout.splitlines():
            path = ROOT / rel
            if path.suffix.lower() not in {".py", ".md", ".json", ".txt", ".yaml", ".yml", ".ps1", ".js"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            offenders += [f"{rel}: {term[:2]}…" for term in terms if term in text]
        self.assertEqual([], offenders)

    def test_sufy_scripts_dir_honours_env_override(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import skill_paths

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sufy-image2" / "scripts"
            target.mkdir(parents=True)
            old = skill_paths.os.environ.get("SUFY_IMAGE2_SKILL")
            skill_paths.os.environ["SUFY_IMAGE2_SKILL"] = str(target.parent)
            try:
                self.assertEqual(target, skill_paths.sufy_scripts_dir())
            finally:
                if old is None:
                    skill_paths.os.environ.pop("SUFY_IMAGE2_SKILL", None)
                else:
                    skill_paths.os.environ["SUFY_IMAGE2_SKILL"] = old

    def test_sufy_scripts_dir_finds_sibling_skill(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import skill_paths

        with tempfile.TemporaryDirectory() as directory:
            skills = Path(directory)
            sibling = skills / "sufy-image2" / "scripts"
            sibling.mkdir(parents=True)
            found = skill_paths.sufy_scripts_dir(
                skill_root=skills / "jd-jingmai-hot-product-mover", env={}, home=skills / "nohome")
            self.assertEqual(sibling, found)


if __name__ == "__main__":
    unittest.main()
