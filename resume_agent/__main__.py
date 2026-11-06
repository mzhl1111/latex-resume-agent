"""CLI used by the Claude Code workflow (and handy by hand).

    python -m resume_agent init                        copy fictional examples into profile/ + templates/
    python -m resume_agent start <url> [--slug S]     fetch JD + create jobs/<slug>/
    python -m resume_agent start --text-file jd.txt --slug S
    python -m resume_agent fetch-jd <url>              print JD markdown only
    python -m resume_agent check <tex>                 compile + 1-page check + PNG
    python -m resume_agent proofread <tex> [--json]    compile + mechanical proofread
    python -m resume_agent status <job-dir>            review rounds + delivery gate
    python -m resume_agent deliver <job-dir> --name F  copy PDF to outputs/ if the verifier passed it
    python -m resume_agent serve [--port 8080]         MCP + A2A HTTP server
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
    if args.cmd == "serve":
        from .server import run

        run(host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
