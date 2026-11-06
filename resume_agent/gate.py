"""Review records and the delivery gate.

A review file (``jobs/<slug>/review-N.md``) is written only by the harness (the
Claude Code SubagentStop hook, or the server's verifier). Its first line records
the verdict and the SHA-256 of ``resume.tex`` at verification time, so the gate
can tell whether the PASS still applies to the current resume.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from . import tracing

HEADER_RE = re.compile(
    r"<!-- harness: verdict=(?P<verdict>[A-Z ]+?) tex_sha256=(?P<sha>[0-9a-f]{64}) must_fix=(?P<n>\d+) -->"
)
MAX_ROUNDS = 3


def tex_hash(tex: Path) -> str:
    return hashlib.sha256(tex.read_bytes()).hexdigest()


def parse_verdict(review: str) -> tuple[str | None, list[str]]:
    m = re.search(r"VERDICT:\s*(PASS|NEEDS CHANGES)", review)
    verdict = m.group(1) if m else None
    sec = re.search(r"MUST FIX\s*\n(.*?)(?:\n\s*CONSIDER|\Z)", review, re.S)
    items = []
    if sec:
        items = [ln.strip()[2:].strip() for ln in sec.group(1).splitlines() if ln.strip().startswith("- ")]
        items = [i for i in items if i.lower().rstrip(".") != "none"]
    if verdict == "PASS" and items:
        verdict = "NEEDS CHANGES"  # a PASS with open MUST FIX items is not a pass
    return verdict, items


def record_review(job_dir: Path, review: str, *, source: str) -> Path:
    """Write review-N.md with a harness header. Returns the path."""
    verdict, must_fix = parse_verdict(review)
    n = len(list(job_dir.glob("review-*.md"))) + 1
    path = job_dir / f"review-{n}.md"
    header = (
        f"<!-- harness: verdict={verdict or 'MALFORMED'} tex_sha256={tex_hash(job_dir / 'resume.tex')} "
        f"must_fix={len(must_fix)} -->\n<!-- source: {source} -->\n\n"
    )
    path.write_text(header + review.strip() + "\n")
    with tracing.span("review.record", job=job_dir.name, verdict=verdict or "MALFORMED", must_fix=len(must_fix),
                      round=n, source=source.split(" ")[0]):
        pass
    return path


@dataclass
class Review:
    path: Path
    verdict: str
    tex_sha256: str
    must_fix: int


def reviews(job_dir: Path) -> list[Review]:
    out = []
    for p in sorted(job_dir.glob("review-*.md"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)):
        m = HEADER_RE.search(p.read_text().split("\n", 1)[0])
        if m:  # files without a harness header (hand-written) never count
            out.append(Review(p, m.group("verdict"), m.group("sha"), int(m.group("n"))))
    return out


@dataclass
class GateStatus:
    deliverable: bool
    reason: str
    rounds: int
    latest: Review | None


def delivery_status(job_dir: Path, *, accept_open_issues: bool = False) -> GateStatus:
    tex = job_dir / "resume.tex"
    if not (job_dir / "resume.pdf").exists():
        return GateStatus(False, "no resume.pdf", 0, None)
    revs = reviews(job_dir)
    if not revs:
        return GateStatus(False, "no independent review recorded yet", 0, None)
    latest = revs[-1]
    if latest.tex_sha256 != tex_hash(tex):
        return GateStatus(False, f"resume.tex changed after {latest.path.name}; run a fresh verifier", len(revs), latest)
    if latest.verdict == "PASS":
        return GateStatus(True, f"{latest.path.name}: PASS", len(revs), latest)
    if accept_open_issues and len(revs) >= MAX_ROUNDS:
        return GateStatus(True, f"{latest.path.name}: {latest.verdict} after {len(revs)} rounds (accepted)", len(revs), latest)
    return GateStatus(
        False,
        f"{latest.path.name}: {latest.verdict}; revise and re-verify"
        + (f" (or pass --accept-open-issues after {MAX_ROUNDS} rounds)" if len(revs) < MAX_ROUNDS else
           " (pass --accept-open-issues to deliver with the open items)"),
        len(revs),
        latest,
    )
