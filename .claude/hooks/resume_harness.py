#!/usr/bin/env python3
"""Claude Code hook shim: stdin JSON -> resume_agent.harness policy -> stdout JSON.

Wired in .claude/settings.json for PreToolUse, SubagentStart and SubagentStop.
If the policy itself crashes, calls made inside the resume subagents are blocked
(fail closed) while the main thread is left alone so ordinary dev work keeps going.
"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

ev = {}
try:
    ev = json.load(sys.stdin)
    from resume_agent.harness import handle

    out = handle(ev)
    if out:
        print(json.dumps(out))
    sys.exit(0)
except Exception:  # noqa: BLE001
    traceback.print_exc(file=sys.stderr)
    in_resume_agent = ev.get("agent_type") in ("resume-writer", "resume-proofreader")
    sys.exit(2 if in_resume_agent and ev.get("hook_event_name") == "PreToolUse" else 0)
