"""Locate sibling skills without hardcoding one user's home directory.

Lookup order for sufy-image2: SUFY_IMAGE2_SKILL env var, a sibling folder next
to this skill (same skills root, e.g. after installing both from SkillHub),
then the common per-agent skill roots under the current user's home.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping
import re

SKILL_ROOT = Path(__file__).resolve().parents[1]
AGENT_SKILL_ROOTS = (".joycode/skills", ".codex/skills", ".agents/skills", ".claude/skills")


def installed_skill_dir(name: str, *, skill_root: Path | None = None,
                        home: Path | None = None) -> Path | None:
    """Resolve named installs or skill-<id> packages by frontmatter, never a guessed ID."""
    root = Path(skill_root or SKILL_ROOT)
    user = Path(home or Path.home())
    bases = [root.parent] + [user / base for base in AGENT_SKILL_ROOTS]
    for base in dict.fromkeys(bases):
        direct = base / name
        if direct.is_dir():
            return direct
        if not base.is_dir():
            continue
        for candidate in sorted(base.glob("skill-*")):
            entry = candidate / "SKILL.md"
            if not entry.is_file():
                continue
            text = entry.read_text(encoding="utf-8-sig", errors="replace")
            header = text.split("---", 2)
            if len(header) >= 3 and re.search(r"^name:\s*[\"']?" + re.escape(name)
                                             + r"[\"']?\s*$", header[1], re.M):
                return candidate
    return None


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
    installed = installed_skill_dir("sufy-image2", skill_root=root, home=home)
    return installed / "scripts" if installed else candidates[0]
