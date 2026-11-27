"""Replay interruptions through the installed shop-copy batch commands with a read-only official CLI double."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def save(file, value):
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(value, ensure_ascii=False), encoding="utf8")


def fixture():
    args = sys.argv[1:]
    arg = lambda name: args[args.index(name) + 1]
    # The merchant CLI sanitizes inherited custom variables; its validated CLI has its own sibling fixture.
    marker = Path(sys.argv[0]).resolve().parent / "fixture-log.jsonl"
    with marker.open("a", encoding="utf8") as log:
        log.write(json.dumps({"method": arg("--method"), "profile": arg("--profile")}) + "\n")
    assert arg("--method") == "GET", "unexpected platform write"
    source = arg("--profile") == "fixture-source"
    sid = 11 if source else 12
    route = arg("--url")
    params = json.loads(arg("--params")) if "--params" in args else {}
    if route.endswith("/venderinfo"):
        data = {"venderId": sid + 100, "shopBaseInfo": {"shopId": sid, "shopName": arg("--profile"), "type": 0, "status": 1}}
        result = {"success": True, "data": data}
    elif route.endswith("/products"):
        data = ([{"productId": n, "productName": "Fixture", "productStatus": 103} for n in (701, 702)]
                if source and "productStatusList" in params else [])
        result = {"success": True, "data": data, "paginationData": {"pageNum": 1, "totalItems": len(data)}}
    else:
        raise AssertionError("unexpected route " + route)
    print(json.dumps(result))


def run(root, entry=None):
    import pop_copy_contract as support
    merchant = (root / "scripts/merchant.py").is_file()
    with tempfile.TemporaryDirectory(prefix="pop-batch-contract-") as d:
        state = Path(d)
        tools = state / "tools/fixture"
        tools.mkdir(parents=True)
        from unittest.mock import patch
        with patch.object(support, "__file__", str(Path(__file__).resolve())):
            cli = support.cli_double(tools)
        save(state / "tools/official.json", {"root": "tools/fixture", "executable": cli.name,
                                             "executableSha256": sha256(cli.read_bytes()).hexdigest()})
        env = {**os.environ, "HEIDENG_POP_HOME": str(state), "HEIDENG_MERCHANT_HOME": str(state),
               "JDO_CLI_PATH": str(cli), "PYTHONUTF8": "1"}
        if merchant:
            subprocess.run([sys.executable, str(root / "install.py"), "--state", str(state), "--skills-dir", str(state / "skills"),
                            "--offline", "--no-auto-update"], env=env, capture_output=True, check=True, timeout=120)
            root = state / "skills/heideng-ops-agent-merchant"
            prefix = [sys.executable, str(root / "scripts/merchant.py"), "--state", str(state), "run", "shop-copy", "--"]
            sys.path.insert(0, str(root))
            from heideng_merchant import pop_copy_pairing as pairing
        else:
            prefix = entry or [sys.executable, str(root / "scripts/jd_product_pipeline.py"), "shop-copy"]
            sys.path.insert(0, str(root / "scripts"))
            from pop_copy import pop_copy_pairing as pairing

        def call(action, *args):
            proc = subprocess.run([*prefix, action, *map(str, args)], env=env, capture_output=True, text=True,
                                  encoding="utf8", errors="replace", timeout=120)
            try:
                row = json.loads(proc.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError) as exc:
                raise AssertionError((action, proc.returncode, proc.stderr[-800:])) from exc
            return row, proc.returncode

        flags = []
        stores = []
        for side, numeric in [("source", 11), ("target", 12)]:
            profile = "fixture-" + side
            store = {"profile": profile, "shopName": profile, "shopId": str(numeric), "venderId": str(numeric + 100)}
            stores.append(store)
            config = state / (profile + ".json")
            save(config, {"store": store})
            flags.extend(["--" + side + "-profile", profile, "--" + side + "-config", config])

        inv, code = call("inventory", *flags, "--status", "103")
        assert code == 0 and inv["recordedProducts"] == 2, inv
        foreign, code = call("batch-plan", *flags, "--inventory", inv["inventoryFile"], "--product-id", "999")
        assert code != 0, foreign
        selected, code = call("batch-plan", *flags, "--inventory", inv["inventoryFile"], "--product-id", "701")
        assert code == 0 and selected["scopeSize"] == 1, selected
        batch = selected["batchId"]
        source, target = stores
        frozen = pairing.freeze_copy(source, target, {"productInfo": {"productId":701, "productName":"Fixture",
            "categoryDetail":{"lastCategoryId":99}, "itemNum":"fixture-one"},
            "skuList":[{"productId":701,"skuId":7011,"saleAttrs":[]}]})
        task = pairing._digest(["11","111","12","112","701"])
        member = state / "work/shop-copies" / task[:24]
        revision = frozen["sha256"]
        save(member / "revisions" / revision[:24] / "source.json", frozen)
        save(member / "current.json", {"taskId": task, "sourceProductId": "701", "sourceShop": source,
                                       "targetShop": target, "revision": revision})
        original = member / "publication"
        body = original / "publish-body.json"
        save(body, {"productApplyDTO":{"itemNum":"fixture-one"}})
        body_sha = sha256(body.read_bytes()).hexdigest()
        save(original / "publish-body.submit-state.json", {"stage":"SUBMITTING", "payloadSha256":body_sha})

        grant = ["--batch", batch, *flags, "--phase", "bulk", "--confirm", "PUBLISH_FROZEN_SCOPE",
                 "--authorization-ref", "fixture-authorized-scope"]
        held, code = call("batch-run", *grant)
        assert code and held.get("hold", {}).get("reason") == "submission-needs-reconciliation", held
        stopped, code = call("batch-stop", "--batch", batch, "--reason", "fixture pause")
        assert code == 0 and stopped.get("active") is True, stopped
        wrong, code = call("batch-resume", "--batch", batch, *flags, "--stop-id", "wrong", "--authorization-ref", "resume")
        assert code and wrong.get("status") == "batch-stopped", wrong
        resumed, code = call("batch-resume", "--batch", batch, *flags, "--stop-id", stopped["stopId"], "--authorization-ref", "resume")
        assert code and resumed["rows"][0]["status"] == "submission-needs-reconciliation", resumed
        save(original / "rejection.json", {"status":"official-rejection", "payloadSha256":body_sha,
                                          "platformCodes":["1010300506"]})
        uncertain, code = call("batch-resume", "--batch", batch, *flags)
        assert code and uncertain["rows"][0]["status"] == "submission-needs-reconciliation", uncertain
        save(original / "rejection.json", {"status":"official-rejection", "payloadSha256":body_sha,
                                          "noIssuedIds": True, "platformCodes":["1010300506"]})
        refused, code = call("batch-resume", "--batch", batch, *flags)
        assert code and refused["rows"][0]["status"] == "publication-rejected", refused
        status, code = call("batch-status", "--batch", batch)
        assert code == 0 and [r["sourceProductId"] for r in status["rows"]] == ["701"]
        assert sha256(body.read_bytes()).hexdigest() == body_sha
        logged = [json.loads(line) for line in (tools / "fixture-log.jsonl").read_text(encoding="utf8").splitlines()]
        assert logged and all(r["method"] == "GET" for r in logged)
        print(json.dumps({"frozenTargets": 1, "wrongResumeRefused": True, "unknownReplayed": False,
                          "confirmedRefusalRetained": True, "platformWrites": 0}))


if __name__ == "__main__":
    if sys.argv[1:2] == ["invoke"]:
        fixture()
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument("--entry-json", type=json.loads)
        opts = parser.parse_args()
        run(Path(__file__).resolve().parents[1], opts.entry_json)
