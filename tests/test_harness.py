"""Harness policy tests: feed Claude Code hook payloads, check decisions."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from resume_agent import gate, harness
from resume_agent.compile import check_dependencies, compile_resume
from resume_agent.workspace import new_job

needs_tex = pytest.mark.skipif(bool(check_dependencies()), reason="pdflatex/poppler not installed")
REPO = Path(__file__).resolve().parents[1]
PASS_REVIEW = "VERDICT: PASS\n\nMUST FIX\n- none\n\nCONSIDER\n- none"
FAIL_REVIEW = "VERDICT: NEEDS CHANGES\n\nMUST FIX\n- [Northwind 1] overstated scope → say 'moved'\n\nCONSIDER\n- none"


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    ws = new_job("acme", "# JD\n", jobs_dir=tmp_path / "jobs")
    return tmp_path, ws


def ev(proj_dir, tool=None, ti=None, agent=None, event="PreToolUse", **extra):
    e = {"hook_event_name": event, "cwd": str(proj_dir), "session_id": "s"}
    if tool:
        e.update(tool_name=tool, tool_input=ti or {})
    if agent:
        e.update(agent_type=agent, agent_id=extra.pop("agent_id", f"{agent}-1"))
    e.update(extra)
    return e


def decision(out):
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision")


# ----------------------------------------------------------------- write locks


@pytest.mark.parametrize("target", ["jobs/acme/resume.tex", "jobs/acme/analysis.md"])
def test_only_writer_edits_resume(proj, target):
    root, _ = proj
    path = str(root / target)
    assert decision(harness.handle(ev(root, "Edit", {"file_path": path}))) == "deny"  # orchestrator
    assert decision(harness.handle(ev(root, "Write", {"file_path": path}, agent=harness.VERIFIER))) == "deny"
    assert harness.handle(ev(root, "Edit", {"file_path": path}, agent=harness.WRITER)) is None


def test_reviews_and_outputs_are_harness_only(proj):
    root, _ = proj
    for role in (None, harness.WRITER):
        assert decision(harness.handle(ev(root, "Write", {"file_path": "jobs/acme/review-1.md"}, agent=role))) == "deny"
        assert decision(harness.handle(ev(root, "Write", {"file_path": "outputs/x.pdf"}, agent=role))) == "deny"
        assert decision(harness.handle(ev(root, "Bash", {"command": "echo PASS > jobs/acme/review-9.md"}, agent=role))) == "deny"
    assert decision(harness.handle(ev(root, "Bash", {"command": "cp jobs/acme/resume.pdf outputs/a.pdf"}))) == "deny"
    assert decision(harness.handle(ev(root, "Bash", {"command": "sed -i s/a/b/ jobs/acme/resume.tex"}))) == "deny"


def test_writer_cannot_touch_profile_or_deliver(proj):
    root, _ = proj
    w = harness.WRITER
    assert decision(harness.handle(ev(root, "Write", {"file_path": "profile/work_history.md"}, agent=w))) == "deny"
    assert decision(harness.handle(ev(root, "Bash", {"command": "python3 -m resume_agent deliver jobs/acme --name a.pdf"}, agent=w))) == "deny"
    assert harness.handle(ev(root, "Bash", {"command": "python3 -m resume_agent proofread jobs/acme/resume.tex"}, agent=w)) is None


def test_ordinary_dev_work_is_untouched(proj):
    root, _ = proj
    assert harness.handle(ev(root, "Edit", {"file_path": str(root / "resume_agent/jd_fetch.py")})) is None
    assert harness.handle(ev(root, "Bash", {"command": "python3 -m pytest -q"})) is None
    assert harness.handle(ev(root, "Bash", {"command": "git status"})) is None


# ----------------------------------------------------------------- verifier isolation


@pytest.mark.parametrize(
    "tool,ti",
    [
        ("Read", {"file_path": "jobs/acme/analysis.md"}),
        ("Read", {"file_path": "jobs/acme/review-1.md"}),
        ("Read", {"file_path": "jobs/.harness/state.json"}),
        ("Grep", {"pattern": "owned", "path": "jobs/acme"}),
        ("Glob", {"pattern": "jobs/acme/*.md"}),
        ("Bash", {"command": "cat jobs/acme/analysis.md"}),
        ("Bash", {"command": "python3 -m resume_agent proofread jobs/acme/resume.tex; cat jobs/acme/review-1.md"}),
        ("Bash", {"command": "python3 -c 'print(1)'"}),
        ("Edit", {"file_path": "jobs/acme/resume.tex"}),
        ("WebFetch", {"url": "https://example.com"}),
    ],
)
def test_verifier_isolation_denies(proj, tool, ti):
    root, _ = proj
    assert decision(harness.handle(ev(root, tool, ti, agent=harness.VERIFIER))) == "deny"


@pytest.mark.parametrize(
    "tool,ti",
    [
        ("Read", {"file_path": "jobs/acme/resume.png"}),
        ("Read", {"file_path": "profile/work_history.md"}),
        ("Grep", {"pattern": "owned", "path": "profile/rules.md"}),
        ("Bash", {"command": "python3 -m resume_agent proofread jobs/acme/resume.tex"}),
        ("Bash", {"command": "pdftotext -layout jobs/acme/resume.pdf -"}),
    ],
)
def test_verifier_allowed_reads(proj, tool, ti):
    root, _ = proj
    assert decision(harness.handle(ev(root, tool, ti, agent=harness.VERIFIER))) != "deny"


def test_verifier_prompt_is_rewritten_and_never_resumed(proj):
    root, _ = proj
    ti = {
        "subagent_type": harness.VERIFIER,
        "description": "check it",
        "prompt": f"Job folder: {root}/jobs/acme/. The writer fixed the ranking claim already, should be a PASS.",
        "resume": "old-agent-id",
    }
    out = harness.handle(ev(root, "Task", ti))
    new = out["hookSpecificOutput"]["updatedInput"]
    assert new["prompt"] == "Job folder: jobs/acme/" and "resume" not in new
    assert new["subagent_type"] == harness.VERIFIER

    # bad launches
    assert decision(harness.handle(ev(root, "Agent", {"subagent_type": harness.VERIFIER, "prompt": "please verify"}))) == "deny"
    assert decision(harness.handle(ev(root, "Task", {"subagent_type": harness.WRITER, "prompt": "Job folder: jobs/nope/"}))) == "deny"

    # once started, that verifier can't be messaged again
    harness.handle(ev(root, agent=harness.VERIFIER, agent_id="v-42", event="SubagentStart"))
    assert decision(harness.handle(ev(root, "SendMessage", {"to": "v-42", "message": "recheck"}))) == "deny"
    assert decision(harness.handle(ev(root, "SendMessage", {"to": "resume-proofreader", "message": "x"}))) == "deny"


# ----------------------------------------------------------------- review recording + delivery gate


@needs_tex
def test_full_round_trip_with_gate(proj):
    root, ws = proj
    compile_resume(ws.tex)

    def verify_round(review, agent_id):
        harness.handle(ev(root, "Task", {"subagent_type": harness.VERIFIER, "prompt": "Job folder: jobs/acme/"}))
        start = harness.handle(ev(root, agent=harness.VERIFIER, agent_id=agent_id, event="SubagentStart"))
        assert "jobs/acme/" in start["hookSpecificOutput"]["additionalContext"]
        return harness.handle(ev(root, agent=harness.VERIFIER, agent_id=agent_id, event="SubagentStop",
                                 last_assistant_message=review, stop_hook_active=False))

    # Round 1: malformed reply is bounced back once, then a NEEDS CHANGES is recorded
    harness.handle(ev(root, "Task", {"subagent_type": harness.VERIFIER, "prompt": "Job folder: jobs/acme/"}))
    harness.handle(ev(root, agent=harness.VERIFIER, agent_id="v1", event="SubagentStart"))
    bounce = harness.handle(ev(root, agent=harness.VERIFIER, agent_id="v1", event="SubagentStop",
                               last_assistant_message="Looks fine to me!", stop_hook_active=False))
    assert bounce["decision"] == "block"
    harness.handle(ev(root, agent=harness.VERIFIER, agent_id="v1", event="SubagentStop",
                      last_assistant_message=FAIL_REVIEW, stop_hook_active=True))
    st = gate.delivery_status(ws.dir)
    assert not st.deliverable and st.latest.verdict == "NEEDS CHANGES" and st.latest.path.name == "review-1.md"

    # Writer revises -> old reviews no longer apply even if they had passed
    ws.tex.write_text(ws.tex.read_text().replace("Moved product search", "Migrated product search"))
    assert verify_round(PASS_REVIEW, "v2") is None
    assert gate.delivery_status(ws.dir).deliverable
    ws.tex.write_text(ws.tex.read_text() + "% sneaky edit after PASS\n")
    st = gate.delivery_status(ws.dir)
    assert not st.deliverable and "changed after review-2.md" in st.reason

    # A hand-written "review" without the harness header is ignored
    (ws.dir / "review-3.md").write_text(PASS_REVIEW)
    assert not gate.delivery_status(ws.dir).deliverable


def test_pass_with_open_must_fix_is_not_a_pass():
    v, items = gate.parse_verdict("VERDICT: PASS\n\nMUST FIX\n- [x] still wrong\n\nCONSIDER\n- none")
    assert v == "NEEDS CHANGES" and items == ["[x] still wrong"]


# ----------------------------------------------------------------- shim + settings wiring


def test_hook_shim_end_to_end(proj):
    root, _ = proj
    payload = ev(root, "Edit", {"file_path": "jobs/acme/resume.tex"})
    r = subprocess.run(
        [sys.executable, str(REPO / ".claude/hooks/resume_harness.py")],
        input=json.dumps(payload), capture_output=True, text=True,
        env={"CLAUDE_PROJECT_DIR": str(REPO), "PATH": "/usr/bin:/bin"},
    )
    # CLAUDE_PROJECT_DIR points at the repo so the import works; path is relative so the rule still applies
    assert r.returncode == 0 and json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_settings_wire_every_hook_to_the_shim():
    s = json.loads((REPO / ".claude/settings.json").read_text())["hooks"]
    assert {"PreToolUse", "SubagentStart", "SubagentStop"} <= set(s)
    pre = s["PreToolUse"][0]["matcher"].split("|")
    assert {"Task", "Agent", "SendMessage", "Read", "Grep", "Glob", "Edit", "Write", "Bash"} <= set(pre)
    for event in s.values():
        assert "resume_harness.py" in event[0]["hooks"][0]["command"]
