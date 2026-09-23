# -*- coding: utf-8 -*-
"""Shared batch-root and batch-config resolution for no-inventory mode scripts.

Every no-inventory batch lives in one root directory with the verified layout:

    <batch-root>/
      batch-config.json       profile / venderId / freight / categories / pricing
      ledger.json             item states + generated codes (must be reused)
      store-config.json       shop identity used by A.identity() three-factor checks
      source/                 source workbook dumps
      images/raw|compliant/    original and remediated material
      candidates/<item>/      candidate.json / state.json / normalized images
      reports/                audit / pricing / image / publish receipts

Scripts must never hardcode a batch root; they accept --root (or the
NO_INVENTORY_ROOT environment variable) and load batch-config.json from it.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

CONFIG_NAME = "batch-config.json"


def add_root_argument(parser: argparse.ArgumentParser, required: bool = True) -> None:
    parser.add_argument(
        "--root",
        default=os.environ.get("NO_INVENTORY_ROOT", ""),
        required=required and not os.environ.get("NO_INVENTORY_ROOT"),
        help="无货源批次根目录（含 batch-config.json / ledger.json / candidates/）",
    )


def resolve_root(value: str) -> Path:
    root = Path(str(value or os.environ.get("NO_INVENTORY_ROOT", ""))).expanduser()
    if not str(root).strip("."):
        raise SystemExit("缺少 --root：请指向无货源批次根目录")
    if not (root / CONFIG_NAME).is_file() and not (root / "ledger.json").is_file():
        raise SystemExit(f"--root 不是有效的批次目录（未找到 {CONFIG_NAME} 或 ledger.json）：{root}")
    return root.resolve()


def root_from_argv(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(add_help=False)
    add_root_argument(parser)
    args, _ = parser.parse_known_args(argv)
    return resolve_root(args.root)


def load_config(root: Path) -> dict:
    path = root / CONFIG_NAME
    if not path.is_file():
        raise SystemExit(f"批次缺少 {CONFIG_NAME}：{path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def person_removal_mode(config: dict) -> str:
    """Verified person policy: always removal + natural background fill."""
    mode = str(config.get("personHandling") or "remove")
    if mode != "remove":
        raise SystemExit("无货源模式人物处理只允许 remove 口径（移除人物+背景自然补全）")
    return mode
