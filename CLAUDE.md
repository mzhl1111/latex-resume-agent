# Claude Code instructions

The resume rules (source of truth, ownership boundaries, positioning, page and
formatting constraints, output convention) live in AGENTS.md so Codex and
Claude Code share one copy:

@AGENTS.md

## Claude Code workflow
- Run `/resume <job-url>` (or `/resume` followed by pasted JD text). The main
  thread only orchestrates: `resume-writer` writes, a fresh `resume-proofreader`
  verifies each round, and `python3 -m resume_agent deliver` ships the PDF.
- Writer/verifier separation is enforced by hooks (`.claude/hooks/resume_harness.py`
  → `resume_agent/harness.py`). If a hook denies a call, follow its message rather
  than working around it. Never write `review-*.md` or into `outputs/` directly.
- Shared tooling is the `resume_agent` Python package. Use the CLI rather than
  re-implementing steps:
  - `python3 -m resume_agent start <url> [--slug S]` — fetch JD, create `jobs/<slug>/`
  - `python3 -m resume_agent start --text-file F --slug S` — same, from pasted text
  - `python3 -m resume_agent check <tex>` — compile, 1-page check, PNG render
  - `python3 -m resume_agent proofread <tex>` — compile + layout/fact/guardrail checks,
    writes `proofread.md` next to the tex; exit code 1 means there are errors
- After the user says they submitted an application, run `/record-application` (or
  `python3 -m resume_agent app record ...`). Record answers verbatim; never edit
  files under `applications/` directly.
- Guardrail phrases are in `profile/guardrails.json`; every number in the PDF must
  exist in `profile/work_history.md` or the proofreader fails it.
- Personal data is gitignored: `profile/` (work_history.md, rules.md,
  guardrails.json), `templates/`, `jobs/`, `outputs/`, `applications/`. Never commit them, and never
  copy personal details into tracked files (code, prompts, tests, README).
  `examples/` is a fictional stand-in; `python3 -m resume_agent init` copies it.
- Never edit `templates/resume_base.tex` or anything in `profile/` while tailoring a
  resume. Changes to those are deliberate, separate requests.
- Run tests with `python3 -m pytest -q`.
