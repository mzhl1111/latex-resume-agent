---
description: Tailor the one-page LaTeX resume to a job posting URL (or pasted JD) and proofread the PDF
argument-hint: <job-url | pasted JD text> [--slug name]
allowed-tools: Bash(python3 -m resume_agent:*), Bash(mkdir:*), Bash(cp:*), Read, Write, Edit, Glob, Grep, WebFetch, Task
---

Tailor the resume for: $ARGUMENTS

Follow AGENTS.md and `profile/rules.md` exactly. Work through these steps without stopping to ask unless a step says to.
If `start` exits with code 4, the private profile is missing: tell the user to run `python3 -m resume_agent init`, replace the examples with their own data, and stop.

## 1. Get the job description
- If the input is a URL, run `python3 -m resume_agent start "<url>"` (add `--slug <name>` if one was given).
  It prints JSON with the workspace paths. Remember `workspace.dir`; call it `$JOB` below.
- If it exits with code 3 (JS-rendered page or blocked), fetch the URL with WebFetch and ask it for the
  complete job description verbatim (title, company, location, responsibilities, requirements, nice-to-haves).
  Save that to a scratch file and run `python3 -m resume_agent start --text-file <file> --slug <company-role>`.
  If WebFetch also fails, ask the user to paste the JD and stop.
- If the input is pasted JD text, save it and use `--text-file` the same way.
- Read `$JOB/jd.md`. If it is clearly not a job description (a login wall, a search page, an expired
  posting), say so and stop.

## 2. Analyse (write `$JOB/analysis.md`)
Read `profile/work_history.md` and `profile/rules.md` in full. Then write:
1. **Positioning**: default or one of the variants in `profile/rules.md`, with one sentence of why.
2. **Requirements → evidence** table: the 5–8 core JD requirements, their category, the strongest
   matching fact from the fact bank (quote the fact bank wording), and strength (strong / partial / gap).
3. **Gaps**: requirements with no honest evidence. These stay out of the resume. Do not paper over them.
4. **Edit plan**: which bullets to keep, reorder, rewrite, or drop, and which Skills to reorder.
   Skills may only list tools with work-history evidence or that are already in the template.

## 3. Tailor `$JOB/resume.tex`
- Edit only the document body: Summary, bullet text and order, Skills order and wording. Leave the preamble alone.
- Every claim and every number must trace to `profile/work_history.md`. Keep its qualifiers
  ("~", "roughly", "contributed to", "designed") and its ownership boundaries.
- Escape LaTeX specials (`%`, `&`, `~` → `\textasciitilde`, `#`, `_`).

## 4. Mechanical proofread loop
Run `python3 -m resume_agent proofread $JOB/resume.tex`.
- Fix every **ERROR** (page count, invented numbers, guardrails, LaTeX leaks) by editing content, never by
  shrinking fonts or spacing.
- Fix **WARN** items unless you have a specific reason not to (write that reason in analysis.md).
- Re-run until the report says PASS. Then Read `$JOB/resume.png` yourself and look at it.

## 5. Independent review
Launch the `resume-proofreader` subagent with the Task tool. Give it only the job folder path. It has not
seen your reasoning, so it is a fair check. Apply every item it marks **MUST FIX**, use judgment on
**CONSIDER** items, then repeat step 4. Do at most 3 review rounds. If MUST FIX items remain after
that, list them for the user instead of looping.

## 6. Deliver
- `mkdir -p outputs` and copy the PDF to the output file name given in `profile/rules.md` (CamelCase, no spaces).
- Reply briefly with: the output path; the positioning chosen; the 2–3 biggest tailoring choices; any
  JD gaps; and the two human checks from the README (does any bullet overstate ownership; does the
  emphasis match the role the user actually wants).
