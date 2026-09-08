#!/usr/bin/env python3
"""Run the bundled jdo-cli invoke implementation with a body read from disk."""

from __future__ import annotations

import argparse
import json
import marshal
import sys
from pathlib import Path


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()

    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("jdo-cli 0.3.4 runtime bridge requires Python 3.12")

    namespace = {"__name__": "jdo_cli_embedded", "__file__": "jdo-cli.py"}
    code = marshal.loads(args.runtime.read_bytes())
    exec(code, namespace)

    request = json.loads(args.request.read_text(encoding="utf-8"))
    body_path = request.pop("bodyFile", None)
    body = Path(body_path).read_text(encoding="utf-8") if body_path else request.pop("body", None)
    invoke_args = argparse.Namespace(
        url=request["url"],
        method=request.get("method", "GET"),
        params=json.dumps(request["params"], ensure_ascii=False, separators=(",", ":"))
        if request.get("params") is not None else None,
        body=body,
        path_params=request.get("pathParams"),
        profile=request.get("profile"),
        app_key=None,
        app_secret=None,
        access_token=None,
        skill_id=request.get("skillId"),
        request_identity=request.get("requestIdentity", "vender"),
        account_identity=None,
        jos_pin=None,
        extra_headers=None,
        timeout=request.get("timeout", 120),
        dry_run=False,
        yes=bool(request.get("yes", False)),
    )
    namespace["cmd_invoke"](invoke_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
