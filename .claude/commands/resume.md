---
description: Tailor the one-page LaTeX resume to a job posting URL (or pasted JD) with separate writer and verifier agents
argument-hint: <job-url | pasted JD text> [--slug name]
allowed-tools: Bash(python3 -m resume_agent:*), Read, Write, Glob, Grep, WebFetch, Task
---

Tailor the resume for: $ARGUMENTS

You are the **orchestrator**. You route work between two subagents, each in its own clean context:

- `resume-writer` writes and revises the resume.
- `resume-proofreader` is the verifier: a new instance every round, which sees only the job folder.

The repo's hooks enforce this (`.claude/settings.json` → `resume_agent/harness.py`):
- Only the writer can edit `resume.tex` / `analysis.md`.
- The verifier's prompt is rewritten to just the folder path.
- The verifier is read-only and can't see the writer's notes or earlier reviews.
- Messaging an old verifier is blocked.
- The verifier's final reply is saved as `review-N.md` by the hook, not by you.
- Copying to `outputs/` only works through the gated `deliver` command.

If a hook denies a call, follow its message. Don't try to work around it.

## 1. Get the job description
- If the input is a URL, run `python3 -m resume_agent start "<url>"` (add `--slug <name>` if one was
  given). It prints JSON with the workspace paths. Call `workspace.dir` `$JOB` below.
- Exit code 4 means the private profile is missing. Tell the user to run `python3 -m resume_agent init`,
  replace the examples with their own data, and stop.
- Exit code 3 means the page renders with JavaScript or blocks bots. Fetch the URL with WebFetch and ask
  for the complete job description verbatim (title, company, location, responsibilities, requirements,
  nice-to-haves). Save it to a scratch file and run
  `python3 -m resume_agent start --text-file <file> --slug <company-role>`. If WebFetch also fails, ask
  the user to paste the JD and stop.
- If the input is pasted JD text, save it and use `--text-file` the same way.
- Read `$JOB/jd.md`. If it is clearly not a job description (login wall, search page, expired
  posting), say so and stop.

## 2. Write
Launch `resume-writer` with the Task tool. Prompt: `Job folder: $JOB/. First pass.` plus any
user-supplied instructions from $ARGUMENTS (for example "emphasize search").

## 3. Verify (up to 3 rounds)
1. Launch a **new** `resume-proofreader` with the prompt `Job folder: $JOB/`. The harness strips anything
   else you add.
2. When it finishes, run `python3 -m resume_agent status $JOB`. It lists the recorded `review-N.md`
   files and whether the job is deliverable.
3. If it says `DELIVERABLE`, go to step 4.
4. Otherwise launch `resume-writer` with `Job folder: $JOB/. Revision: address $JOB/review-N.md.` (the
   newest review), then go back to step 1.

After 3 rounds, stop looping.

## 4. Deliver
- Run `python3 -m resume_agent deliver $JOB --name <file>.pdf`, using the output file name pattern
  from `profile/rules.md` (CamelCase, no spaces). If 3 rounds ended without a PASS, add
  `--accept-open-issues` only after telling the user which MUST FIX items are still open.
- Reply briefly with:
  - the output path;
  - the final verdict and how many rounds it took;
  - any MUST FIX items still open, quoted from the last review;
  - the positioning and the 2–3 biggest tailoring choices, taken from `$JOB/analysis.md`;
  - JD gaps;
  - the two human checks: does any bullet overstate ownership, and does the emphasis match the role the
    user actually wants.
