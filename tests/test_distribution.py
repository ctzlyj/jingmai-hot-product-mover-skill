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


if __name__ == "__main__":
    unittest.main()
