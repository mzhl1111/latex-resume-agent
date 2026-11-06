"""CLI used by the Claude Code workflow (and handy by hand).

    python -m resume_agent init                        copy fictional examples into profile/ + templates/
    python -m resume_agent start <url> [--slug S]     fetch JD + create jobs/<slug>/
    python -m resume_agent start --text-file jd.txt --slug S
    python -m resume_agent fetch-jd <url>              print JD markdown only
    python -m resume_agent check <tex>                 compile + 1-page check + PNG
    python -m resume_agent proofread <tex> [--json]    compile + mechanical proofread
    python -m resume_agent status <job-dir>            review rounds + delivery gate
    python -m resume_agent deliver <job-dir> --name F  copy PDF to outputs/ if the verifier passed it
    python -m resume_agent app record --url U (--slug S | --pdf F) [--qa F] [--cover-letter F] ...
    python -m resume_agent app update <id> [--status S] [--note N]
    python -m resume_agent app list [--status S] [--company C] [--since DATE]
    python -m resume_agent app show <id>
    python -m resume_agent dashboard [--port 8050] [--export F.html] [--traces D] [--applications D]
    python -m resume_agent demo-data [--out demo]      fictional traces + applications for the dashboard
    python -m resume_agent generate <url|--text-file F> [--writer-backend B] [--verifier-backend B]
    python -m resume_agent llm-check [--role writer|verifier]   which backend each role uses + a ping
    python -m resume_agent serve [--port 8080]         MCP + A2A HTTP server
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import applications as apps_mod
from . import gate, paths
from .compile import compile_resume
from .jd_fetch import FetchError, fetch_jd, slugify
from .proofread import proofread
from .workspace import new_job


def _start(args) -> int:
    if args.text_file:
        if not args.slug:
            print("--slug is required with --text-file", file=sys.stderr)
            return 2
        md = Path(args.text_file).read_text()
        ws = new_job(args.slug, md)
        print(json.dumps({"workspace": ws.describe(), "source": "text"}, indent=2))
        return 0
    try:
        jd = fetch_jd(args.url)
    except FetchError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    slug = args.slug or slugify(jd.company, jd.title)
    ws = new_job(slug, jd.to_markdown())
    print(
        json.dumps(
            {
                "workspace": ws.describe(),
                "title": jd.title,
                "company": jd.company,
                "location": jd.location,
                "source": jd.source,
                "chars": len(jd.text),
            },
            indent=2,
        )
    )
    return 0


def _read_qa(path: str | None) -> list[dict]:
    if not path:
        return []
    text = Path(path).read_text()
    if text.lstrip().startswith("["):
        return json.loads(text)
    qas, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^#{2,3}\s+(?:\d+\.\s*)?(.+?)\s*$", line)
        if m:
            cur = {"question": m.group(1), "answer": ""}
            qas.append(cur)
        elif cur is not None:
            cur["answer"] += line + "\n"
    for q in qas:
        q["answer"] = q["answer"].strip()
    return qas


def _app(args) -> int:
    import base64

    if args.app_cmd == "record":
        kw = json.loads(Path(args.from_json).read_text()) if args.from_json else {}
        for k in ("job_url", "slug", "company", "role", "location", "submitted_at", "platform",
                  "confirmation_id", "submitted_by", "notes"):
            if getattr(args, k, None):
                kw[k] = getattr(args, k)
        if args.pdf:
            kw["resume_pdf_base64"] = base64.b64encode(Path(args.pdf).read_bytes()).decode()
        if args.cover_letter:
            cl = Path(args.cover_letter)
            if cl.suffix.lower() in (".md", ".txt"):
                kw["cover_letter_text"] = cl.read_text()
            else:
                kw["cover_letter_base64"] = base64.b64encode(cl.read_bytes()).decode()
                kw["cover_letter_filename"] = cl.name
        if args.qa:
            kw["questions"] = kw.get("questions", []) + _read_qa(args.qa)
        if args.no_snapshot:
            kw["fetch_jd_snapshot"] = False
        if not kw.get("job_url"):
            raise apps_mod.ApplicationError("--url is required")
        res = apps_mod.record_application(**kw)
        print(f"{'Recorded' if res['created'] else 'Already recorded'}: {res['id']}\n  {res['path']}")
        for w in res["warnings"]:
            print(f"  warning: {w}")
        return 0
    if args.app_cmd == "update":
        rec = apps_mod.update_application(
            args.id, status=args.status, note=args.note, at=args.at, by="cli",
            add_questions=_read_qa(args.qa), confirmation_id=args.confirmation_id,
        )
        print(f"{rec['id']}: {rec['status']} ({len(rec['events'])} events)")
        return 0
    if args.app_cmd == "list":
        rows = apps_mod.list_applications(status=args.status, company=args.company, since=args.since)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                flag = "" if r["resume_verified"] is not False else "  [unverified resume]"
                print(f"{r['submitted_at'][:10]}  {r['status']:<12} {r['company'][:24]:<24} {r['role'][:40]:<40} {r['id']}{flag}")
            print(f"{len(rows)} application(s); index: {paths.APPLICATIONS_DIR / 'index.md'}")
        return 0
    if args.app_cmd == "file":
        if not args.name:
            for f in apps_mod.list_files(args.id):
                print(f"{f['name']:<40} {f['bytes']:>9}  {f['mime']}")
            return 0
        data, mime = apps_mod.read_file(args.id, args.name)
        if args.out:
            Path(args.out).write_bytes(data)
            print(f"Wrote {args.out} ({len(data)} bytes, {mime})")
        elif mime.startswith("text/"):
            sys.stdout.write(data.decode("utf-8", "replace"))
        else:
            raise apps_mod.ApplicationError(f"{args.name} is {mime}; pass --out <path>")
        return 0
    rec = apps_mod.get_application(args.id)
    print(json.dumps(rec, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="resume_agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="fetch a JD and create a job workspace")
    s.add_argument("url", nargs="?")
    s.add_argument("--slug")
    s.add_argument("--text-file", help="use pasted JD text instead of fetching")

    f = sub.add_parser("fetch-jd")
    f.add_argument("url")

    c = sub.add_parser("check")
    c.add_argument("tex")

    r = sub.add_parser("proofread")
    r.add_argument("tex")
    r.add_argument("--json", action="store_true")

    sub.add_parser("init", help="create profile/ and templates/ from examples/ (never overwrites)")

    st = sub.add_parser("status", help="show review rounds and whether the job can be delivered")
    st.add_argument("job")

    dl = sub.add_parser("deliver", help="copy resume.pdf to outputs/ (gated on an independent PASS)")
    dl.add_argument("job")
    dl.add_argument("--name", required=True, help="output file name, e.g. Jane_Doe_Acme_MLE.pdf")
    dl.add_argument("--accept-open-issues", action="store_true",
                    help=f"deliver after {gate.MAX_ROUNDS} rounds even if the last verdict is not PASS")

    ap = sub.add_parser("app", help="record and track submitted applications")
    asub = ap.add_subparsers(dest="app_cmd", required=True)
    rec = asub.add_parser("record", help="record a submitted application")
    rec.add_argument("--from-json", help="JSON file with any record_application fields (CLI flags override)")
    rec.add_argument("--url", dest="job_url")
    rec.add_argument("--slug", help="job workspace whose resume.pdf was submitted")
    rec.add_argument("--pdf", help="the exact PDF submitted, if not the job's resume.pdf")
    rec.add_argument("--company")
    rec.add_argument("--role")
    rec.add_argument("--location")
    rec.add_argument("--submitted-at", help="ISO 8601, default now")
    rec.add_argument("--platform")
    rec.add_argument("--confirmation-id")
    rec.add_argument("--by", dest="submitted_by", default="cli")
    rec.add_argument("--cover-letter", help=".md/.txt (stored as text) or .pdf/.docx (stored as file)")
    rec.add_argument("--qa", help="questions: JSON [{question, answer}] or markdown with '## question' headings")
    rec.add_argument("--notes")
    rec.add_argument("--no-snapshot", action="store_true", help="don't fetch the posting if no jd.md is available")
    upd = asub.add_parser("update")
    upd.add_argument("id")
    upd.add_argument("--status", choices=list(apps_mod.STATUSES))
    upd.add_argument("--note")
    upd.add_argument("--at")
    upd.add_argument("--confirmation-id")
    upd.add_argument("--qa", help="extra questions to append (same formats as record)")
    lst = asub.add_parser("list")
    lst.add_argument("--status")
    lst.add_argument("--company")
    lst.add_argument("--since")
    lst.add_argument("--json", action="store_true")
    shw = asub.add_parser("show")
    shw.add_argument("id")
    fl = asub.add_parser("file", help="list an application's files, or print/extract one")
    fl.add_argument("id")
    fl.add_argument("name", nargs="?", help="e.g. resume.pdf, cover_letter.md, jd.md, answers.md")
    fl.add_argument("--out", help="write the file here instead of printing (binary files need this)")

    db = sub.add_parser("dashboard", help="local dashboard over traces/ and applications/")
    db.add_argument("--host", default="127.0.0.1")
    db.add_argument("--port", type=int, default=8050)
    db.add_argument("--traces", help="traces dir (default RESUME_TRACES_DIR or ./traces)")
    db.add_argument("--applications", help="applications dir (default RESUME_APPLICATIONS_DIR or ./applications)")
    db.add_argument("--export", help="write a self-contained HTML file instead of serving")
    db.add_argument("--title")
    db.add_argument("--open", action="store_true", help="open a browser tab")
    db.add_argument("--read-only", action="store_true", help="disable editing application status from the page")
    db.add_argument("--allow-host", action="append", default=[], help="extra Host header to accept (e.g. a LAN name)")

    dd = sub.add_parser("demo-data", help="generate fictional traces + applications")
    dd.add_argument("--out", default="demo")
    dd.add_argument("--days", type=int, default=60)
    dd.add_argument("--jobs", type=int, default=48)
    dd.add_argument("--seed", type=int, default=7)

    gn = sub.add_parser("generate", help="run the server-side writer -> verifier pipeline locally")
    gn.add_argument("url", nargs="?")
    gn.add_argument("--text-file")
    gn.add_argument("--slug")
    gn.add_argument("--instructions", default="")
    gn.add_argument("--writer-backend", choices=["anthropic-api", "claude-code", "codex"])
    gn.add_argument("--verifier-backend", choices=["anthropic-api", "claude-code", "codex"])
    gn.add_argument("--rounds", type=int, default=3)

    lc = sub.add_parser("llm-check", help="show the LLM backend per role and send a tiny test prompt")
    lc.add_argument("--role", choices=["writer", "verifier"], action="append")
    lc.add_argument("--no-ping", action="store_true", help="only show the configuration")

    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)

    args = p.parse_args(argv)

    if args.cmd == "init":
        import shutil

        pairs = [
            (paths.EXAMPLES_DIR / "profile", paths.PROFILE_DIR),
            (paths.EXAMPLES_DIR / "templates", paths.TEMPLATES_DIR),
        ]
        for src_dir, dst_dir in pairs:
            dst_dir.mkdir(parents=True, exist_ok=True)
            for src in src_dir.iterdir():
                dst = dst_dir / src.name
                if dst.exists():
                    print(f"kept     {dst}")
                else:
                    shutil.copy(src, dst)
                    print(f"created  {dst}  (fictional example: replace with your own data)")
        return 0
    if args.cmd == "start":
        try:
            paths.require_profile()
        except paths.ProfileMissing as exc:
            print(str(exc), file=sys.stderr)
            return 4
        if not args.url and not args.text_file:
            p.error("start needs a URL or --text-file")
        return _start(args)
    if args.cmd == "fetch-jd":
        try:
            print(fetch_jd(args.url).to_markdown())
        except FetchError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        return 0
    if args.cmd == "check":
        res = compile_resume(args.tex)
        print(res.summary())
        return 0 if res.ok else 1
    if args.cmd == "proofread":
        rep = proofread(args.tex)
        out = rep.to_markdown()
        Path(args.tex).with_name("proofread.md").write_text(out)
        print(json.dumps(rep.to_dict(), indent=2) if args.json else out)
        return 0 if rep.ok else 1
    if args.cmd in ("status", "deliver"):
        job = Path(args.job)
        st_ = gate.delivery_status(job, accept_open_issues=getattr(args, "accept_open_issues", False))
        if args.cmd == "status":
            for r in gate.reviews(job):
                print(f"{r.path.name}: {r.verdict} (must fix: {r.must_fix})")
            print(("DELIVERABLE: " if st_.deliverable else "BLOCKED: ") + st_.reason)
            return 0 if st_.deliverable else 1
        from . import tracing

        with tracing.span("resume.deliver", job=job.name, deliverable=st_.deliverable, rounds=st_.rounds,
                          verdict=st_.latest.verdict if st_.latest else "", reason=st_.reason,
                          accepted_open_issues=bool(args.accept_open_issues and st_.deliverable
                                                    and st_.latest and st_.latest.verdict != "PASS")):
            pass
        if not st_.deliverable:
            print(f"BLOCKED: {st_.reason}", file=sys.stderr)
            return 1
        name = Path(args.name).name
        if not name.endswith(".pdf"):
            name += ".pdf"
        out = paths.REPO_ROOT / "outputs" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copy(job / "resume.pdf", out)
        print(f"Delivered {out} ({st_.reason})")
        return 0
    if args.cmd == "app":
        try:
            return _app(args)
        except apps_mod.ApplicationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.cmd == "dashboard":
        from . import dashboard

        tr = Path(args.traces) if args.traces else None
        ad = Path(args.applications) if args.applications else None
        if args.export:
            out = dashboard.export(Path(args.export), tr, ad, title=args.title)
            print(f"Wrote {out}")
            return 0
        dashboard.serve(args.host, args.port, tr, ad, open_browser=args.open, allow_hosts=args.allow_host,
                        read_only=args.read_only)
        return 0
    if args.cmd == "demo-data":
        from . import demo

        res = demo.generate(Path(args.out), days=args.days, jobs=args.jobs, seed=args.seed)
        print(f"Wrote {res['spans']} spans and {res['applications_count']} applications under {args.out}/ (fictional)")
        print(f"View: python3 -m resume_agent dashboard --traces {res['traces']} --applications {res['applications']}")
        return 0
    if args.cmd == "generate":
        import os

        from .llm_pipeline import PipelineError, generate_resume

        if args.writer_backend:
            os.environ["RESUME_WRITER_BACKEND"] = args.writer_backend
        if args.verifier_backend:
            os.environ["RESUME_VERIFIER_BACKEND"] = args.verifier_backend
        if not (args.url or args.text_file):
            p.error("generate needs a URL or --text-file")
        try:
            res = generate_resume(jd_url=args.url, jd_text=Path(args.text_file).read_text() if args.text_file else None,
                                  slug=args.slug, instructions=args.instructions, max_rounds=args.rounds,
                                  progress=lambda m: print(f"  {m}", file=sys.stderr))
        except (PipelineError, FetchError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({k: v for k, v in res.to_dict().items() if k != "review"}, indent=2))
        return 0 if res.ok else 1
    if args.cmd == "llm-check":
        from .llm_backends import BackendError, Message, describe, get_backend, role_model

        info = describe()
        rc = 0
        for role in args.role or ["writer", "verifier"]:
            d = info[role]
            line = f"{role:<9} backend={d.get('backend')} model={d.get('model') or '(default)'} ready={d.get('ready')}"
            if args.no_ping or not d.get("ready"):
                print(line + ("" if d.get("ready") else f"  {d.get('error', '')}"))
                rc |= 0 if d.get("ready") else 1
                continue
            try:
                r = get_backend(role).complete(system="Reply with exactly one word.", model=role_model(role),
                                               messages=[Message("user", "Say PONG")], max_tokens=20)
                print(line + f"  ping ok ({r.text.strip()[:20]!r}, {r.input_tokens}+{r.output_tokens} tokens)")
            except BackendError as exc:
                print(line + f"  ping FAILED: {exc}")
                rc = 1
        return rc
    if args.cmd == "serve":
        from .server import run

        run(host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
