# Resume Customization Agent Instructions

## Goal
Given a target job description, produce a truthful, one-page, ATS-friendly LaTeX resume tailored to that role, compile it to PDF, and review the rendered page before declaring success.

## Source of truth
Personal data is kept out of git. `profile/` and `templates/` are gitignored. `examples/` holds a fictional sample with the same layout.
1. `profile/work_history.md` is the factual source of truth for work-history claims.
2. `profile/rules.md` holds this candidate's ownership boundaries, positioning variants, bullet budget and output file name. It takes precedence over the generic guidance below.
3. `templates/resume_base.tex` is the canonical visual/layout baseline.
4. Never invent metrics, responsibilities, seniority, tools, or ownership.
5. Preserve every ownership boundary in `profile/rules.md` exactly.

If `profile/` or `templates/` is missing, stop and tell the user to run `python3 -m resume_agent init` and fill in their own data.

## Customization strategy
For each JD:
1. Extract 5–8 core requirements and classify them into:
   - ML systems / platform
   - search / retrieval / ranking
   - GenAI / RAG / agents
   - model training / applied ML
   - distributed systems / reliability
   - data / feature pipelines
   - developer productivity / CI/CD
2. Map each requirement to concrete evidence from `profile/work_history.md`.
3. Select bullets by evidence strength, not keyword overlap.
4. Reorder and lightly rewrite bullets to emphasize the target role while keeping claims faithful.
5. Keep the strongest quantitative evidence whenever relevant.
6. Do not keyword-stuff or add tools that appear only in Skills unless there is work-history evidence.
7. Use the default positioning from `profile/rules.md`. Switch to a variant only when the JD strongly favors that direction.

## Page and formatting constraints
- Must remain exactly 1 page on US Letter.
- Keep 11pt body document class unless there is a compelling reason not to.
- Never reduce font size to make content fit before trying content edits.
- Keep the current compact one-line Education section.
- Summary may be 2–3 lines maximum.
- Follow the bullet budget in `profile/rules.md`.
- Prefer shortening/removing lower-value content over shrinking layout.
- Do not break the visual rhythm of the base template.

## Compilation and validation
After editing a resume:
1. Run `python3 -m resume_agent proofread <tex-file>` (compile + page check + PNG + layout,
   number and guardrail checks; writes `proofread.md`). `scripts/check_resume.sh <tex-file>`
   still works for a compile-only check.
2. Confirm `pdfinfo` reports exactly 1 page.
3. Render the PDF to PNG and inspect the image.
4. Check for:
   - clipping / overlap
   - tiny or inconsistent font
   - excessive vertical compression
   - awkward orphan lines
   - overly dense Education/Skills area
   - section headers colliding with bullets
5. If visual review is poor, fix content/layout and recompile.

## Output convention
For a role folder `jobs/<slug>/`:
- `jd.md` — source JD text/notes
- `analysis.md` — requirements-to-evidence mapping and customization rationale
- `resume.tex` — customized LaTeX
- `resume.pdf` — compiled resume
- `resume.png` — rendered first-page preview

`jobs/`, `outputs/` and `applications/` contents are gitignored because they contain personal data.
Do not overwrite `templates/resume_base.tex` when customizing a role.
