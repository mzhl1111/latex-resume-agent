# LaTeX Resume Agent

Tailor a one-page LaTeX resume to a job posting, given just the link. The agent fetches the JD,
maps requirements to evidence from a factual work-history bank, edits the template, compiles it,
proofreads the PDF (mechanically and with an independent visual reviewer), and hands back the result.

It runs two ways on one shared core:

| Mode | Who does the LLM work | Entry point |
|---|---|---|
| **Claude Code** | Claude Code on your plan | `/resume <job-url>` |
| **MCP / A2A server** | the calling agent, or the server with an `ANTHROPIC_API_KEY` | `docker compose up` |

Codex still works too: `AGENTS.md` is the single copy of the rules, and `CLAUDE.md` imports it.

## Your data stays local
Everything personal is gitignored: `profile/` (your fact bank, rules and guardrails), `templates/`
(your LaTeX resume), `jobs/` and `outputs/`. The repo ships a **fictional** example in `examples/`.

```bash
python3 -m resume_agent init     # copies examples/ into profile/ and templates/ (never overwrites)
# then replace them with your own:
#   profile/work_history.md   every fact, metric and ownership note you are allowed to claim
#   profile/rules.md          ownership boundaries, positioning variants, bullet budget, output name
#   profile/guardrails.json   overclaim phrases the proofreader should flag
#   templates/resume_base.tex your one-page LaTeX resume (the visual baseline)
```

Keep a private backup of `profile/` and `templates/` (e.g. a private repo or cloud drive). Git won't.

## Why this structure
The hard part of resume customization is not LaTeX generation. It is repeatedly making the same judgment calls:
- which work facts are strongest for this JD,
- what ownership can be claimed safely,
- which metrics deserve space,
- how to stay within one page without shrinking the layout,
- and whether the compiled PDF still looks good.

The repo turns those decisions into persistent instructions (`AGENTS.md`) and turns everything that
can be checked mechanically into code (`resume_agent/proofread.py`, `profile/guardrails.json`).

## Project structure

```text
AGENTS.md                          Generic rules (shared by Codex, Claude Code, the server)
CLAUDE.md                          Claude Code entry; imports AGENTS.md
.claude/commands/resume.md         /resume slash command (full workflow)
.claude/agents/resume-proofreader.md  Independent reviewer subagent (also the server's review prompt)
.claude/settings.json              Tool permissions; profile/ and templates/ are edit-denied
examples/                          Fictional profile + template (tests use these)
profile/            (gitignored)   work_history.md, rules.md, guardrails.json: your private data
templates/          (gitignored)   resume_base.tex: your one-page visual baseline
resume_agent/                      Shared Python core
  jd_fetch.py                        URL → JD (Greenhouse, Lever, Ashby, Workday, LinkedIn, JSON-LD, HTML)
  compile.py                         pdflatex + 1-page check + PNG + log parsing
  proofread.py                       layout / fact / guardrail / hygiene checks
  workspace.py                       jobs/<slug>/ folders
  llm_pipeline.py                    server-side writer → proofread → reviewer loop (Anthropic API)
  mcp_server.py, a2a_server.py, server.py   MCP + A2A over one HTTP app
scripts/                           Original shell helpers (still work)
jobs/<slug>/                       One folder per target role
outputs/                           Final PDFs
```

## Local dependencies
- Python 3.11+
- `pdflatex` (TeX Live with `titlesec`, `enumitem`, `fullpage`, `marvosym`)
- Poppler: `pdfinfo`, `pdftoppm`, `pdftotext`
- `pip install -e ".[server,dev]"` (only `requests` is needed for Claude Code mode)

## Mode 1: Claude Code (uses your plan)

```bash
cd latex-resume-agent
claude
> /resume https://job-boards.greenhouse.io/acme/jobs/1234567
```

The command:
1. `python3 -m resume_agent start <url>` fetches the JD into `jobs/<slug>/jd.md`. If the page needs
   JavaScript, it falls back to WebFetch, and then to asking you to paste the JD.
2. Writes `analysis.md` (positioning, requirements → evidence, gaps, edit plan).
3. Edits `resume.tex` (body only).
4. Loops `python3 -m resume_agent proofread` until there are no errors, then looks at `resume.png` itself.
5. Runs the `resume-proofreader` subagent. It starts with fresh context and checks the rendered page and
   every claim against the fact bank. MUST FIX items are applied, for at most 3 rounds.
6. Copies the PDF to `outputs/` (file name set in `profile/rules.md`) and summarises the tailoring choices.

The CLI is also usable by hand:

```bash
python3 -m resume_agent start <url> [--slug acme-ml]       # or: start --text-file jd.txt --slug acme-ml
python3 -m resume_agent proofread jobs/acme-ml/resume.tex   # writes proofread.md; exit 1 on errors
python3 -m resume_agent check jobs/acme-ml/resume.tex       # compile + 1 page + PNG only
```

## Mode 2: MCP / A2A server

```bash
cp .env.example .env    # set RESUME_AGENT_TOKEN, optionally ANTHROPIC_API_KEY and PUBLIC_BASE_URL
# profile/ and templates/ are mounted read-only into the container, not baked into the image
docker compose up --build
curl localhost:8080/healthz
```

| Endpoint | Protocol |
|---|---|
| `POST /mcp` | MCP, streamable HTTP (stateless) |
| `GET /.well-known/agent-card.json` | A2A agent card |
| `POST /a2a` | A2A JSON-RPC: `message/send`, `tasks/get` |

All endpoints except health and the agent card require `Authorization: Bearer $RESUME_AGENT_TOKEN`.

### MCP tools
| Tool | Purpose |
|---|---|
| `generate_resume(jd_url \| jd_text, slug?, instructions?)` | Full pipeline on the server and returns the PDF. Needs `ANTHROPIC_API_KEY`. |
| `start_job(jd_url \| jd_text, slug?)` | Fetches the JD and creates a workspace. |
| `get_resume_context(slug?)` | Returns the rules, fact bank, template, guardrails, reviewer checklist, and the job's JD/tex. |
| `write_resume(slug, resume_tex, analysis_md?)` | Saves, compiles and proofreads; returns the report and the rendered page image. |
| `proofread_resume(slug)` / `get_resume_pdf(slug)` / `list_jobs()` / `fetch_job_description(url)` | Helpers. |

There are two ways a calling agent can use the server:
- **The caller does the writing (no server key needed).** Use the `tailor_resume` prompt:
  `start_job` → `get_resume_context` → `write_resume` (repeat) → `get_resume_pdf`. The caller's own
  model does the tailoring, and the server keeps it honest with the proofreader.
- **The server does the writing.** Call `generate_resume`. It runs writer → mechanical proofread →
  independent reviewer (the same prompt as the Claude Code subagent, given the rendered PNG) → fix,
  for up to 3 rounds. Models are set by `RESUME_WRITER_MODEL` / `RESUME_REVIEWER_MODEL`.

Example Claude Code client config (`.mcp.json` in another project):

```json
{
  "mcpServers": {
    "resume": {
      "type": "http",
      "url": "https://resume.example.com/mcp",
      "headers": { "Authorization": "Bearer ${RESUME_AGENT_TOKEN}" }
    }
  }
}
```

### A2A
Send a text part with a job URL (any extra text becomes instructions), or a data part
`{"jd_url", "jd_text", "slug", "instructions"}`. The completed task has a `resume.pdf` file artifact and
an `analysis.md` text artifact. Pass `configuration.blocking: false` to get a `working` task back
immediately, then poll `tasks/get`. Server-side generation requires `ANTHROPIC_API_KEY`.

## Proofreading checks
`resume_agent/proofread.py` fails the resume on:
- not exactly one page, or LaTeX errors;
- any number that doesn't appear in `work_history.md` or the template;
- `error` guardrails from `profile/guardrails.json` (e.g. sole-ownership language, claiming work someone else owned);
- LaTeX markup leaking into the PDF text, or an 11pt class change.

It warns on orphan lines, bottom whitespace, squeezed line spacing, overfull boxes, a preamble that
drifted from the template, `warn` guardrails, and repeated words.

## Tests
```bash
python3 -m pytest -q
```

## Recommended human-in-the-loop step
Before submitting, review only two things:
1. whether any bullet overstates ownership;
2. whether the selected emphasis matches the role you actually want.
