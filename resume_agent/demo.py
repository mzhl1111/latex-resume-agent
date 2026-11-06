"""Fictional demo data for the dashboard (README screenshot, trying the UI).

    python3 -m resume_agent demo-data --out demo
    python3 -m resume_agent dashboard --traces demo/traces --applications demo/applications

Everything is made up: companies are fictional, people don't exist, numbers are
simulated from plausible distributions. Output goes to its own folder so it never
mixes with real traces/ or applications/.
"""

from __future__ import annotations

import json
import random
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from . import applications as apps

COMPANIES = [
    "Northwind Traders", "Contoso", "Fabrikam", "Tailspin Toys", "Wingtip Labs", "Adventure Works", "Litware",
    "Proseware", "Woodgrove Bank", "Fourth Coffee", "Alpine Ski House", "Blue Yonder Airlines", "Coho Vineyard",
    "Lamna Health", "Relecloud", "Trey Research", "Wide World Importers", "Margie's Travel", "Humongous Insurance",
    "VanArsdel", "Lucerne Publishing", "Graphic Design Institute", "Southridge Video", "City Power & Light",
]
ROLES = [
    ("Senior ML Engineer, Search", "search"), ("Machine Learning Engineer, Ranking", "search"),
    ("ML Platform Engineer", "platform"), ("Staff Engineer, ML Infrastructure", "platform"),
    ("Applied Scientist, Retrieval", "search"), ("AI Engineer, LLM Applications", "genai"),
    ("Software Engineer, GenAI Platform", "genai"), ("MLOps Engineer", "platform"),
    ("Senior Software Engineer, Recommendations", "applied"), ("Machine Learning Engineer, Forecasting", "applied"),
]
SOURCES = [("greenhouse", 0.34), ("lever", 0.2), ("ashby", 0.16), ("workday", 0.12), ("json-ld", 0.12), ("linkedin", 0.06)]
CHECKS = [("orphan-line", 0.34), ("unverified-number", 0.14), ("page-count", 0.18), ("whitespace", 0.12),
          ("overfull-box", 0.09), ("guardrail:sole-ownership", 0.07), ("repeated-word", 0.05), ("layout-drift", 0.04)]
QUESTIONS = [
    "Why are you interested in {company}?",
    "How many years of production machine learning experience do you have?",
    "Are you legally authorized to work in the country where this role is located?",
    "Will you now or in the future require visa sponsorship?",
    "What are your salary expectations?",
    "Describe a system you built that you are proud of.",
    "How did you hear about this role?",
]
VERIFIER_DENIALS = [
    ("Read", "The verifier may not read the writer's analysis, earlier reviews or harness state."),
    ("Grep", "Searches may not cover the project root or job folders (they contain writer notes)."),
    ("Bash", "Verifier Bash is limited to: python3 -m resume_agent proofread <tex> | pdftotext [-layout] <pdf> - | pdfinfo <pdf> | ls <dir>."),
]
ORCH_DENIALS = [
    ("Edit", "Only the resume-writer subagent may edit jobs/{job}/resume.tex. Launch it with the Task tool."),
    ("Write", "Review records are written only by the harness (SubagentStop hook)."),
    ("Bash", "Deliver with `python3 -m resume_agent deliver <job> --name <file>.pdf`; it checks the review gate."),
]


def _pick(rng: random.Random, weighted):
    r, acc = rng.random(), 0.0
    for item, w in weighted:
        acc += w
        if r <= acc:
            return item
    return weighted[-1][0]


class _Writer:
    def __init__(self, out: Path, rng: random.Random):
        self.out, self.rng, self.n = out, rng, 0
        self.files: dict[str, list[str]] = {}

    def span(self, name, start: datetime, ms: float, mode: str, attrs: dict, *, trace=None, parent=None,
             status="ok", error=None) -> dict:
        rec = {
            "trace_id": trace or secrets.token_hex(16), "span_id": secrets.token_hex(8), "parent_id": parent,
            "name": name, "start": start.isoformat(timespec="milliseconds"), "attrs": attrs, "events": [],
            "status": status, "duration_ms": round(ms, 2),
            "resource": {"service": "latex-resume-agent", "version": __version__, "host": "demo", "pid": 0, "mode": mode},
        }
        if error:
            rec["error"] = error
        self.files.setdefault(rec["start"][:10], []).append(json.dumps(rec))
        self.n += 1
        return rec

    def flush(self):
        self.out.mkdir(parents=True, exist_ok=True)
        for day, lines in self.files.items():
            (self.out / f"spans-{day}.jsonl").write_text("\n".join(lines) + "\n")


def _proofread(w: _Writer, rng, t, mode, job, trace=None, parent=None, clean=False):
    c = w.span("latex.compile", t, rng.uniform(650, 1400), mode, {"job": job, "ok": True, "pages": 1, "errors": 0, "overfull": 0},
               trace=trace, parent=parent)
    checks = [] if clean else sorted({c for c, p in CHECKS if rng.random() < p})
    errors = sum(1 for c in checks if c in ("unverified-number", "page-count") or c.startswith("guardrail"))
    w.span("proofread", t, c["duration_ms"] + rng.uniform(180, 420), mode,
           {"job": job, "ok": errors == 0, "errors": errors, "warnings": len(checks) - errors, "checks": checks,
            "fill_ratio": round(rng.uniform(0.88, 0.97), 3)}, trace=trace, parent=parent)
    return errors == 0 and not checks


def generate(out: Path, *, days: int = 60, jobs: int = 48, seed: int = 7, end: datetime | None = None) -> dict:
    rng = random.Random(seed)
    end = end or datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    traces, apps_dir = out / "traces", out / "applications"
    for d in (traces, apps_dir):
        if d.exists():
            shutil.rmtree(d)
    w = _Writer(traces, rng)
    n_apps = 0

    starts = sorted(end - timedelta(days=days * (rng.random() ** 0.85)) for _ in range(jobs))
    for i, t0 in enumerate(starts):
        t0 = t0.replace(hour=rng.randint(8, 21), minute=rng.randint(0, 59))
        if t0 > end:
            t0 = end - timedelta(hours=rng.randint(2, 30))
        company = rng.choice(COMPANIES)
        role, _ = rng.choice(ROLES)
        job = f"{company.lower().replace(' ', '-').replace('&', 'and').replace(chr(39), '')}-{role.split(',')[0].lower().replace(' ', '-')}"[:58] + f"-{i}"
        mode = _pick(rng, [("claude-code", 0.64), ("server", 0.31), ("cli", 0.05)])
        t = t0
        trace = secrets.token_hex(16) if mode == "server" else None
        source = _pick(rng, SOURCES)
        fetch_fail = rng.random() < 0.08
        w.span("jd.fetch", t, rng.uniform(380, 2400), mode,
               {"host": f"{source}.example", "source": source if not fetch_fail else None, "chars": rng.randint(2400, 7800),
                "company": company, "role": role},
               trace=trace, status="error" if fetch_fail else "ok",
               error="FetchError: page renders with JavaScript; fell back to WebFetch" if fetch_fail else None)
        t += timedelta(seconds=3 if not fetch_fail else 25)
        w.span("job.create", t, rng.uniform(6, 20), mode, {"job": job, "company": company, "role": role}, trace=trace)

        # rounds until PASS (or 3)
        p_pass = [0.58, 0.72, 0.6]
        verdicts = []
        for r in range(3):
            ok = rng.random() < p_pass[r]
            verdicts.append("PASS" if ok else "NEEDS CHANGES")
            if ok:
                break
        tokens = 0
        gen_start = t
        for r, verdict in enumerate(verdicts, 1):
            if mode == "server":
                ti, to = rng.randint(6200, 9400), rng.randint(2600, 4800)
                tokens += ti + to
                ms = rng.uniform(24000, 58000)
                w.span("llm.call", t, ms, mode, {"role": "writer", "model": "claude-sonnet-5-5", "job": job,
                                                 "input_tokens": ti, "output_tokens": to}, trace=trace)
                t += timedelta(milliseconds=ms)
                for k in range(rng.choice([1, 1, 2, 2, 3])):
                    _proofread(w, rng, t, mode, job, trace=trace, clean=(k > 0 and rng.random() < 0.6))
                    t += timedelta(seconds=2)
                vi, vo = rng.randint(9800, 14200), rng.randint(380, 900)
                tokens += vi + vo
                vms = rng.uniform(11000, 29000)
                w.span("llm.call", t, vms, mode, {"role": "verifier", "model": "claude-sonnet-5-5", "job": job,
                                                  "input_tokens": vi, "output_tokens": vo}, trace=trace)
                w.span("resume.verify", t, vms + 1800, mode, {"job": job, "model": "claude-sonnet-5-5", "source": "server",
                                                              "verdict": verdict, "must_fix": 0 if verdict == "PASS" else rng.randint(1, 3)}, trace=trace)
                t += timedelta(milliseconds=vms + 1800)
                w.span("review.record", t, 2, mode, {"job": job, "verdict": verdict, "round": r, "source": "server",
                                                     "must_fix": 0 if verdict == "PASS" else rng.randint(1, 3)}, trace=trace)
            else:
                secs_w = rng.uniform(80, 320) if r == 1 else rng.uniform(40, 140)
                for k in range(rng.choice([1, 2, 2, 3])):
                    _proofread(w, rng, t + timedelta(seconds=secs_w * (k + 1) / 4), mode, job, clean=(k > 0 and rng.random() < 0.6))
                t += timedelta(seconds=secs_w)
                w.span("agent.writer", t, 3, "claude-code", {"job": job, "agent_seconds": round(secs_w, 1), "bounced": False})
                w.span("harness.decision", t, 1, "claude-code", {"role": "orchestrator", "tool": "Task", "decision": "rewrite",
                                                                 "reason": "verifier gets only the job folder", "job": job})
                if rng.random() < 0.16:
                    tool, reason = rng.choice(VERIFIER_DENIALS)
                    w.span("harness.decision", t + timedelta(seconds=8), 1, "claude-code",
                           {"role": "resume-proofreader", "tool": tool, "decision": "deny", "reason": reason, "job": job})
                if rng.random() < 0.06:
                    tool, reason = rng.choice(ORCH_DENIALS)
                    w.span("harness.decision", t + timedelta(seconds=2), 1, "claude-code",
                           {"role": "orchestrator", "tool": tool, "decision": "deny", "reason": reason.format(job=job), "job": job})
                secs_v = rng.uniform(45, 150)
                _proofread(w, rng, t + timedelta(seconds=10), mode, job, clean=verdict == "PASS")
                t += timedelta(seconds=secs_v)
                w.span("review.record", t, 2, "claude-code", {"job": job, "verdict": verdict, "round": r, "source": "claude-code",
                                                              "must_fix": 0 if verdict == "PASS" else rng.randint(1, 3)})
                w.span("agent.verifier", t, 3, "claude-code", {"job": job, "agent_seconds": round(secs_v, 1),
                                                               "review": f"review-{r}.md", "bounced": rng.random() < 0.04})
        final = verdicts[-1]
        deliverable = final == "PASS" or rng.random() < 0.5
        if mode == "server":
            w.span("resume.generate", gen_start, (t - gen_start).total_seconds() * 1000, mode,
                   {"job": job, "ok": final == "PASS", "verdict": final, "rounds": len(verdicts), "remaining": 0 if final == "PASS" else 2,
                    "writer_model": "claude-sonnet-5-5"}, trace=trace)
            w.span("mcp.tool", gen_start - timedelta(milliseconds=40), (t - gen_start).total_seconds() * 1000 + 120, mode,
                   {"tool": "generate_resume", "is_error": False}, trace=trace)
        t += timedelta(seconds=20)
        w.span("resume.deliver", t, 4, mode,
               {"job": job, "deliverable": deliverable, "rounds": len(verdicts), "verdict": final,
                "reason": f"review-{len(verdicts)}.md: {final}", "accepted_open_issues": deliverable and final != "PASS"})
        if not deliverable or rng.random() > 0.8:
            continue

        # application submitted (by another agent via MCP/A2A, or by the user via Claude Code)
        submitted = t + timedelta(hours=rng.uniform(1, 40))
        if submitted > end:
            continue
        by = "browser-agent (MCP)" if mode == "server" or rng.random() < 0.35 else "claude-code"
        qs = rng.sample(QUESTIONS, rng.randint(1, 4))
        n_apps += 1
        app_id = f"{submitted.date().isoformat()}_{job[:40]}_{secrets.token_hex(3)}"
        age = (end - submitted).days
        events = [{"at": submitted.isoformat(timespec="seconds"), "status": "submitted", "note": "", "by": by}]
        status = "submitted"
        clock = submitted

        def step(new, lo, hi):
            nonlocal status, clock
            nxt = clock + timedelta(days=rng.uniform(lo, hi))
            if nxt > end:
                return False
            clock, status = nxt, new
            events.append({"at": nxt.isoformat(timespec="seconds"), "status": new, "note": "", "by": "user"})
            return True

        if age > 1 and rng.random() < 0.66 and step("acknowledged", 0.2, 2):
            r = rng.random()
            if r < 0.38:
                step("rejected", 2, 10)
            elif r < 0.82 and step("screen", 1.5, 6):
                if rng.random() < 0.62 and step("interview", 2, 7):
                    r2 = rng.random()
                    if r2 < 0.3:
                        step("offer", 4, 9)
                    elif r2 < 0.75:
                        step("rejected", 2, 8)
        elif age > 21 and rng.random() < 0.5:
            step("ghosted", 21, 24)

        rec = {
            "schema": apps.SCHEMA_VERSION, "id": app_id, "status": status, "submitted_at": submitted.isoformat(timespec="seconds"),
            "recorded_at": submitted.isoformat(timespec="seconds"), "submitted_by": by, "confirmation_id": f"DEMO-{rng.randint(10000, 99999)}",
            "idempotency_key": "",
            "job": {"url": f"https://jobs.example.com/{job}", "company": company, "role": role, "location": rng.choice(["Remote", "Seattle, WA", "Toronto, ON", "New York, NY"]),
                    "platform": source if source != "json-ld" else "careers site", "jd_file": "jd.md", "source_slug": job},
            "resume": {"file": "resume.pdf", "sha256": secrets.token_hex(32), "matches_generated": True,
                       "verification": {"deliverable": final == "PASS", "reason": f"review-{len(verdicts)}.md: {final}",
                                        "rounds": len(verdicts), "verdict": final}},
            "cover_letter": {"file": "cover_letter.md", "sha256": secrets.token_hex(32)} if rng.random() < 0.4 else None,
            "questions": [{"question": q.format(company=company), "answer": "(demo answer)"} for q in qs],
            "attachments": [], "notes": "Fictional demo record", "extra": {}, "events": events, "warnings": [],
        }
        folder = apps_dir / app_id
        folder.mkdir(parents=True)
        (folder / "application.json").write_text(json.dumps(rec, indent=2))
        (folder / "answers.md").write_text(apps._answers_md(rec))
        w.span("application.record", submitted, rng.uniform(40, 900), "server" if "MCP" in by else "claude-code",
               {"job": job, "application_id": app_id, "company": company, "role": role, "platform": rec["job"]["platform"],
                "created": True, "verified": final == "PASS", "questions": len(qs)})
        if "MCP" in by:
            w.span("mcp.tool", submitted - timedelta(milliseconds=30), 950, "server", {"tool": "record_application", "is_error": False, "job": job})
        for e in events[1:]:
            w.span("application.update", datetime.fromisoformat(e["at"]), rng.uniform(15, 60), "cli",
                   {"application_id": app_id, "status": e["status"], "company": company})

    w.flush()
    apps.rebuild_index(apps_dir)
    return {"traces": str(traces), "applications": str(apps_dir), "spans": w.n, "applications_count": n_apps, "jobs": jobs}
