"""Claude Code hook policy: writer/verifier separation enforced by the harness.

Roles come from the hook input: ``agent_type`` is set inside subagents and absent
on the main thread (the orchestrator).

    orchestrator          main thread. Routes work; cannot write the resume, analysis or reviews.
    resume-writer         the only role that may write jobs/<slug>/resume.tex and analysis.md.
    resume-proofreader    the verifier. Read-only; cannot see analysis.md, earlier reviews or harness
                          state; its launch prompt is rewritten to just the job folder; a new instance
                          every round (resuming / messaging an old verifier is blocked).

The SubagentStop hook, not any agent, records the verifier's reply as review-N.md
(stamped with the resume.tex hash), and ``resume_agent deliver`` refuses to copy a
PDF to outputs/ unless the latest review is a PASS for the current resume.tex.

Everything here is pure functions over the hook JSON so it can be unit tested;
``.claude/hooks/resume_harness.py`` is the thin stdin/stdout shim.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from . import gate

WRITER = "resume-writer"
VERIFIER = "resume-proofreader"
ORCHESTRATOR = "orchestrator"
AGENT_TOOLS = {"Task", "Agent"}
WRITE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob"}

JOB_RE = re.compile(r"((?:[^\s'\"`(=]*/)?jobs/[A-Za-z0-9][A-Za-z0-9._-]*)")
WRITER_ONLY = re.compile(r"(^|/)jobs/[^/]+/(resume\.tex|analysis\.md)$")
REVIEW_FILE = re.compile(r"(^|/)jobs/[^/]+/review-[^/]*$")
HIDDEN_FROM_VERIFIER = re.compile(r"analysis\.md|review-|\.harness")
VERIFIER_BASH = re.compile(
    r"^\s*(?:"
    r"python3? -m resume_agent proofread \S+"
    r"|pdftotext(?: -layout)?(?: -f \d+ -l \d+)? \S+(?: -)?"
    r"|pdfinfo \S+"
    r"|ls(?: -[a-zA-Z]+)?(?: \S+)?"
    r")\s*$"
)
SHELL_META = re.compile(r"[;&|`<>]|\$\(")
SHELL_WRITE = re.compile(r"(>|\btee\b|\bcp\b|\bmv\b|\brm\b|\bsed\s+-i|\bperl\s+-i|\bln\b|\btouch\b|\btruncate\b)")


# --------------------------------------------------------------------------- helpers


def role_of(ev: dict) -> str:
    return ev.get("agent_type") or ORCHESTRATOR


def project_dir(ev: dict) -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or ev.get("cwd") or ".").resolve()


def _rel(ev: dict, p: str | None) -> str:
    if not p:
        return ""
    path = Path(p)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(project_dir(ev)).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix().removeprefix("./")


def _state_path(ev: dict) -> Path:
    return project_dir(ev) / "jobs" / ".harness" / "state.json"


def load_state(ev: dict) -> dict:
    p = _state_path(ev)
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"pending": {WRITER: [], VERIFIER: []}, "agents": {}}


def save_state(ev: dict, state: dict) -> None:
    p = _state_path(ev)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


def deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"[resume harness] {reason}",
        }
    }


def rewrite(tool_input: dict, reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"[resume harness] {reason}",
            "updatedInput": tool_input,
        }
    }


def find_job(ev: dict, text: str) -> str | None:
    """First jobs/<slug> path in text, normalised to be relative to the project."""
    m = JOB_RE.search(text or "")
    return _rel(ev, m.group(1)) if m else None


# --------------------------------------------------------------------------- PreToolUse


def _agent_launch(ev: dict, ti: dict) -> dict | None:
    kind = ti.get("subagent_type")
    if kind not in (WRITER, VERIFIER):
        return None
    job = find_job(ev, ti.get("prompt", ""))
    if not job or not (project_dir(ev) / job / "resume.tex").exists():
        return deny(f"{kind} must be launched with an existing job folder, e.g. 'Job folder: jobs/<slug>/'.")
    state = load_state(ev)
    state["pending"].setdefault(kind, []).append(job)
    save_state(ev, state)
    if kind == WRITER:
        return None
    # Verifier: strip everything but the folder, and never resume an earlier instance.
    clean = {k: v for k, v in ti.items() if k not in ("resume", "agent_id", "session_id")}
    clean["prompt"] = f"Job folder: {job}/"
    clean["description"] = "Independent resume verification"
    changed = clean != ti
    return rewrite(clean, "verifier gets only the job folder" if changed else "verifier launch ok")


def _send_message(ev: dict, ti: dict) -> dict | None:
    state = load_state(ev)
    verifier_ids = [a for a, info in state.get("agents", {}).items() if info.get("type") == VERIFIER]
    blob = json.dumps(ti)
    if VERIFIER in blob or any(a in blob for a in verifier_ids):
        return deny("A verifier is single-use. Launch a fresh resume-proofreader instead of messaging an old one.")
    return None


def _write(ev: dict, role: str, ti: dict) -> dict | None:
    path = _rel(ev, ti.get("file_path") or ti.get("notebook_path"))
    if role == VERIFIER:
        return deny("The verifier is read-only.")
    if REVIEW_FILE.search(path) or path.startswith("jobs/.harness"):
        return deny("Review records are written only by the harness (SubagentStop hook).")
    if path.startswith("applications/"):
        return deny("Application records are written through `python3 -m resume_agent app record|update` "
                    "(or the MCP/A2A tools) so the index and hashes stay consistent.")
    if path.startswith("outputs/"):
        return deny("Deliver with `python3 -m resume_agent deliver <job> --name <file>.pdf`; it checks the review gate.")
    if WRITER_ONLY.search(path) and role != WRITER:
        return deny(f"Only the resume-writer subagent may edit {path}. Launch it with the Task tool.")
    if role == WRITER and (path.startswith("profile/") or path.startswith("templates/")):
        return deny("The writer must not change the fact bank, rules or template.")
    return None


def _read(ev: dict, role: str, tool: str, ti: dict) -> dict | None:
    if role != VERIFIER:
        return None
    targets = [_rel(ev, ti.get(k)) for k in ("file_path", "path") if ti.get(k)]
    if tool == "Glob" and ti.get("pattern"):
        targets.append(str(ti["pattern"]))
    if tool == "Grep" and ti.get("glob"):
        targets.append(str(ti["glob"]))
    if any(HIDDEN_FROM_VERIFIER.search(t) for t in targets):
        return deny("The verifier may not read the writer's analysis, earlier reviews or harness state.")
    if tool in ("Grep", "Glob"):
        base = _rel(ev, ti.get("path")) if ti.get("path") else "."
        scope = " ".join([base] + targets)
        if base in (".", "") or "jobs" in scope:
            return deny(
                "Searches may not cover the project root or job folders (they contain writer notes). "
                "Read the job's resume.png / resume.pdf / resume.tex / jd.md by name, and search profile/ files."
            )
    return None


def _bash(ev: dict, role: str, ti: dict) -> dict | None:
    cmd = ti.get("command", "")
    if role == VERIFIER:
        if HIDDEN_FROM_VERIFIER.search(cmd) or SHELL_META.search(cmd) or not VERIFIER_BASH.match(cmd):
            return deny(
                "Verifier Bash is limited to: python3 -m resume_agent proofread <tex> | pdftotext [-layout] <pdf> - "
                "| pdfinfo <pdf> | ls <dir>."
            )
        return None
    if re.match(r"^\s*python3? -m resume_agent\b", cmd) and not SHELL_META.search(cmd.replace("2>&1", "")):
        if role == WRITER and re.search(r"resume_agent\s+(deliver|app)\b", cmd):
            return deny("Only the orchestrator delivers or records applications.")
        return None
    if SHELL_WRITE.search(cmd):
        if re.search(r"review-|jobs/\.harness", cmd):
            return deny("Review records are written only by the harness.")
        if re.search(r"\bapplications/", cmd):
            return deny("Use `python3 -m resume_agent app ...` to change application records.")
        if re.search(r"\boutputs/", cmd):
            return deny("Deliver with `python3 -m resume_agent deliver <job> --name <file>.pdf`.")
        if role != WRITER and re.search(r"jobs/[^/\s]+/(resume\.tex|analysis\.md)", cmd):
            return deny("Only the resume-writer subagent may change resume.tex / analysis.md.")
        if role == WRITER and re.search(r"\b(profile|templates)/", cmd):
            return deny("The writer must not change the fact bank, rules or template.")
    return None


def pre_tool_use(ev: dict) -> dict | None:
    tool, ti, role = ev.get("tool_name", ""), ev.get("tool_input") or {}, role_of(ev)
    if tool in AGENT_TOOLS:
        return _agent_launch(ev, ti) if role == ORCHESTRATOR else None
    if tool == "SendMessage":
        return _send_message(ev, ti)
    if tool in WRITE_TOOLS:
        return _write(ev, role, ti)
    if tool in READ_TOOLS:
        return _read(ev, role, tool, ti)
    if tool == "Bash":
        return _bash(ev, role, ti)
    if role == VERIFIER and tool not in READ_TOOLS:
        return deny(f"The verifier may not use {tool}.")
    return None


# --------------------------------------------------------------------------- subagent lifecycle


def subagent_start(ev: dict) -> dict | None:
    kind, agent_id = ev.get("agent_type"), ev.get("agent_id")
    if kind not in (WRITER, VERIFIER) or not agent_id:
        return None
    state = load_state(ev)
    queue = state["pending"].setdefault(kind, [])
    job = queue.pop(0) if queue else None
    state.setdefault("agents", {})[agent_id] = {"type": kind, "job": job}
    save_state(ev, state)
    if kind == VERIFIER and job:
        ctx = (
            f"You are verifying {job}/. You are a fresh verifier: you have no prior rounds and no access to the "
            "writer's notes. The harness will record your final reply as the review."
        )
        return {"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": ctx}}
    return None


def _last_message(ev: dict) -> str:
    if ev.get("last_assistant_message"):
        return ev["last_assistant_message"]
    path = ev.get("agent_transcript_path")
    text = ""
    if path and Path(path).exists():
        for line in Path(path).read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message") or {}
            if (rec.get("type") or msg.get("role")) == "assistant":
                content = msg.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                    if any(parts):
                        text = "\n".join(parts)
    return text


def subagent_stop(ev: dict) -> dict | None:
    if ev.get("agent_type") != VERIFIER:
        return None
    state = load_state(ev)
    info = state.get("agents", {}).get(ev.get("agent_id", ""), {})
    job = info.get("job")
    if not job:
        return None
    review = _last_message(ev)
    verdict, _ = gate.parse_verdict(review)
    if verdict is None and not ev.get("stop_hook_active"):
        return {
            "decision": "block",
            "reason": "[resume harness] Finish with the required format: a line 'VERDICT: PASS' or "
            "'VERDICT: NEEDS CHANGES', then 'MUST FIX' and 'CONSIDER' sections.",
        }
    path = gate.record_review(project_dir(ev) / job, review, source=f"claude-code verifier {ev.get('agent_id')}")
    info["review"] = path.name
    save_state(ev, state)
    return None


# --------------------------------------------------------------------------- entry


def handle(ev: dict) -> Any:
    name = ev.get("hook_event_name")
    if name == "PreToolUse":
        return pre_tool_use(ev)
    if name == "SubagentStart":
        return subagent_start(ev)
    if name == "SubagentStop":
        return subagent_stop(ev)
    return None
