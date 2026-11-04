"""Locate sibling skills without hardcoding one user's home directory.

Lookup order for sufy-image2: SUFY_IMAGE2_SKILL env var, a sibling folder next
to this skill (same skills root, e.g. after installing both from SkillHub),
then the common per-agent skill roots under the current user's home.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

SKILL_ROOT = Path(__file__).resolve().parents[1]
AGENT_SKILL_ROOTS = (".joycode/skills", ".codex/skills", ".agents/skills", ".claude/skills")


def sufy_scripts_dir(
    skill_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if env is None else env
    override = str(env.get("SUFY_IMAGE2_SKILL") or "").strip()
    if override:
        return Path(override).expanduser() / "scripts"
    root = Path(skill_root or SKILL_ROOT)
    candidates = [root.parent / "sufy-image2" / "scripts"]
    home = Path(home or Path.home())
    candidates += [home / base / "sufy-image2" / "scripts" for base in AGENT_SKILL_ROOTS]
    for candidate in candidates:
        if (candidate / "sufy_image2.py").is_file() or candidate.is_dir():
            return candidate
    return candidates[0]
