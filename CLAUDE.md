# Claude Code instructions

The resume rules (source of truth, ownership boundaries, positioning, page and
formatting constraints, output convention) live in AGENTS.md so Codex and
Claude Code share one copy:

@AGENTS.md

## Claude Code workflow
- Run `/resume <job-url>` (or `/resume` followed by pasted JD text) for the full
  flow: fetch JD → analysis → tailored `resume.tex` → compile → mechanical
  proofread → independent `resume-proofreader` subagent review → final PDF.
- Shared tooling is the `resume_agent` Python package. Use the CLI rather than
  re-implementing steps:
  - `python3 -m resume_agent start <url> [--slug S]` — fetch JD, create `jobs/<slug>/`
  - `python3 -m resume_agent start --text-file F --slug S` — same, from pasted text
  - `python3 -m resume_agent check <tex>` — compile, 1-page check, PNG render
  - `python3 -m resume_agent proofread <tex>` — compile + layout/fact/guardrail checks,
    writes `proofread.md` next to the tex; exit code 1 means there are errors
- Guardrail phrases are in `profile/guardrails.json`; every number in the PDF must
  exist in `profile/work_history.md` or the proofreader fails it.
- Personal data is gitignored: `profile/` (work_history.md, rules.md,
  guardrails.json), `templates/`, `jobs/`, `outputs/`. Never commit them, and never
  copy personal details into tracked files (code, prompts, tests, README).
  `examples/` is a fictional stand-in; `python3 -m resume_agent init` copies it.
- Never edit `templates/resume_base.tex` or anything in `profile/` while tailoring a
  resume. Changes to those are deliberate, separate requests.
- Run tests with `python3 -m pytest -q`.
