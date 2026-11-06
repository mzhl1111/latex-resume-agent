"""Submitted-application tracker.

When an agent (via MCP or A2A) or the user (via the CLI or Claude Code) actually
submits an application, it records it here. Each application gets its own folder
under ``applications/`` (gitignored: personal data):

    applications/
      index.md                     human-readable table, newest first
      index.csv                    same, for spreadsheets
      2026-11-06_acme_ml-engineer_3f9a2c/
        application.json           the record (schema below) incl. status timeline
        resume.pdf                 the exact PDF that was submitted (sha256 in the record)
        resume.tex                 source, if the resume came from a job workspace
        jd.md                      job description snapshot (postings disappear)
        cover_letter.md|.pdf|...   cover letter, if any
        answers.md                 application questions and the answers given
        attachments/               any other files submitted

``application.json`` is the source of truth; the index files are regenerated
from the folders on every write, so they can be deleted safely.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import re
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import gate, paths, tracing
from .jd_fetch import slugify

SCHEMA_VERSION = 1
STATUSES = (
    "submitted",
    "acknowledged",
    "screen",
    "assessment",
    "interview",
    "offer",
    "accepted",
    "rejected",
    "withdrawn",
    "ghosted",
)
MAX_FILE_BYTES = 10 * 1024 * 1024
ALLOWED_ATTACHMENT_EXT = {".pdf", ".md", ".txt", ".docx", ".doc", ".rtf", ".html", ".png", ".jpg", ".jpeg"}
DUPLICATE_WINDOW_DAYS = 30

_lock = threading.RLock()


class ApplicationError(ValueError):
    """Bad input or unknown application."""


# --------------------------------------------------------------------------- helpers


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: str | None) -> str:
    if not value:
        return _now()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApplicationError(f"submitted_at must be ISO 8601, got {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.astimezone()  # assume the server's local time zone
    return dt.isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(data_b64: str, what: str) -> bytes:
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApplicationError(f"{what} is not valid base64") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise ApplicationError(f"{what} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    return raw


def _safe_name(name: str, default: str) -> str:
    base = Path(name or default).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or default
    if Path(base).suffix.lower() not in ALLOWED_ATTACHMENT_EXT:
        raise ApplicationError(f"File type not allowed: {base} (allowed: {sorted(ALLOWED_ATTACHMENT_EXT)})")
    return base[:100]


def _atomic_write(path: Path, data: bytes | str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data.encode() if isinstance(data, str) else data)
    tmp.replace(path)


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for key, name in (
        ("greenhouse.io", "greenhouse"),
        ("lever.co", "lever"),
        ("ashbyhq.com", "ashby"),
        ("myworkdayjobs.com", "workday"),
        ("linkedin.com", "linkedin"),
        ("smartrecruiters.com", "smartrecruiters"),
        ("icims.com", "icims"),
        ("indeed.com", "indeed"),
    ):
        if key in host:
            return name
    return host.removeprefix("www.") or "unknown"


def _jd_meta(jd_md: str) -> dict[str, str]:
    """Pull title/company/location out of a jd.md written by JobDescription.to_markdown()."""
    meta: dict[str, str] = {}
    m = re.search(r"^#\s+(.+)$", jd_md, re.M)
    if m and m.group(1).strip().lower() != "job description":
        meta["role"] = m.group(1).strip()
    for key, field in (("Company", "company"), ("Location", "location")):
        m = re.search(rf"^- \*\*{key}:\*\*\s*(.+)$", jd_md, re.M)
        if m:
            meta[field] = m.group(1).strip()
    return meta


def _answers_md(rec: dict) -> str:
    lines = [f"# Application questions — {rec['job'].get('company', '')} · {rec['job'].get('role', '')}", ""]
    if not rec["questions"]:
        lines.append("_No additional questions recorded._")
    for i, qa in enumerate(rec["questions"], 1):
        lines += [f"## {i}. {qa['question'].strip()}", "", qa.get("answer", "").strip() or "_(blank)_", ""]
    return "\n".join(lines).rstrip() + "\n"


def _apps_dir(apps_dir: Path | None) -> Path:
    return apps_dir or paths.APPLICATIONS_DIR


def _load(folder: Path) -> dict:
    return json.loads((folder / "application.json").read_text())


def _save(folder: Path, rec: dict) -> None:
    _atomic_write(folder / "application.json", json.dumps(rec, indent=2, ensure_ascii=False) + "\n")


def _folder(app_id: str, apps_dir: Path | None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", app_id or ""):
        raise ApplicationError(f"Invalid application id: {app_id!r}")
    folder = _apps_dir(apps_dir) / app_id
    if not (folder / "application.json").exists():
        raise ApplicationError(f"No application {app_id!r}")
    return folder


def _all(apps_dir: Path | None) -> list[dict]:
    root = _apps_dir(apps_dir)
    if not root.exists():
        return []
    recs = []
    for f in root.iterdir():
        if (f / "application.json").exists():
            try:
                recs.append(_load(f))
            except json.JSONDecodeError:
                continue
    return sorted(recs, key=lambda r: r.get("submitted_at", ""), reverse=True)


def summary(rec: dict) -> dict:
    return {
        "id": rec["id"],
        "submitted_at": rec["submitted_at"],
        "status": rec["status"],
        "company": rec["job"].get("company", ""),
        "role": rec["job"].get("role", ""),
        "platform": rec["job"].get("platform", ""),
        "url": rec["job"].get("url", ""),
        "resume_verified": (rec["resume"].get("verification") or {}).get("deliverable"),
        "cover_letter": bool(rec.get("cover_letter")),
        "questions": len(rec.get("questions", [])),
        "last_update": rec["events"][-1]["at"] if rec.get("events") else rec["submitted_at"],
    }


def rebuild_index(apps_dir: Path | None = None) -> None:
    root = _apps_dir(apps_dir)
    root.mkdir(parents=True, exist_ok=True)
    rows = [summary(r) for r in _all(apps_dir)]
    buf = io.StringIO()
    cols = ["id", "submitted_at", "status", "company", "role", "platform", "url", "resume_verified",
            "cover_letter", "questions", "last_update"]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    _atomic_write(root / "index.csv", buf.getvalue())

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    md = ["# Applications", "", f"{len(rows)} total — " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())), ""]
    md += ["| Submitted | Company | Role | Status | Platform | Verified | Folder |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        verified = {True: "yes", False: "**no**", None: "?"}[r["resume_verified"]]
        md.append(
            f"| {r['submitted_at'][:10]} | {r['company']} | [{r['role'] or 'posting'}]({r['url']}) | {r['status']} "
            f"| {r['platform']} | {verified} | [{r['id']}]({r['id']}/) |"
        )
    _atomic_write(root / "index.md", "\n".join(md) + "\n")


# --------------------------------------------------------------------------- API


def record_application(
    *,
    job_url: str,
    slug: str | None = None,
    resume_pdf_base64: str | None = None,
    company: str | None = None,
    role: str | None = None,
    location: str | None = None,
    submitted_at: str | None = None,
    platform: str | None = None,
    confirmation_id: str | None = None,
    submitted_by: str | None = None,
    cover_letter_text: str | None = None,
    cover_letter_base64: str | None = None,
    cover_letter_filename: str | None = None,
    questions: list[dict] | None = None,
    attachments: list[dict] | None = None,
    jd_text: str | None = None,
    notes: str | None = None,
    extra: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    fetch_jd_snapshot: bool = True,
    apps_dir: Path | None = None,
    jobs_dir: Path | None = None,
) -> dict:
    """Record a submitted application. Returns {"id", "path", "created", "warnings", "record"}."""
    with tracing.span("application.record", job=slug or "", platform=detect_platform(job_url or ""),
                      submitted_by=submitted_by or "") as s:
        res = _record(**{k: v for k, v in locals().items() if k != "s"})
        rec = res["record"]
        s.set(
            created=res["created"], application_id=res["id"], company=rec["job"].get("company", ""),
            role=rec["job"].get("role", ""), questions=len(rec.get("questions", [])),
            cover_letter=bool(rec.get("cover_letter")),
            verified=(rec["resume"].get("verification") or {}).get("deliverable"),
            matches_generated=rec["resume"].get("matches_generated"), warnings=len(res["warnings"]),
        )
        return res


def _record(
    *,
    job_url, slug, resume_pdf_base64, company, role, location, submitted_at, platform, confirmation_id,
    submitted_by, cover_letter_text, cover_letter_base64, cover_letter_filename, questions, attachments,
    jd_text, notes, extra, idempotency_key, fetch_jd_snapshot, apps_dir, jobs_dir,
) -> dict:
    if not re.match(r"^https?://", job_url or ""):
        raise ApplicationError("job_url must be an http(s) URL")
    if not slug and not resume_pdf_base64:
        raise ApplicationError("Provide slug (a generated job) or resume_pdf_base64 (the file you submitted)")
    root = _apps_dir(apps_dir)
    warnings: list[str] = []

    with _lock:
        existing = _all(apps_dir)
        if idempotency_key:
            for r in existing:
                if r.get("idempotency_key") == idempotency_key:
                    return {"id": r["id"], "path": str(root / r["id"]), "created": False,
                            "warnings": ["idempotency_key matched an existing record; nothing changed"], "record": r}

        # Resume: the exact bytes submitted, compared against the generated one.
        ws_dir = None
        if slug:
            from .workspace import get_job

            ws = get_job(slug, jobs_dir)
            ws_dir = ws.dir
            if not ws.dir.exists():
                raise ApplicationError(f"No job workspace {slug!r}")
        generated = ws_dir / "resume.pdf" if ws_dir else None
        if resume_pdf_base64:
            pdf = _b64(resume_pdf_base64, "resume_pdf_base64")
            if not pdf.startswith(b"%PDF"):
                raise ApplicationError("resume_pdf_base64 does not look like a PDF")
        elif generated and generated.exists():
            pdf = generated.read_bytes()
        else:
            raise ApplicationError(f"Job {slug!r} has no resume.pdf; pass resume_pdf_base64")
        pdf_sha = _sha256(pdf)
        matches = _sha256(generated.read_bytes()) == pdf_sha if generated and generated.exists() else None
        if matches is False:
            warnings.append("Submitted PDF differs from the generated resume.pdf for this job")

        verification = None
        if ws_dir is not None and (ws_dir / "resume.pdf").exists():
            st = gate.delivery_status(ws_dir)
            verification = {"deliverable": st.deliverable, "reason": st.reason, "rounds": st.rounds,
                            "verdict": st.latest.verdict if st.latest else None}
            if not st.deliverable:
                warnings.append(f"Resume was not verifier-approved at submission: {st.reason}")

        # Job metadata: explicit args > job workspace jd.md > fetched snapshot.
        jd_md = jd_text
        if not jd_md and ws_dir is not None and (ws_dir / "jd.md").exists():
            jd_md = (ws_dir / "jd.md").read_text()
        if not jd_md and fetch_jd_snapshot:
            try:
                from .jd_fetch import fetch_jd

                jd_md = fetch_jd(job_url).to_markdown()
            except Exception as exc:  # snapshot is best effort
                warnings.append(f"Could not snapshot the job posting: {str(exc)[:160]}")
        meta = _jd_meta(jd_md) if jd_md else {}
        company = company or meta.get("company") or ""
        role = role or meta.get("role") or ""
        location = location or meta.get("location") or ""

        url_norm = job_url.split("#")[0].rstrip("/")
        for r in existing:
            if r["job"].get("url", "").split("#")[0].rstrip("/") == url_norm and r.get("status") != "withdrawn":
                warnings.append(f"Possible duplicate: already applied to this URL as {r['id']} on {r['submitted_at'][:10]}")

        when = _parse_time(submitted_at)
        app_id = f"{when[:10]}_{slugify(company or detect_platform(job_url), max_len=30)}_{slugify(role or 'role', max_len=40)}_{secrets.token_hex(3)}"
        folder = root / app_id
        folder.mkdir(parents=True)
        try:
            _atomic_write(folder / "resume.pdf", pdf)
            if ws_dir is not None and (ws_dir / "resume.tex").exists():
                shutil.copy(ws_dir / "resume.tex", folder / "resume.tex")
            if jd_md:
                _atomic_write(folder / "jd.md", jd_md)

            cover = None
            if cover_letter_base64:
                raw = _b64(cover_letter_base64, "cover_letter_base64")
                name = _safe_name(f"cover_letter{Path(cover_letter_filename or 'x.pdf').suffix}", "cover_letter.pdf")
                _atomic_write(folder / name, raw)
                cover = {"file": name, "sha256": _sha256(raw)}
            elif cover_letter_text:
                _atomic_write(folder / "cover_letter.md", cover_letter_text.strip() + "\n")
                cover = {"file": "cover_letter.md", "sha256": _sha256(cover_letter_text.encode())}

            files = []
            for i, att in enumerate(attachments or []):
                if not isinstance(att, dict) or not att.get("content_base64"):
                    raise ApplicationError("attachments must be objects with filename and content_base64")
                raw = _b64(att["content_base64"], f"attachments[{i}]")
                name = _safe_name(att.get("filename", ""), f"attachment_{i}.pdf")
                (folder / "attachments").mkdir(exist_ok=True)
                _atomic_write(folder / "attachments" / name, raw)
                files.append({"file": f"attachments/{name}", "sha256": _sha256(raw), "label": att.get("label", "")})

            qas = []
            for q in questions or []:
                if not isinstance(q, dict) or not str(q.get("question", "")).strip():
                    raise ApplicationError("questions must be objects with a non-empty 'question'")
                qas.append({"question": str(q["question"]).strip(), "answer": str(q.get("answer", "")).strip()})

            rec = {
                "schema": SCHEMA_VERSION,
                "id": app_id,
                "status": "submitted",
                "submitted_at": when,
                "recorded_at": _now(),
                "submitted_by": submitted_by or "",
                "confirmation_id": confirmation_id or "",
                "idempotency_key": idempotency_key or "",
                "job": {
                    "url": job_url,
                    "company": company,
                    "role": role,
                    "location": location,
                    "platform": platform or detect_platform(job_url),
                    "jd_file": "jd.md" if jd_md else None,
                    "source_slug": slug or "",
                },
                "resume": {
                    "file": "resume.pdf",
                    "sha256": pdf_sha,
                    "matches_generated": matches,
                    "verification": verification,
                },
                "cover_letter": cover,
                "questions": qas,
                "attachments": files,
                "notes": notes or "",
                "extra": extra or {},
                "events": [{"at": when, "status": "submitted", "note": notes or "", "by": submitted_by or ""}],
                "warnings": warnings,
            }
            _atomic_write(folder / "answers.md", _answers_md(rec))
            _save(folder, rec)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        rebuild_index(apps_dir)
    return {"id": app_id, "path": str(folder), "created": True, "warnings": warnings, "record": rec}


def update_application(
    app_id: str,
    *,
    status: str | None = None,
    note: str | None = None,
    at: str | None = None,
    by: str | None = None,
    add_questions: list[dict] | None = None,
    confirmation_id: str | None = None,
    extra: dict[str, Any] | None = None,
    apps_dir: Path | None = None,
) -> dict:
    """Append a timeline event (status change and/or note) and merge extra fields."""
    with tracing.span("application.update", application_id=app_id, status=status or "") as s:
        rec = _update(app_id, status=status, note=note, at=at, by=by, add_questions=add_questions,
                      confirmation_id=confirmation_id, extra=extra, apps_dir=apps_dir)
        s.set(company=rec["job"].get("company", ""), status=rec["status"])
        return rec


def _update(app_id, *, status, note, at, by, add_questions, confirmation_id, extra, apps_dir) -> dict:
    if status and status not in STATUSES:
        raise ApplicationError(f"status must be one of {', '.join(STATUSES)}")
    if not (status or note or add_questions or confirmation_id or extra):
        raise ApplicationError("Nothing to update")
    with _lock:
        folder = _folder(app_id, apps_dir)
        rec = _load(folder)
        if status or note:
            rec["events"].append({"at": _parse_time(at), "status": status or rec["status"], "note": note or "", "by": by or ""})
            if status:
                rec["status"] = status
        for q in add_questions or []:
            if not str(q.get("question", "")).strip():
                raise ApplicationError("questions must have a non-empty 'question'")
            rec["questions"].append({"question": str(q["question"]).strip(), "answer": str(q.get("answer", "")).strip()})
        if add_questions:
            _atomic_write(folder / "answers.md", _answers_md(rec))
        if confirmation_id:
            rec["confirmation_id"] = confirmation_id
        if extra:
            rec["extra"].update(extra)
        _save(folder, rec)
        rebuild_index(apps_dir)
    return rec


def get_application(app_id: str, *, apps_dir: Path | None = None) -> dict:
    folder = _folder(app_id, apps_dir)
    rec = _load(folder)
    rec["files"] = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
    rec["path"] = str(folder)
    return rec


def list_applications(
    *,
    status: str | None = None,
    company: str | None = None,
    since: str | None = None,
    apps_dir: Path | None = None,
) -> list[dict]:
    rows = [summary(r) for r in _all(apps_dir)]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if company:
        rows = [r for r in rows if company.lower() in r["company"].lower()]
    if since:
        cutoff = datetime.fromisoformat(_parse_time(since))
        rows = [r for r in rows if datetime.fromisoformat(r["submitted_at"]) >= cutoff]
    return rows


READABLE = {".pdf": "application/pdf", ".md": "text/markdown", ".txt": "text/plain", ".docx":
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".doc": "application/msword",
            ".rtf": "application/rtf", ".html": "text/html", ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg"}


def list_files(app_id: str, *, apps_dir: Path | None = None) -> list[dict]:
    folder = _folder(app_id, apps_dir)
    return [
        {"name": str(p.relative_to(folder)), "bytes": p.stat().st_size, "mime": READABLE[p.suffix.lower()]}
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.suffix.lower() in READABLE
    ]


def read_file(app_id: str, name: str, *, apps_dir: Path | None = None) -> tuple[bytes, str]:
    """Bytes + MIME type of one file in an application folder (e.g. resume.pdf, cover_letter.md,
    jd.md, answers.md, attachments/portfolio.pdf). Refuses anything outside the folder."""
    folder = _folder(app_id, apps_dir).resolve()
    if not re.fullmatch(r"(attachments/)?[A-Za-z0-9][A-Za-z0-9._-]*", name or ""):
        raise ApplicationError(f"Invalid file name: {name!r}")
    target = (folder / name).resolve()
    mime = READABLE.get(target.suffix.lower())
    if not mime or folder not in target.parents or not target.is_file():
        raise ApplicationError(f"No file {name!r} in {app_id}")
    return target.read_bytes(), mime
