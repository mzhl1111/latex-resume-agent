# Resume Customization Agent Instructions

## Goal
Given a target job description, produce a truthful, one-page, ATS-friendly LaTeX resume tailored to that role, compile it to PDF, and review the rendered page before declaring success.

## Source of truth
1. `profile/work_history.md` is the factual source of truth for work-history claims.
2. `templates/resume_base.tex` is the canonical visual/layout baseline.
3. Never invent metrics, responsibilities, seniority, tools, or ownership.
4. Preserve ownership boundaries exactly, especially:
   - Northwind ranking-model training was owned by another engineer.
   - Northwind LLM evaluation became an org-wide effort; only the initial service design is claimable.
   - Contoso deployment platform was a team effort; describe contributions, not ownership.

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

## Default positioning
The default profile is:
"Machine Learning Engineer with production search, ranking, and ML platform experience."

Use variants only when the JD strongly favors one direction:
- Search / Retrieval: lead with the Northwind search platform and latency work.
- ML Platform: lead with Contoso deployment platform and CI migration.
- Applied ML: lead with forecasting and churn modeling.

## Page and formatting constraints
- Must remain exactly 1 page on US Letter.
- Keep 11pt body document class unless there is a compelling reason not to.
- Never reduce font size to make content fit before trying content edits.
- Keep the current compact one-line Education section.
- Summary may be 2–3 lines maximum.
- Prefer Northwind 4–5 bullets, Contoso 2–3, Fabrikam 1–2, OpenVector 1 line.
- Prefer shortening/removing lower-value content over shrinking layout.
- Do not break the visual rhythm of the base template.

## Compilation and validation
After editing a resume:
1. Run `scripts/check_resume.sh <tex-file>`.
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

Do not overwrite `templates/resume_base.tex` when customizing a role.
