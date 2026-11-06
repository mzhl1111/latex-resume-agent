"""LLM backends for the server-side writer and verifier.

    anthropic-api  Anthropic Messages API with ANTHROPIC_API_KEY (pay-as-you-go)
    claude-code    the `claude` CLI in headless mode (`claude -p`), using whatever account the CLI is
                   logged in with, e.g. a Claude Pro/Max plan
    codex          the `codex` CLI (`codex exec`), using its login, e.g. a ChatGPT Plus/Pro plan

Pick per role, so the verifier can be a different vendor from the writer:

    RESUME_LLM_BACKEND=claude-code            # both roles
    RESUME_WRITER_BACKEND=codex               # override one role
    RESUME_VERIFIER_BACKEND=claude-code
    RESUME_WRITER_MODEL / RESUME_VERIFIER_MODEL   model per role (CLI defaults if unset)

With no setting: ANTHROPIC_API_KEY -> anthropic-api, else a `claude` CLI on PATH -> claude-code,
else `codex` -> codex.

Plan-backed CLIs are for your own use on your own machine: keep RESUME_AGENT_TOKEN set so only your
agents can spend your plan. The subprocess environment has API-key variables removed, so a stray
ANTHROPIC_API_KEY / OPENAI_API_KEY never silently switches a plan backend to API billing.

Isolation: every CLI call runs in a fresh, empty temp directory outside the repo, with no settings,
hooks, MCP servers or session persistence. The writer gets no tools. The verifier can only Read a
copy of the rendered page image. Everything else is inlined in the prompt, so it sees exactly what the
pipeline gives it and never the writer's notes.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

BACKENDS = ("anthropic-api", "claude-code", "codex")
ROLES = ("writer", "verifier")
CLI_TIMEOUT = int(os.environ.get("RESUME_LLM_TIMEOUT", "900"))
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")


class BackendError(RuntimeError):
    """The model call failed (not logged in, quota, timeout, bad output...)."""


@dataclass
class Message:
    role: str  # "user" | "assistant"
    text: str


@dataclass
class LLMResult:
    text: str
    backend: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    raw: dict = field(default_factory=dict, repr=False)


class Backend(Protocol):
    name: str

    def complete(self, *, system: str, messages: list[Message], images: list[Path] = (),
                 model: str | None = None, max_tokens: int = 8000) -> LLMResult: ...


# --------------------------------------------------------------------------- helpers


def _transcript(messages: list[Message]) -> str:
    """Flatten a multi-turn conversation for single-shot CLIs."""
    if len(messages) == 1:
        return messages[0].text
    parts = ["This continues an earlier exchange. Earlier turns, oldest first:"]
    for m in messages[:-1]:
        parts.append(f"<{m.role}_turn>\n{m.text}\n</{m.role}_turn>")
    parts.append("Now respond to this latest message:\n" + messages[-1].text)
    return "\n\n".join(parts)


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _API_KEY_VARS}
    home = env.get("HOME", "")
    extra = [f"{home}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"] if home else []
    env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    return env


def _which(binary: str) -> str | None:
    return shutil.which(binary, path=_clean_env()["PATH"])


def _run(argv: list[str], *, stdin: str, cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, input=stdin, cwd=cwd, env=_clean_env(), capture_output=True, text=True,
                              timeout=CLI_TIMEOUT)
    except FileNotFoundError as exc:
        raise BackendError(f"{argv[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"{Path(argv[0]).name} timed out after {CLI_TIMEOUT}s") from exc


# --------------------------------------------------------------------------- Anthropic API


class AnthropicAPIBackend:
    name = "anthropic-api"
    default_model = "claude-sonnet-5-5"

    def __init__(self, client: Any = None):
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise BackendError("pip install anthropic to use the anthropic-api backend") from exc
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise BackendError("ANTHROPIC_API_KEY is not set")
            client = anthropic.Anthropic()
        self.client = client

    def complete(self, *, system, messages, images=(), model=None, max_tokens=8000) -> LLMResult:
        msgs: list[dict] = [{"role": m.role, "content": m.text} for m in messages]
        if images:
            blocks: list[dict] = [{"type": "text", "text": msgs[-1]["content"]}]
            for p in images:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                           "data": base64.standard_b64encode(Path(p).read_bytes()).decode()}})
            msgs[-1] = {"role": "user", "content": blocks}
        model = model or self.default_model
        resp = self.client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=msgs)
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        usage = getattr(resp, "usage", None)
        return LLMResult(text, self.name, model, getattr(usage, "input_tokens", 0) or 0,
                         getattr(usage, "output_tokens", 0) or 0)


# --------------------------------------------------------------------------- Claude Code CLI (plan login)


class ClaudeCodeBackend:
    name = "claude-code"

    def __init__(self, binary: str | None = None):
        self.binary = binary or os.environ.get("RESUME_CLAUDE_BIN", "claude")

    def complete(self, *, system, messages, images=(), model=None, max_tokens=8000) -> LLMResult:
        with tempfile.TemporaryDirectory(prefix="resume-claude-") as tmp:
            cwd = Path(tmp)
            prompt = _transcript(messages)
            argv = [
                self.binary, "-p", "--output-format", "json", "--system-prompt", system,
                "--no-session-persistence", "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            ]
            if images:
                names = []
                for i, p in enumerate(images):
                    dst = cwd / f"page-{i + 1}.png"
                    shutil.copy(p, dst)
                    names.append(dst.name)
                argv += ["--tools", "Read", "--allowedTools", "Read", "--permission-mode", "dontAsk"]
                prompt += ("\n\nBefore answering, use the Read tool to look at the rendered page image(s) in the "
                           f"current directory: {', '.join(names)}. Do not read any other files.")
            else:
                argv += ["--tools", ""]
            if model:
                argv += ["--model", model]
            proc = _run(argv, stdin=prompt, cwd=cwd)
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise BackendError(f"claude CLI returned no JSON (exit {proc.returncode}): "
                               f"{(proc.stderr or proc.stdout)[:300]}") from exc
        if out.get("is_error") or proc.returncode != 0:
            raise BackendError(f"claude CLI error: {str(out.get('result') or proc.stderr)[:300]} "
                               "(is the CLI logged in? run `claude` once and /login)")
        usage = out.get("usage") or {}
        used_model = next(iter(out.get("modelUsage") or {}), model or "")
        return LLMResult(out.get("result", ""), self.name, used_model,
                         (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
                         + (usage.get("cache_creation_input_tokens") or 0),
                         usage.get("output_tokens") or 0, out.get("total_cost_usd"), out)


# --------------------------------------------------------------------------- Codex CLI (ChatGPT login)


class CodexBackend:
    name = "codex"

    def __init__(self, binary: str | None = None):
        self.binary = binary or os.environ.get("RESUME_CODEX_BIN", "codex")

    def complete(self, *, system, messages, images=(), model=None, max_tokens=8000) -> LLMResult:
        with tempfile.TemporaryDirectory(prefix="resume-codex-") as tmp:
            cwd = Path(tmp)
            argv = [self.binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                    "--sandbox", "read-only", "-C", str(cwd)]
            for i, p in enumerate(images):
                dst = cwd / f"page-{i + 1}.png"
                shutil.copy(p, dst)
                argv += ["--image", str(dst)]
            if model:
                argv += ["--model", model]
            argv.append("-")  # prompt from stdin
            prompt = (f"<instructions>\n{system}\n</instructions>\n\n"
                      "Answer directly from the material below. Do not run commands or edit files.\n\n"
                      + _transcript(messages))
            proc = _run(argv, stdin=prompt, cwd=cwd)
        text, usage, error = "", {}, None
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            item = ev.get("item") or {}
            if kind == "item.completed" and item.get("type") in ("agent_message", "assistant_message"):
                text = item.get("text", "")  # keep the last one: the final answer
            elif kind == "turn.completed":
                usage = ev.get("usage") or {}
            elif kind in ("turn.failed", "error"):
                error = (ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else ev.get("message")
                error = error or json.dumps(ev)[:300]
        if error or proc.returncode != 0 or not text:
            raise BackendError(f"codex error: {error or (proc.stderr or 'no agent message')[:300]} "
                               "(is the CLI logged in? run `codex login`)")
        return LLMResult(text, self.name, model or "", usage.get("input_tokens", 0) or 0,
                         (usage.get("output_tokens", 0) or 0) + (usage.get("reasoning_output_tokens", 0) or 0))


# --------------------------------------------------------------------------- selection


def default_backend_name() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic-api"
    if _which(os.environ.get("RESUME_CLAUDE_BIN", "claude")):
        return "claude-code"
    if _which(os.environ.get("RESUME_CODEX_BIN", "codex")):
        return "codex"
    return "anthropic-api"


def backend_name(role: str) -> str:
    name = (os.environ.get(f"RESUME_{role.upper()}_BACKEND") or os.environ.get("RESUME_LLM_BACKEND")
            or default_backend_name()).strip()
    if name not in BACKENDS:
        raise BackendError(f"Unknown LLM backend {name!r}; use one of {', '.join(BACKENDS)}")
    return name


def role_model(role: str) -> str | None:
    legacy = "RESUME_REVIEWER_MODEL" if role == "verifier" else None
    return os.environ.get(f"RESUME_{role.upper()}_MODEL") or (os.environ.get(legacy) if legacy else None) or None


def get_backend(role: str, *, client: Any = None) -> Backend:
    """Backend for a role. ``client`` (an Anthropic-style client) forces anthropic-api, for tests/embedding."""
    if client is not None:
        return AnthropicAPIBackend(client)
    name = backend_name(role)
    if name == "anthropic-api":
        return AnthropicAPIBackend()
    if name == "claude-code":
        b = ClaudeCodeBackend()
    else:
        b = CodexBackend()
    if not _which(b.binary):
        raise BackendError(f"{name} backend selected but `{b.binary}` is not on PATH")
    return b


def describe() -> dict[str, Any]:
    """What /healthz reports: which backend each role would use, and whether it's usable."""
    out: dict[str, Any] = {}
    for role in ROLES:
        try:
            name = backend_name(role)
        except BackendError as exc:
            out[role] = {"backend": None, "ready": False, "error": str(exc)}
            continue
        if name == "anthropic-api":
            ready = bool(os.environ.get("ANTHROPIC_API_KEY"))
        else:
            ready = bool(_which(os.environ.get("RESUME_CLAUDE_BIN" if name == "claude-code" else "RESUME_CODEX_BIN",
                                               "claude" if name == "claude-code" else "codex")))
        out[role] = {"backend": name, "model": role_model(role), "ready": ready}
    return out
