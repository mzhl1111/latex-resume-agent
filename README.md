# Resume Codex Workflow

A local, Codex-friendly project for tailoring a one-page LaTeX resume to a job description while preserving factual accuracy and visual quality.

## Why this structure
The hard part of resume customization is not LaTeX generation. It is repeatedly making the same judgment calls:
- which work facts are strongest for this JD,
- what ownership can be claimed safely,
- which metrics deserve space,
- how to stay within one page without shrinking the layout,
- and whether the compiled PDF still looks good.

This repo turns those decisions into persistent project instructions.

## Project structure

```text
AGENTS.md                  Codex instructions and guardrails
profile/work_history.md    Detailed factual work-history bank
templates/resume_base.tex  Canonical one-page visual baseline
scripts/new_job.py         Create a role workspace
scripts/check_resume.sh    Compile + verify exactly one page + render PNG
jobs/<slug>/               One folder per target role
outputs/                   Optional final exports
```

## Local dependencies
- Python 3
- `pdflatex`
- Poppler tools: `pdfinfo` and `pdftoppm`

## One-job workflow

### 1. Create a role folder
```bash
python scripts/new_job.py stripe-ml-infra
```

### 2. Add the JD
Paste the JD into `jobs/stripe-ml-infra/jd.md`.

### 3. Ask Codex
Example prompt:

> Read AGENTS.md, profile/work_history.md, templates/resume_base.tex, and jobs/stripe-ml-infra/jd.md. Analyze the JD, write the requirements-to-evidence mapping to analysis.md, customize resume.tex without inventing facts, compile it, verify it is exactly one page, render and inspect the PNG, and iterate until the formatting is clean. Preserve the 11pt layout unless content editing cannot solve the issue.

### 4. Validate locally
```bash
bash scripts/check_resume.sh jobs/stripe-ml-infra/resume.tex
```

### 5. Review
Check:
- `jobs/stripe-ml-infra/analysis.md`
- `jobs/stripe-ml-infra/resume.tex`
- `jobs/stripe-ml-infra/resume.pdf`
- `jobs/stripe-ml-infra/resume.png`

## Recommended human-in-the-loop step
Before submitting, review only two things:
1. whether any bullet overstates ownership;
2. whether the selected emphasis matches the role you actually want.

Everything else should be repeatable by Codex.
