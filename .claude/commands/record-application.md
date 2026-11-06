---
description: Record an application you submitted (job link, resume used, cover letter, application Q&A) in applications/
argument-hint: <job-url> [job slug] then paste questions/answers, cover letter, notes
allowed-tools: Bash(python3 -m resume_agent:*), Read, Write, Glob
---

Record a submitted application from: $ARGUMENTS

1. Work out the **job URL** and the **job workspace** (`jobs/<slug>/`) whose `resume.pdf` was submitted.
   If no slug was given, look for a job folder whose `jd.md` mentions this URL. If the user submitted a
   different PDF, ask for its path and use `--pdf`.
2. From what the user pasted, extract:
   - every application question with the **exact** answer they submitted, written to a scratch file
     `/tmp/qa.json` as `[{"question": "...", "answer": "..."}]`;
   - the cover letter, if any, written to `/tmp/cover_letter.md` verbatim (or note its file path);
   - the submission time (default now), confirmation number, referral, salary answer, and anything else.
     Things that don't fit a flag go into `notes` or a `--from-json` file's `extra` object.
   Don't rewrite or "improve" answers. Record them as submitted.
3. Run
   `python3 -m resume_agent app record --url "<url>" --slug <slug> --qa /tmp/qa.json [--cover-letter /tmp/cover_letter.md] [--submitted-at ...] [--confirmation-id ...] [--notes "..."] --by claude-code`
4. Report the application id and folder, and repeat any warnings (an unverified resume, a submitted PDF
   that differs from the generated one, a possible duplicate).

For later status changes, use: `python3 -m resume_agent app update <id> --status interview --note "..."`.
