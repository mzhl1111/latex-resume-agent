"""LiteLLM wrapper: model routing per role, keys, image attachment, usage/errors."""

import types

import pytest

from resume_agent import llm


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("RESUME_LLM_MODEL", "RESUME_WRITER_MODEL", "RESUME_VERIFIER_MODEL", "RESUME_REVIEWER_MODEL",
              "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OR_APP_NAME"):
        monkeypatch.delenv(k, raising=False)


def fake(text="ok", *, raise_exc=None, content=None, record=None):
    def completion(*, model, messages, max_tokens):
        if record is not None:
            record.append({"model": model, "messages": messages, "max_tokens": max_tokens})
        if raise_exc:
            raise raise_exc
        msg = types.SimpleNamespace(content=text if content is None else content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], model=model,
                                     usage=types.SimpleNamespace(prompt_tokens=11, completion_tokens=3))
    return completion


def test_model_routing(monkeypatch):
    assert llm.model_for("writer") == "anthropic/claude-sonnet-5-5"
    monkeypatch.setenv("RESUME_LLM_MODEL", "claude-opus-5-5")
    assert llm.model_for("verifier") == "anthropic/claude-opus-5-5"  # bare name -> anthropic/
    monkeypatch.setenv("RESUME_REVIEWER_MODEL", "openrouter/openai/gpt-x")  # legacy name still works
    assert llm.model_for("verifier") == "openrouter/openai/gpt-x"
    monkeypatch.setenv("RESUME_VERIFIER_MODEL", "openrouter/google/gemini-x")
    assert llm.model_for("verifier") == "openrouter/google/gemini-x" and llm.provider_of("openrouter/google/gemini-x") == "openrouter"


def test_describe_reports_missing_keys(monkeypatch):
    monkeypatch.setenv("RESUME_VERIFIER_MODEL", "openrouter/openai/gpt-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    d = llm.describe()
    assert d["writer"] == {"model": "anthropic/claude-sonnet-5-5", "provider": "anthropic", "key_env": "ANTHROPIC_API_KEY", "ready": True}
    assert d["verifier"]["key_env"] == "OPENROUTER_API_KEY" and d["verifier"]["ready"] is False


def test_complete_builds_openai_style_messages_with_image(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    png = tmp_path / "p.png"
    png.write_bytes(b"\x89PNG")
    calls = []
    r = llm.complete(system="SYS", messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "a"},
                                             {"role": "user", "content": "look"}],
                     model="openrouter/openai/gpt-x", images=[png], max_tokens=50, completion=fake("PASS", record=calls))
    (c,) = calls
    assert c["model"] == "openrouter/openai/gpt-x" and c["max_tokens"] == 50
    assert [m["role"] for m in c["messages"]] == ["system", "user", "assistant", "user"]
    last = c["messages"][-1]["content"]
    assert last[0] == {"type": "text", "text": "look"}
    assert last[1]["image_url"]["url"] == "data:image/png;base64,iVBORw==" and last[1]["image_url"]["format"] == "image/png"
    assert (r.text, r.provider, r.input_tokens, r.output_tokens) == ("PASS", "openrouter", 11, 3)
    import os
    assert os.environ["OR_APP_NAME"] == "latex-resume-agent"


def test_errors_are_wrapped(monkeypatch):
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY is not set"):
        llm.complete(system="s", messages=[{"role": "user", "content": "x"}], model="anthropic/m", completion=fake())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with pytest.raises(llm.LLMError, match="RateLimitError: slow down"):
        llm.complete(system="s", messages=[{"role": "user", "content": "x"}], model="anthropic/m",
                     completion=fake(raise_exc=type("RateLimitError", (Exception,), {})("slow down")))
    with pytest.raises(llm.LLMError, match="empty response"):
        llm.complete(system="s", messages=[{"role": "user", "content": "x"}], model="anthropic/m", completion=fake(""))
    r = llm.complete(system="s", messages=[{"role": "user", "content": "x"}], model="anthropic/m",
                     completion=fake(content=[{"type": "text", "text": "part1 "}, {"type": "text", "text": "part2"}]))
    assert r.text == "part1 part2"


def test_missing_litellm_is_a_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_litellm(name, *a, **kw):
        if name == "litellm":
            raise ImportError("no litellm")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_litellm)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with pytest.raises(llm.LLMError, match="pip install"):
        llm.complete(system="s", messages=[{"role": "user", "content": "x"}], model="anthropic/m")


def test_llm_check_and_health(monkeypatch, capsys):
    from starlette.testclient import TestClient

    from resume_agent.__main__ import main
    from resume_agent.server import create_app

    monkeypatch.setenv("RESUME_VERIFIER_MODEL", "openrouter/openai/gpt-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert main(["llm-check", "--no-ping"]) == 1  # OpenRouter key missing
    out = capsys.readouterr().out
    assert "writer    anthropic/claude-sonnet-5-5  key ANTHROPIC_API_KEY: set" in out
    assert "verifier  openrouter/openai/gpt-x  key OPENROUTER_API_KEY: MISSING" in out
    monkeypatch.delenv("RESUME_REQUIRE_TOKEN", raising=False)
    body = TestClient(create_app(token="")).get("/healthz").json()
    assert body["llm"]["verifier"]["provider"] == "openrouter" and body["generate_available"] is False
