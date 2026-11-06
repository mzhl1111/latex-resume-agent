"""End-to-end generation with the Anthropic API (server mode only).

Claude Code mode does NOT use this: there, Claude itself is the writer and the
``resume-proofreader`` subagent is the reviewer, on the user's plan tokens.
A deployed server can't use plan tokens, so ``generate_resume`` uses an
``ANTHROPIC_API_KEY`` and mirrors the same loop:

    fetch JD -> writer drafts analysis + tex -> mechanical proofread -> fix
    -> independent visual reviewer (same prompt as the subagent) -> fix -> ...
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import gate, paths, tracing
from .jd_fetch import fetch_jd, slugify
from .proofread import proofread
from .workspace import JobWorkspace, new_job

DEFAULT_WRITER_MODEL = os.environ.get("RESUME_WRITER_MODEL", "claude-sonnet-5-5")
DEFAULT_REVIEWER_MODEL = os.environ.get("RESUME_REVIEWER_MODEL", "claude-sonnet-5-5")
REVIEWER_PROMPT = paths.REPO_ROOT / ".claude" / "agents" / "resume-proofreader.md"

WRITER_SYSTEM = """You tailor a one-page LaTeX resume to a job description.
Follow these project rules exactly:

{agents_md}

Output format, every time:
<analysis>
markdown: positioning, requirements-to-evidence table, gaps, edit plan
</analysis>
<resume_tex>
the COMPLETE .tex file, starting with \\documentclass
</resume_tex>
Edit only the document body (never the preamble). Every number must come from the fact bank."""


class PipelineError(RuntimeError):
    pass


@dataclass
class GenerationResult:
    ok: bool
    workspace: dict
    rounds: int
    verdict: str
    remaining_issues: list[str] = field(default_factory=list)
    review: str = ""
    log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise PipelineError("Install the 'anthropic' package to use generate_resume") from exc
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise PipelineError(
            "ANTHROPIC_API_KEY is not set. Either set it on the server, or call the low-level tools "
            "(start_job, get_resume_context, write_resume, proofread_resume) and do the writing yourself."
        )
    return anthropic.Anthropic()


def _llm(client: Any, *, role: str, job: str, **kwargs: Any) -> Any:
    """One model call, traced with token usage."""
    with tracing.span("llm.call", role=role, model=kwargs.get("model"), job=job) as s:
        resp = client.messages.create(**kwargs)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            s.set(input_tokens=getattr(usage, "input_tokens", 0) or 0,
                  output_tokens=getattr(usage, "output_tokens", 0) or 0)
        s.set(stop_reason=getattr(resp, "stop_reason", None))
        return resp


def _text(resp: Any) -> str:
    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")


def _tag(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.S)
    return m.group(1) if m else None


def _strip_frontmatter(md: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", md, flags=re.S).strip()


def _review_issues(review: str) -> list[str]:
    m = re.search(r"MUST FIX\s*\n(.*?)(?:\n\s*CONSIDER|\Z)", review, re.S)
    if not m:
        return []
    items = [ln.strip()[2:].strip() for ln in m.group(1).splitlines() if ln.strip().startswith("- ")]
    return [i for i in items if i.lower() not in {"none", "none."}]


def generate_resume(**kwargs: Any) -> "GenerationResult":
    """Full server-side pipeline (traced as ``resume.generate``)."""
    with tracing.span("resume.generate", has_url=bool(kwargs.get("jd_url")), writer_model=kwargs.get("writer_model"),
                      instructions=bool(kwargs.get("instructions"))) as s:
        res = _generate_resume(**kwargs)
        s.set(job=res.workspace["slug"], ok=res.ok, verdict=res.verdict, rounds=res.rounds,
              remaining=len(res.remaining_issues))
        return res


def _generate_resume(
    *,
    jd_url: str | None = None,
    jd_text: str | None = None,
    slug: str | None = None,
    instructions: str = "",
    max_rounds: int = 3,
    writer_model: str = DEFAULT_WRITER_MODEL,
    reviewer_model: str = DEFAULT_REVIEWER_MODEL,
    client: Any = None,
    jobs_dir: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> GenerationResult:
    log: list[str] = []

    def note(msg: str) -> None:
        log.append(msg)
        if progress:
            progress(msg)

    if not (jd_url or jd_text):
        raise PipelineError("Provide jd_url or jd_text")
    try:
        paths.require_profile()
    except paths.ProfileMissing as exc:
        raise PipelineError(str(exc)) from exc
    client = client or _client()

    # 1. JD + workspace
    if jd_url and not jd_text:
        jd = fetch_jd(jd_url)
        ws: JobWorkspace = new_job(slug or slugify(jd.company, jd.title), jd.to_markdown(), jobs_dir=jobs_dir)
        note(f"Fetched JD via {jd.source}: {jd.title} @ {jd.company}")
    else:
        md = jd_text if jd_text.lstrip().startswith("#") else f"# Job Description\n\n{jd_text}"
        if jd_url:
            md += f"\n\nSource: {jd_url}\n"
        ws = new_job(slug or slugify(jd_text[:60]), md, jobs_dir=jobs_dir)
        note("Using provided JD text")

    agents_md = paths.AGENT_INSTRUCTIONS.read_text() + "\n\n" + paths.read_rules()
    system = WRITER_SYSTEM.format(agents_md=agents_md)
    context = (
        f"<work_history>\n{paths.WORK_HISTORY.read_text()}\n</work_history>\n\n"
        f"<base_template>\n{paths.BASE_TEMPLATE.read_text()}\n</base_template>\n\n"
        f"<job_description>\n{ws.jd.read_text()}\n</job_description>\n\n"
        + (f"<extra_instructions>\n{instructions}\n</extra_instructions>\n\n" if instructions else "")
        + "Produce the analysis and the tailored resume."
    )
    messages: list[dict] = [{"role": "user", "content": context}]

    def write_round() -> None:
        resp = _llm(client, role="writer", job=ws.slug, model=writer_model, max_tokens=8000, system=system,
                    messages=messages)
        out = _text(resp)
        messages.append({"role": "assistant", "content": out})
        tex = _tag(out, "resume_tex")
        if not tex or "\\documentclass" not in tex:
            raise PipelineError("Writer did not return a complete <resume_tex>")
        analysis = _tag(out, "analysis")
        if analysis:
            ws.analysis.write_text(analysis.strip() + "\n")
        ws.tex.write_text(tex.strip() + "\n")

    review, verdict, remaining = "", "NOT REVIEWED", []
    rounds = 0
    write_round()
    note("Draft written")

    for rounds in range(1, max_rounds + 1):
        # 2. mechanical proofread, with up to 2 quick fix-ups per round
        for attempt in range(3):
            rep = proofread(ws.tex)
            ws.proofread.write_text(rep.to_markdown())
            blocking = [i for i in rep.issues if i.severity in ("error", "warn")]
            if not blocking or attempt == 2:
                break
            note(f"Round {rounds}: proofread found {len(blocking)} issue(s); revising")
            messages.append(
                {
                    "role": "user",
                    "content": "The compiled resume failed mechanical proofreading:\n\n"
                    + rep.to_markdown()
                    + "\nFix every ERROR and WARN by editing content (not fonts/spacing). "
                    "Return the full output format again.",
                }
            )
            write_round()
        if not rep.ok:
            remaining = [f"{i.check}: {i.message} {i.detail}".strip() for i in rep.issues if i.severity == "error"]
            verdict = "FAILED MECHANICAL CHECKS"
            break

        # 3. independent verifier: a fresh conversation built only from artifacts
        v = verify_resume(ws, client=client, model=reviewer_model, round_no=rounds)
        review = v.review
        remaining, verdict = v.must_fix, v.verdict
        note(f"Round {rounds}: verifier verdict {verdict} ({len(remaining)} must-fix)")
        if verdict == "PASS" or rounds == max_rounds:
            break
        messages.append(
            {
                "role": "user",
                "content": "The independent verifier returned:\n\n" + review
                + "\n\nApply every MUST FIX item; apply CONSIDER items where they help. "
                "Return the full output format again.",
            }
        )
        write_round()

    ok = verdict == "PASS"
    return GenerationResult(ok, ws.describe(), rounds, verdict, remaining, review, log)


# --------------------------------------------------------------------------- verifier


@dataclass
class Verification:
    verdict: str  # "PASS" | "NEEDS CHANGES" | "FAILED MECHANICAL CHECKS"
    must_fix: list[str]
    review: str
    review_path: str


def verifier_inputs(ws: JobWorkspace) -> list[dict]:
    """The verifier's entire world: source facts, rules, JD, and the produced artifacts.

    Deliberately excludes analysis.md, earlier reviews and the writer conversation, so the
    verifier judges the resume the way a recruiter and a fact-checker would.
    """
    pdf_text = subprocess.run(
        ["pdftotext", "-layout", str(ws.pdf), "-"], capture_output=True, text=True, check=True
    ).stdout
    png_b64 = base64.standard_b64encode(ws.png.read_bytes()).decode()
    blocks = [
        ("work_history", paths.WORK_HISTORY.read_text()),
        ("agents_md", paths.AGENT_INSTRUCTIONS.read_text()),
        ("profile_rules", paths.read_rules()),
        ("jd", ws.jd.read_text()),
        ("proofread_md", ws.proofread.read_text()),
        ("resume_pdf_text", pdf_text),
        ("resume_tex", ws.tex.read_text()),
    ]
    content: list[dict] = [{"type": "text", "text": f"<{k}>\n{v}\n</{k}>"} for k, v in blocks]
    content += [
        {"type": "text", "text": "Rendered page (resume.png):"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png_b64}},
        {
            "type": "text",
            "text": "Verify it now. The files are inlined above and the mechanical proofread was just "
            "re-run for you; you cannot run tools.",
        },
    ]
    return content


def verify_resume(
    ws: JobWorkspace,
    *,
    client: Any = None,
    model: str = DEFAULT_REVIEWER_MODEL,
    round_no: int | None = None,
) -> Verification:
    """Run one independent verification in a brand-new conversation."""
    with tracing.span("resume.verify", job=ws.slug, model=model, source="server") as s:
        v = _verify(ws, client=client, model=model)
        s.set(verdict=v.verdict, must_fix=len(v.must_fix))
        return v


def _verify(ws: JobWorkspace, *, client: Any, model: str) -> Verification:
    client = client or _client()
    rep = proofread(ws.tex)  # fresh compile + report; never trust an old one
    ws.proofread.write_text(rep.to_markdown())
    if not ws.pdf.exists() or not ws.png.exists():
        issues = [f"{i.check}: {i.message}" for i in rep.issues if i.severity == "error"]
        return Verification("FAILED MECHANICAL CHECKS", issues, rep.to_markdown(), "")
    resp = _llm(
        client,
        role="verifier",
        job=ws.slug,
        model=model,
        max_tokens=2000,
        system=_strip_frontmatter(REVIEWER_PROMPT.read_text()),
        messages=[{"role": "user", "content": verifier_inputs(ws)}],
    )
    review = _text(resp)
    path = gate.record_review(ws.dir, review, source=f"server verifier ({model})")
    must_fix = _review_issues(review)
    if not rep.ok:
        must_fix = [f"{i.check}: {i.message}" for i in rep.issues if i.severity == "error"] + must_fix
    verdict = "PASS" if "VERDICT: PASS" in review and not must_fix else "NEEDS CHANGES"
    return Verification(verdict, must_fix, review, str(path))
