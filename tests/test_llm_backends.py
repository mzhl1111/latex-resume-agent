"""Plan-backed LLM backends, tested against fake `claude` / `codex` executables that speak each CLI's
real output format (claude -p --output-format json; codex exec --json JSONL events)."""

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from resume_agent import llm_backends as lb
from resume_agent import paths
from resume_agent.compile import check_dependencies

needs_tex = pytest.mark.skipif(bool(check_dependencies()), reason="pdflatex/poppler not installed")
REPO = Path(__file__).resolve().parents[1]

FAKE = r'''#!PYTHON
import json, os, sys
from pathlib import Path
kind = Path(sys.argv[0]).name
stdin = sys.stdin.read()
log = Path(os.environ["FAKE_LOG"])
rec = {"argv": sys.argv[1:], "cwd": os.getcwd(), "files": sorted(os.listdir(".")), "stdin": stdin,
       "api_keys": [k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY") if k in os.environ]}
with log.open("a") as f:
    f.write(json.dumps(rec) + "\n")
argv = sys.argv[1:]
system = argv[argv.index("--system-prompt") + 1] if "--system-prompt" in argv else stdin
mode = os.environ.get("FAKE_MODE", "ok")
if "independent resume verifier" in system:
    answer = "VERDICT: PASS\n\nMUST FIX\n- none\n\nCONSIDER\n- none"
elif "<base_template>" in stdin:
    tex = Path(os.environ["FAKE_TEX"]).read_text()
    answer = "<analysis>\n# Analysis WRITER-SECRET-NOTES\n</analysis>\n<resume_tex>\n" + tex + "\n</resume_tex>"
else:
    answer = "PONG"
if kind == "claude":
    if mode == "logged_out":
        print(json.dumps({"type": "result", "is_error": True, "result": "Invalid API key · Please run /login"}))
    else:
        print(json.dumps({"type": "result", "is_error": False, "result": answer, "total_cost_usd": 0,
                          "usage": {"input_tokens": 1200, "cache_read_input_tokens": 300, "output_tokens": 450},
                          "modelUsage": {"claude-sonnet-plan": {}}}))
else:
    print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
    if mode == "logged_out":
        print(json.dumps({"type": "turn.failed", "error": {"message": "401 Unauthorized: run codex login"}}))
        sys.exit(1)
    print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "thinking"}}))
    print(json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": answer}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 0,
                                                          "output_tokens": 300, "reasoning_output_tokens": 50}}))
'''


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("claude", "codex"):
        f = bindir / name
        f.write_text(FAKE.replace("#!PYTHON", f"#!{sys.executable}"))
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_TEX", str(REPO / "examples/templates/resume_base.tex"))
    monkeypatch.setenv("RESUME_CLAUDE_BIN", str(bindir / "claude"))
    monkeypatch.setenv("RESUME_CODEX_BIN", str(bindir / "codex"))
    for k in ("RESUME_LLM_BACKEND", "RESUME_WRITER_BACKEND", "RESUME_VERIFIER_BACKEND", "RESUME_WRITER_MODEL",
              "RESUME_VERIFIER_MODEL", "RESUME_REVIEWER_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-reach-the-cli")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-reach-the-cli")

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return calls


def test_claude_code_backend_writer_call(fakes):
    r = lb.ClaudeCodeBackend().complete(system="SYS", messages=[lb.Message("user", "hello")], model="sonnet")
    assert (r.text, r.backend, r.model, r.input_tokens, r.output_tokens) == ("PONG", "claude-code", "claude-sonnet-plan", 1500, 450)
    (c,) = fakes()
    a = c["argv"]
    assert a[:3] == ["-p", "--output-format", "json"] and a[a.index("--system-prompt") + 1] == "SYS"
    assert a[a.index("--tools") + 1] == "" and "--no-session-persistence" in a and "--strict-mcp-config" in a
    assert a[a.index("--setting-sources") + 1] == "" and a[a.index("--model") + 1] == "sonnet"
    assert c["stdin"] == "hello"
    assert c["api_keys"] == [], "API keys must be stripped so the CLI uses the plan login"
    assert not c["cwd"].startswith(str(REPO)) and c["files"] == [], "runs in an empty dir outside the repo"


def test_claude_code_backend_verifier_sees_only_the_image(fakes, tmp_path):
    png = tmp_path / "resume.png"
    png.write_bytes(b"\x89PNG fake")
    lb.ClaudeCodeBackend().complete(system="independent resume verifier", messages=[lb.Message("user", "check")],
                                    images=[png])
    (c,) = fakes()
    a = c["argv"]
    assert a[a.index("--tools") + 1] == "Read" and a[a.index("--permission-mode") + 1] == "dontAsk"
    assert c["files"] == ["page-1.png"] and "page-1.png" in c["stdin"]


def test_codex_backend(fakes, tmp_path):
    png = tmp_path / "resume.png"
    png.write_bytes(b"\x89PNG fake")
    r = lb.CodexBackend().complete(system="SYS-RULES", messages=[lb.Message("user", "hi"), lb.Message("assistant", "draft"),
                                                                 lb.Message("user", "revise")], images=[png], model="gpt-5-codex")
    assert (r.text, r.backend, r.input_tokens, r.output_tokens) == ("PONG", "codex", 900, 350)
    (c,) = fakes()
    a = c["argv"]
    assert a[0] == "exec" and {"--json", "--ephemeral", "--skip-git-repo-check"} <= set(a)
    assert a[a.index("--sandbox") + 1] == "read-only" and a[-1] == "-" and a[a.index("--model") + 1] == "gpt-5-codex"
    assert Path(a[a.index("--image") + 1]).name == "page-1.png" and Path(a[a.index("-C") + 1]) == Path(c["cwd"])
    assert c["stdin"].startswith("<instructions>\nSYS-RULES") and "<assistant_turn>\ndraft" in c["stdin"]
    assert c["api_keys"] == []


@pytest.mark.parametrize("backend,hint", [(lb.ClaudeCodeBackend, "/login"), (lb.CodexBackend, "codex login")])
def test_logged_out_cli_raises_with_hint(fakes, monkeypatch, backend, hint):
    monkeypatch.setenv("FAKE_MODE", "logged_out")
    with pytest.raises(lb.BackendError, match=hint):
        backend().complete(system="s", messages=[lb.Message("user", "x")])


def test_selection(fakes, monkeypatch):
    assert lb.backend_name("writer") == "anthropic-api"  # API key present -> API by default
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert lb.backend_name("writer") == "claude-code"  # else the claude CLI
    monkeypatch.setenv("RESUME_LLM_BACKEND", "codex")
    monkeypatch.setenv("RESUME_VERIFIER_BACKEND", "claude-code")
    monkeypatch.setenv("RESUME_REVIEWER_MODEL", "opus")  # legacy name still honoured
    assert (lb.backend_name("writer"), lb.backend_name("verifier")) == ("codex", "claude-code")
    assert isinstance(lb.get_backend("writer"), lb.CodexBackend) and lb.role_model("verifier") == "opus"
    d = lb.describe()
    assert d["writer"] == {"backend": "codex", "model": None, "ready": True}
    monkeypatch.setenv("RESUME_LLM_BACKEND", "gemini")
    with pytest.raises(lb.BackendError, match="Unknown"):
        lb.get_backend("writer")
    monkeypatch.setenv("RESUME_LLM_BACKEND", "codex")
    monkeypatch.setenv("RESUME_CODEX_BIN", "/nonexistent/codex")
    with pytest.raises(lb.BackendError, match="not on PATH"):
        lb.get_backend("writer")


@needs_tex
def test_pipeline_on_two_plans_keeps_verifier_isolated(fakes, tmp_path, monkeypatch):
    """Writer on Codex (ChatGPT plan), verifier on Claude Code (Claude plan): cross-vendor verification."""
    from resume_agent.llm_pipeline import generate_resume

    monkeypatch.setattr(paths, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setenv("RESUME_WRITER_BACKEND", "codex")
    monkeypatch.setenv("RESUME_VERIFIER_BACKEND", "claude-code")
    res = generate_resume(jd_text="# ML Engineer at Acme\n\nBuild search. " * 10, slug="acme-plans")
    assert res.ok and res.verdict == "PASS", res.log
    assert res.log[0].startswith("Writer: codex") and "verifier: claude-code" in res.log[0]

    writer_call, verifier_call = fakes()
    assert writer_call["argv"][0] == "exec" and verifier_call["argv"][0] == "-p"
    assert "WRITER-SECRET-NOTES" not in verifier_call["stdin"], "verifier must not see the writer's analysis"
    assert verifier_call["files"] == ["page-1.png"]
    review = (tmp_path / "jobs/acme-plans/review-1.md").read_text()
    assert "verdict=PASS" in review and "claude-code" in review


def test_llm_check_cli(fakes, monkeypatch, capsys):
    from resume_agent.__main__ import main

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("RESUME_VERIFIER_BACKEND", "codex")
    assert main(["llm-check"]) == 0
    out = capsys.readouterr().out
    assert "writer    backend=claude-code" in out and "verifier  backend=codex" in out and out.count("ping ok") == 2
    monkeypatch.setenv("FAKE_MODE", "logged_out")
    assert main(["llm-check", "--role", "writer"]) == 1


def test_health_reports_backends(fakes, monkeypatch):
    from starlette.testclient import TestClient

    from resume_agent.server import create_app

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("RESUME_REQUIRE_TOKEN", raising=False)
    body = TestClient(create_app(token="")).get("/healthz").json()
    assert body["llm"]["writer"]["backend"] == "claude-code" and body["generate_available"] is True
