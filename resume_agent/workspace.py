"""Per-role workspaces under ``jobs/<slug>/``."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import paths


@dataclass
class JobWorkspace:
    slug: str
    dir: Path

    @property
    def jd(self) -> Path:
        return self.dir / "jd.md"

    @property
    def analysis(self) -> Path:
        return self.dir / "analysis.md"

    @property
    def tex(self) -> Path:
        return self.dir / "resume.tex"

    @property
    def pdf(self) -> Path:
        return self.dir / "resume.pdf"

    @property
    def png(self) -> Path:
        return self.dir / "resume.png"

    @property
    def proofread(self) -> Path:
        return self.dir / "proofread.md"

    def describe(self) -> dict[str, str]:
        return {
            "slug": self.slug,
            "dir": str(self.dir),
            "jd": str(self.jd),
            "analysis": str(self.analysis),
            "tex": str(self.tex),
            "pdf": str(self.pdf),
            "png": str(self.png),
        }


def _validate_slug(slug: str) -> str:
    if not slug or "/" in slug or slug.startswith(".") or ".." in slug:
        raise ValueError(f"Invalid job slug: {slug!r}")
    return slug


def get_job(slug: str, jobs_dir: Path | None = None) -> JobWorkspace:
    jobs_dir = jobs_dir or paths.JOBS_DIR
    return JobWorkspace(_validate_slug(slug), jobs_dir / slug)


def new_job(
    slug: str,
    jd_markdown: str | None = None,
    *,
    jobs_dir: Path | None = None,
    overwrite: bool = False,
) -> JobWorkspace:
    """Create ``jobs/<slug>/`` with jd.md, analysis.md and a fresh resume.tex.

    If the folder exists and ``overwrite`` is False, a numeric suffix is added
    so earlier tailored versions are never clobbered.
    """
    paths.require_profile()
    jobs_dir = jobs_dir or paths.JOBS_DIR
    base = _validate_slug(slug)
    ws = get_job(base, jobs_dir)
    n = 2
    while ws.dir.exists() and not overwrite:
        ws = get_job(f"{base}-{n}", jobs_dir)
        n += 1
    ws.dir.mkdir(parents=True, exist_ok=True)
    ws.jd.write_text(jd_markdown or "# Job Description\n\nPaste the JD text or URL notes here.\n")
    ws.analysis.write_text("# JD → Evidence Mapping\n\n")
    shutil.copy(paths.BASE_TEMPLATE, ws.tex)
    return ws
