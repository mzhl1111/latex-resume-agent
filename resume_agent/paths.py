"""Filesystem layout. Override with env vars when running in a container."""

from __future__ import annotations

import os
from pathlib import Path

def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


# Repo checkout holding AGENTS.md, profile/, templates/, .claude/agents/.
# Set RESUME_AGENT_HOME when the package is pip-installed outside the checkout.
REPO_ROOT = _env_path("RESUME_AGENT_HOME", Path(__file__).resolve().parents[1])


# Where the fact bank, guardrails and template live (read-only inputs).
PROFILE_DIR = _env_path("RESUME_PROFILE_DIR", REPO_ROOT / "profile")
TEMPLATES_DIR = _env_path("RESUME_TEMPLATES_DIR", REPO_ROOT / "templates")
# Where per-role workspaces are written.
JOBS_DIR = _env_path("RESUME_JOBS_DIR", REPO_ROOT / "jobs")

WORK_HISTORY = PROFILE_DIR / "work_history.md"
GUARDRAILS = PROFILE_DIR / "guardrails.json"
RULES = PROFILE_DIR / "rules.md"
BASE_TEMPLATE = TEMPLATES_DIR / "resume_base.tex"
AGENT_INSTRUCTIONS = REPO_ROOT / "AGENTS.md"
EXAMPLES_DIR = REPO_ROOT / "examples"


class ProfileMissing(RuntimeError):
    pass


def require_profile() -> None:
    """Personal data is gitignored; fail clearly if it hasn't been set up."""
    missing = [p for p in (WORK_HISTORY, RULES, BASE_TEMPLATE) if not p.exists()]
    if missing:
        raise ProfileMissing(
            "Missing private profile files: "
            + ", ".join(str(m) for m in missing)
            + ". Run `python3 -m resume_agent init` to copy the fictional examples, then replace them "
            "with your own data (or point RESUME_PROFILE_DIR / RESUME_TEMPLATES_DIR at them)."
        )


def read_rules() -> str:
    return RULES.read_text() if RULES.exists() else ""
