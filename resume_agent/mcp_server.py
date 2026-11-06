"""MCP server: lets other agents tailor resumes.

Two ways to use it:

* **Caller does the writing** (no API key on the server): ``start_job`` ->
  ``get_resume_context`` -> write the .tex yourself -> ``write_resume`` (which
  compiles + proofreads and returns the rendered page) -> iterate ->
  ``get_resume_pdf``. The ``tailor_resume`` prompt describes this loop.
* **Server does the writing**: ``generate_resume`` runs the full pipeline with the
  server's ANTHROPIC_API_KEY and returns the PDF.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import anyio
from pydantic import BaseModel, Field
import mcp.types as t
from mcp.server.mcpserver import MCPServer

from . import __version__, applications, paths, tracing
from .jd_fetch import FetchError, fetch_jd, slugify
from .proofread import proofread
from .workspace import get_job, new_job

INSTRUCTIONS = """Tailors the server owner's one-page LaTeX resume to a job posting and proofreads the PDF.
Fast path: call generate_resume with a job URL (needs the server's API key).
Do-it-yourself path (works without a server key): start_job -> get_resume_context ->
write_resume (repeat until proofread passes and the page looks right) -> get_resume_pdf.
Never invent facts: every claim and number must come from the work history returned by
get_resume_context.
After you actually SUBMIT an application with the generated PDF, call record_application with the
job URL, the slug (or the exact PDF bytes you uploaded), any cover letter, and every application
question with the answer you gave. Report later status changes with update_application."""

# LaTeX primitives that could read/write files outside the job folder.
_UNSAFE_TEX = re.compile(r"\\(input|include|openin|openout|write|write18|immediate|read|catcode|csname)(?![a-zA-Z])")

async def _trace_tool_calls(ctx, call_next):
    """Server middleware: one span per tools/call, named mcp.tool with the tool name."""
    if getattr(ctx, "method", None) != "tools/call":
        return await call_next(ctx)
    params = getattr(ctx, "params", None) or {}
    name = params.get("name") if isinstance(params, dict) else getattr(params, "name", None)
    args = (params.get("arguments") if isinstance(params, dict) else getattr(params, "arguments", None)) or {}
    with tracing.span("mcp.tool", tool=name or "", job=args.get("slug") if isinstance(args, dict) else None) as s:
        result = await call_next(ctx)
        is_error = getattr(result, "is_error", None)
        if is_error is None and isinstance(result, dict):
            is_error = result.get("isError")
        s.set(is_error=bool(is_error))
        if is_error:
            s.set_error("tool returned isError")
        return result


mcp = MCPServer(name="latex-resume-agent", version=__version__, instructions=INSTRUCTIONS,
                middleware=[_trace_tool_calls])


def _pdf_resource(slug: str) -> t.EmbeddedResource:
    ws = get_job(slug)
    return t.EmbeddedResource(
        type="resource",
        resource=t.BlobResourceContents(
            uri=f"resume://jobs/{slug}/resume.pdf",
            mime_type="application/pdf",
            blob=base64.b64encode(ws.pdf.read_bytes()).decode(),
        ),
    )


def _png(slug: str) -> t.ImageContent | None:
    ws = get_job(slug)
    if not ws.png.exists():
        return None
    return t.ImageContent(type="image", data=base64.b64encode(ws.png.read_bytes()).decode(), mime_type="image/png")


def _report_result(slug: str) -> t.CallToolResult:
    ws = get_job(slug)
    if not ws.tex.exists():
        return t.CallToolResult(content=[t.TextContent(type="text", text=f"No job '{slug}'")], is_error=True)
    rep = proofread(ws.tex)
    ws.proofread.write_text(rep.to_markdown())
    content: list = [t.TextContent(type="text", text=rep.to_markdown())]
    img = _png(slug)
    if img:
        content.append(t.TextContent(type="text", text="Rendered page — inspect it for visual problems:"))
        content.append(img)
    return t.CallToolResult(content=content, structured_content={"slug": slug, **rep.to_dict()})


@mcp.tool(structured_output=True, description="Fetch a job posting URL and return its title, company, location and description as markdown.")
def fetch_job_description(url: str) -> dict[str, Any]:
    try:
        jd = fetch_jd(url)
    except FetchError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **jd.to_dict(), "markdown": jd.to_markdown()}


@mcp.tool(
    structured_output=True,
    description="Create a job workspace from a job URL or pasted JD text. Returns the slug used by the other "
    "tools and the JD markdown. If URL fetching fails, fetch the page yourself and pass jd_text."
)
def start_job(jd_url: str | None = None, jd_text: str | None = None, slug: str | None = None) -> dict[str, Any]:
    try:
        paths.require_profile()
        if jd_text:
            md = jd_text if jd_text.lstrip().startswith("#") else f"# Job Description\n\n{jd_text}"
            if jd_url:
                md += f"\n\nSource: {jd_url}\n"
            ws = new_job(slug or slugify(jd_text[:60]), md)
        elif jd_url:
            jd = fetch_jd(jd_url)
            ws = new_job(slug or slugify(jd.company, jd.title), jd.to_markdown())
        else:
            return {"ok": False, "error": "Provide jd_url or jd_text"}
    except (FetchError, paths.ProfileMissing) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "slug": ws.slug, "jd_markdown": ws.jd.read_text()}


@mcp.tool(
    structured_output=True,
    description="Everything needed to write the tailored resume: rules, factual work history (the only "
    "allowed source of claims and numbers), the base LaTeX template, guardrails, the reviewer checklist, "
    "and (if slug given) the job's JD and current resume.tex."
)
def get_resume_context(slug: str | None = None) -> dict[str, Any]:
    try:
        paths.require_profile()
    except paths.ProfileMissing as exc:
        return {"ok": False, "error": str(exc)}
    ctx = {
        "ok": True,
        "rules_md": paths.AGENT_INSTRUCTIONS.read_text(),
        "profile_rules_md": paths.read_rules(),
        "work_history_md": paths.WORK_HISTORY.read_text(),
        "base_template_tex": paths.BASE_TEMPLATE.read_text(),
        "guardrails": json.loads(paths.GUARDRAILS.read_text())["rules"] if paths.GUARDRAILS.exists() else [],
        "reviewer_checklist_md": re.sub(
            r"\A---\n.*?\n---\n", "", (paths.REPO_ROOT / ".claude/agents/resume-proofreader.md").read_text(), flags=re.S
        ),
    }
    if slug:
        ws = get_job(slug)
        ctx["jd_md"] = ws.jd.read_text() if ws.jd.exists() else None
        ctx["current_resume_tex"] = ws.tex.read_text() if ws.tex.exists() else None
    return ctx


@mcp.tool(
    description="Save a complete tailored resume.tex (and optional analysis markdown) for a job, compile it, "
    "and return the proofread report plus the rendered page image. Edit only the document body."
)
def write_resume(slug: str, resume_tex: str, analysis_md: str | None = None) -> t.CallToolResult:
    ws = get_job(slug)
    if not ws.dir.exists():
        return t.CallToolResult(content=[t.TextContent(type="text", text=f"No job '{slug}'; call start_job")], is_error=True)
    body = resume_tex.split("\\begin{document}", 1)[-1]
    pre = resume_tex.split("\\begin{document}", 1)[0].replace("\\input{glyphtounicode}", "")
    if _UNSAFE_TEX.search(pre) or _UNSAFE_TEX.search(body):
        return t.CallToolResult(
            content=[t.TextContent(type="text", text="Rejected: file I/O primitives (\\input, \\write18, ...) are not allowed")],
            is_error=True,
        )
    if "\\documentclass" not in resume_tex:
        return t.CallToolResult(content=[t.TextContent(type="text", text="resume_tex must be a complete document")], is_error=True)
    ws.tex.write_text(resume_tex)
    if analysis_md:
        ws.analysis.write_text(analysis_md)
    return _report_result(slug)


@mcp.tool(description="Recompile and proofread a job's current resume.tex; returns the report and rendered page.")
def proofread_resume(slug: str) -> t.CallToolResult:
    return _report_result(slug)


@mcp.tool(description="Return a job's compiled resume PDF as an embedded resource (application/pdf, base64).")
def get_resume_pdf(slug: str) -> t.CallToolResult:
    ws = get_job(slug)
    if not ws.pdf.exists():
        return t.CallToolResult(content=[t.TextContent(type="text", text="No PDF yet; call write_resume")], is_error=True)
    return t.CallToolResult(
        content=[t.TextContent(type="text", text=f"resume.pdf for {slug}"), _pdf_resource(slug)],
        structured_content={"slug": slug, "path": str(ws.pdf)},
    )


@mcp.tool(
    description="Independent verification of a job's current resume by a separate model call that sees only "
    "the fact bank, rules, JD, PDF text and rendered page (never your reasoning or analysis). Use this instead "
    "of reviewing your own draft. Requires ANTHROPIC_API_KEY on the server."
)
async def verify_resume(slug: str) -> t.CallToolResult:
    from .llm_pipeline import PipelineError
    from .llm_pipeline import verify_resume as run_verify

    ws = get_job(slug)
    if not ws.tex.exists():
        return t.CallToolResult(content=[t.TextContent(type="text", text=f"No job '{slug}'")], is_error=True)
    try:
        v = await anyio.to_thread.run_sync(lambda: run_verify(ws))
    except PipelineError as exc:
        return t.CallToolResult(content=[t.TextContent(type="text", text=str(exc))], is_error=True)
    return t.CallToolResult(
        content=[t.TextContent(type="text", text=v.review)],
        structured_content={"slug": slug, "verdict": v.verdict, "must_fix": v.must_fix},
    )


@mcp.tool(structured_output=True, description="List job workspaces on this server.")
def list_jobs() -> list[dict[str, Any]]:
    if not paths.JOBS_DIR.exists():
        return []
    return [
        {"slug": d.name, "has_pdf": (d / "resume.pdf").exists()}
        for d in sorted(paths.JOBS_DIR.iterdir())
        if d.is_dir()
    ]


@mcp.tool(
    description="Full pipeline on the server: fetch JD, tailor, compile, proofread, independent review, and "
    "return the PDF. Requires ANTHROPIC_API_KEY on the server. Takes 1–4 minutes."
)
async def generate_resume(
    jd_url: str | None = None,
    jd_text: str | None = None,
    slug: str | None = None,
    instructions: str = "",
) -> t.CallToolResult:
    from .llm_pipeline import PipelineError
    from .llm_pipeline import generate_resume as run

    try:
        res = await anyio.to_thread.run_sync(
            lambda: run(jd_url=jd_url, jd_text=jd_text, slug=slug, instructions=instructions)
        )
    except (PipelineError, FetchError) as exc:
        return t.CallToolResult(content=[t.TextContent(type="text", text=str(exc))], is_error=True)
    slug_out = res.workspace["slug"]
    summary = (
        f"Verdict: {res.verdict} after {res.rounds} round(s)\n"
        + ("Remaining issues:\n- " + "\n- ".join(res.remaining_issues) + "\n" if res.remaining_issues else "")
        + "\n".join(res.log)
    )
    content: list = [t.TextContent(type="text", text=summary)]
    if get_job(slug_out).pdf.exists():
        content.append(_pdf_resource(slug_out))
    return t.CallToolResult(content=content, structured_content=res.to_dict())


class QuestionAnswer(BaseModel):
    question: str = Field(description="The application form question, verbatim")
    answer: str = Field(default="", description="The exact answer submitted")


class Attachment(BaseModel):
    filename: str = Field(description="e.g. portfolio.pdf")
    content_base64: str
    label: str = ""


def _app_result(res: dict) -> t.CallToolResult:
    rec = res["record"]
    text = (
        f"{'Recorded' if res['created'] else 'Already recorded'} application {res['id']}\n"
        f"{rec['job'].get('company', '')} · {rec['job'].get('role', '')} · status {rec['status']}"
        + ("\nWarnings:\n- " + "\n- ".join(res["warnings"]) if res["warnings"] else "")
    )
    return t.CallToolResult(
        content=[t.TextContent(type="text", text=text)],
        structured_content={k: v for k, v in res.items() if k != "path"},
    )


def _app_error(exc: Exception) -> t.CallToolResult:
    return t.CallToolResult(content=[t.TextContent(type="text", text=str(exc))], is_error=True)


@mcp.tool(
    description="Record that an application was ACTUALLY SUBMITTED. Call this only after submitting. Stores the "
    "job link, submission time, the exact resume PDF used (from slug, or pass resume_pdf_base64 if you "
    "uploaded a different file), cover letter, every additional application question with the answer given, "
    "confirmation id and notes. A job-posting snapshot is saved, since postings disappear. Pass "
    "idempotency_key to make retries safe. Returns the application id for later update_application calls."
)
async def record_application(
    job_url: str,
    slug: str | None = None,
    resume_pdf_base64: str | None = None,
    submitted_at: str | None = Field(default=None, description="ISO 8601; defaults to now"),
    company: str | None = None,
    role: str | None = None,
    location: str | None = None,
    platform: str | None = Field(default=None, description="ATS / site; detected from the URL if omitted"),
    confirmation_id: str | None = None,
    submitted_by: str | None = Field(default=None, description="Name of the submitting agent or person"),
    cover_letter_text: str | None = None,
    cover_letter_base64: str | None = None,
    cover_letter_filename: str | None = None,
    questions: list[QuestionAnswer] | None = None,
    attachments: list[Attachment] | None = None,
    jd_text: str | None = None,
    notes: str | None = None,
    extra: dict[str, Any] | None = Field(default=None, description="Anything else, e.g. salary expectation, referral"),
    idempotency_key: str | None = None,
) -> t.CallToolResult:
    kwargs = dict(
        job_url=job_url, slug=slug, resume_pdf_base64=resume_pdf_base64, submitted_at=submitted_at,
        company=company, role=role, location=location, platform=platform, confirmation_id=confirmation_id,
        submitted_by=submitted_by, cover_letter_text=cover_letter_text, cover_letter_base64=cover_letter_base64,
        cover_letter_filename=cover_letter_filename,
        questions=[q.model_dump() for q in questions or []],
        attachments=[a.model_dump() for a in attachments or []],
        jd_text=jd_text, notes=notes, extra=extra, idempotency_key=idempotency_key,
    )
    try:
        res = await anyio.to_thread.run_sync(lambda: applications.record_application(**kwargs))
    except applications.ApplicationError as exc:
        return _app_error(exc)
    return _app_result(res)


@mcp.tool(
    description="Update a recorded application: change status (one of "
    + ", ".join(applications.STATUSES)
    + "), add a timeline note, add more question/answer pairs, set the confirmation id, or merge extra fields."
)
def update_application(
    application_id: str,
    status: str | None = None,
    note: str | None = None,
    at: str | None = None,
    by: str | None = None,
    add_questions: list[QuestionAnswer] | None = None,
    confirmation_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> t.CallToolResult:
    try:
        rec = applications.update_application(
            application_id, status=status, note=note, at=at, by=by,
            add_questions=[q.model_dump() for q in add_questions or []],
            confirmation_id=confirmation_id, extra=extra,
        )
    except applications.ApplicationError as exc:
        return _app_error(exc)
    return t.CallToolResult(
        content=[t.TextContent(type="text", text=f"{application_id}: status {rec['status']}, {len(rec['events'])} events")],
        structured_content=applications.summary(rec),
    )


@mcp.tool(structured_output=True, description="List recorded applications, newest first. Filter by status, company substring, or since (ISO date).")
def list_applications(status: str | None = None, company: str | None = None, since: str | None = None) -> list[dict[str, Any]]:
    return applications.list_applications(status=status, company=company, since=since)


@mcp.tool(description="Full record of one application: job, resume hash + verification, cover letter, Q&A, timeline, files.")
def get_application(application_id: str) -> t.CallToolResult:
    try:
        rec = applications.get_application(application_id)
    except applications.ApplicationError as exc:
        return _app_error(exc)
    rec.pop("path", None)
    return t.CallToolResult(content=[t.TextContent(type="text", text=json.dumps(rec, indent=2))], structured_content=rec)


@mcp.prompt(description="Step-by-step loop for an agent tailoring the resume itself with this server's tools.")
def tailor_resume(job_url: str) -> str:
    return f"""Tailor the resume for {job_url}.
1. start_job(jd_url="{job_url}"). If it fails, fetch the page yourself and call start_job(jd_text=...).
2. get_resume_context(slug). Follow rules_md and profile_rules_md exactly; work_history_md is the only source of facts.
3. Map the JD's 5–8 core requirements to evidence, then edit the BODY of base_template_tex.
4. write_resume(slug, resume_tex, analysis_md). Fix every ERROR/WARN in the report by editing content;
   look at the returned page image and fix visual problems. Repeat.
5. Do not sign off on your own draft. Call verify_resume(slug): an independent verifier with no access
   to your reasoning. Fix its MUST FIX items with write_resume and call verify_resume again (max 3 rounds).
   If the server has no API key, hand the PDF, reviewer_checklist_md, profile_rules_md and work_history_md
   to a separate agent that has not seen this conversation.
6. get_resume_pdf(slug) and use that exact PDF for the application.
7. After the application is actually submitted, call record_application(job_url="{job_url}", slug=..., questions=[...],
   cover_letter_text=...) with every additional question and the answer you gave."""
