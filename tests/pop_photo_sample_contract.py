"""One acknowledged preview, reused at the same slot through either installed command."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def run(root, prefix=None):
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp).resolve()
        package = state / "listing"
        package.mkdir()
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in ("PATH", "WINDIR", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "PATHEXT")
        }
        env.update(HOME=str(state), USERPROFILE=str(state), HEIDENG_MERCHANT_HOME=str(state), PYTHONUTF8="1")
        merchant = (root / "scripts/merchant.py").is_file()
        if merchant:
            installed = state / "skills/heideng-ops-agent-merchant"
            subprocess.run(
                [
                    sys.executable,
                    str(root / "install.py"),
                    "--skills-dir",
                    str(installed.parent),
                    "--state",
                    str(state),
                    "--offline",
                    "--no-auto-update",
                ],
                env=env,
                check=True,
                capture_output=True,
            )
            prefix = [
                sys.executable,
                str(installed / "scripts/merchant.py"),
                "--state",
                str(state),
                "run",
                "photo-listing",
                "--",
            ]
        elif prefix is None:
            prefix = [sys.executable, str(root / "scripts/photo_listing.py")]

        def call(command, *args, expect_ok=True):
            result = subprocess.run(
                [*prefix, command, "--package", str(package), *map(str, args)],
                env=env,
                capture_output=True,
                timeout=90,
            )
            assert (result.returncode == 0) == expect_ok, (
                command,
                result.stdout[-600:],
                result.stderr[-400:],
            )
            return json.loads(result.stdout)

        manifest = {
            "schemaVersion": "listing-package/v1",
            "mode": "photo-new-listing",
            "packageId": "fixture",
            "title": "Fixture item",
            "facts": {"title": "Fixture item", "skus": []},
            "sourcePhotos": ["source.jpg"],
            "reference": {"questionAnswered": True},
            "store": {"profile": "fixture-store", "shopId": 31, "venderId": 51, "shopName": "Fixture shop"},
            "mainImages": [],
            "variants": [],
            "detailImages": [],
            "timings": {},
        }
        write(package / "manifest.json", manifest)
        Image.new("RGB", (64, 64), "white").save(package / "source.jpg")
        design = package / "design.json"
        write(
            design,
            {
                "images": [
                    {"id": "main-01", "role": "main", "photos": ["source.jpg"], "prompt": "fixture preview"}
                ]
            },
        )
        sample = Path(
            call("make-sample", "--image-prompts-file", design, "--image-id", "main-01")["samplePackage"]
        )
        # Seed the original completed provider receipt. No provider or shop is contacted.
        observed = json.loads((sample / "manifest.json").read_bytes())
        Image.new("RGB", (64, 64), "blue").save(sample / "preview.jpg")
        digest = sha256((sample / "preview.jpg").read_bytes()).hexdigest()
        observed["mainImages"] = [
            {"id": "main-01", "previewOnly": True, "localPath": "preview.jpg", "sha256": digest}
        ]
        observed["samplePlanSha256"] = sha256((sample / "plan.json").read_bytes()).hexdigest()
        write(sample / "manifest.json", observed)
        accepted = call("accept-sample", "--image-id", "main-01", "--evidence", "fixture approval")
        context = call("sample-context", "--image-id", "main-01")
        assert context["sha256"] == digest and context["reusePhoto"] == accepted["reusePhoto"]
        write(design, {"images": [{"id": "main-01", "role": "main", "reusePhoto": accepted["reusePhoto"]}]})
        call("make-plan", "--preview", "--image-prompts-file", design)
        plan = json.loads((package / "plan.json").read_bytes())
        assert len(plan) == 1 and plan[0]["reusePhoto"] == accepted["reusePhoto"]
        if merchant:
            assert json.loads((package / "image-quote.json").read_bytes())["maxRequests"] == 0
        generated = call("generate")
        assert generated.get("submittedCount", 0) == 0
        final = json.loads((package / "manifest.json").read_bytes())["mainImages"]
        assert (
            len(final) == 1 and sha256((package / final[0]["localPath"]).read_bytes()).hexdigest() == digest
        )
        (package / accepted["reusePhoto"]).write_bytes(b"changed")
        call("sample-context", "--image-id", "main-01", expect_ok=False)
        print(
            json.dumps(
                {"approvedSamples": 1, "reusedImages": 1, "newRequests": 0, "changedBytesRefused": True}
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry-json", type=json.loads)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    run(args.root, args.entry_json)
