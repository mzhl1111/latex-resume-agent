"""Model calls for the server-side writer and verifier, through LiteLLM.

LiteLLM gives one API over many providers; the model string picks the provider:

    anthropic/claude-sonnet-5-5               Anthropic API        (ANTHROPIC_API_KEY)
    openrouter/<org>/<model>                  OpenRouter           (OPENROUTER_API_KEY)
        e.g. openrouter/anthropic/<slug>, openrouter/openai/<slug>, ... (slugs: openrouter.ai/models)

Per role:

    RESUME_WRITER_MODEL=anthropic/claude-sonnet-5-5
    RESUME_VERIFIER_MODEL=openrouter/openai/<slug>      # a different vendor checks the writer
    RESUME_LLM_MODEL=...                                # default for both roles

A bare name without a provider prefix is treated as an Anthropic model. OpenRouter attribution
headers come from OR_SITE_URL / OR_APP_NAME (defaults to "latex-resume-agent").
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

DEFAULT_MODEL = "anthropic/claude-sonnet-5-5"
ROLES = ("writer", "verifier")
KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


class LLMError(RuntimeError):
    """A model call failed (missing key, provider error, empty answer...)."""


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


def normalize(model: str) -> str:
    model = model.strip()
    return model if "/" in model else f"anthropic/{model}"


def model_for(role: str) -> str:
    legacy = os.environ.get("RESUME_REVIEWER_MODEL") if role == "verifier" else None
    raw = os.environ.get(f"RESUME_{role.upper()}_MODEL") or legacy or os.environ.get("RESUME_LLM_MODEL") or DEFAULT_MODEL
    return normalize(raw)


def provider_of(model: str) -> str:
    return normalize(model).split("/", 1)[0]


def key_present(model: str) -> bool | None:
    """True/False for providers we know the key for; None when LiteLLM resolves credentials itself."""
    env = KEY_ENV.get(provider_of(model))
    return bool(os.environ.get(env)) if env else None


def describe() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for role in ROLES:
        m = model_for(role)
        env = KEY_ENV.get(provider_of(m))
        present = key_present(m)
        out[role] = {"model": m, "provider": provider_of(m), "key_env": env, "ready": present is not False}
    return out


def _litellm_completion() -> Callable[..., Any]:
    try:
        import litellm
    except ImportError as exc:
        raise LLMError('LiteLLM is not installed: pip install ".[server]" (or pip install litellm)') from exc
    litellm.drop_params = True  # ignore params a provider doesn't support instead of failing
    return litellm.completion


def _cost(resp: Any) -> float | None:
    try:
        import litellm

        return float(litellm.completion_cost(completion_response=resp))
    except Exception:  # unknown/new model in LiteLLM's price map, or litellm missing
        return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # some providers return content parts
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return ""


def complete(
    *,
    system: str,
    messages: list[dict],
    model: str,
    images: list[Path] = (),
    max_tokens: int = 8000,
    completion: Callable[..., Any] | None = None,
) -> LLMResult:
    """One chat call. ``messages`` are {"role": "user"|"assistant", "content": str}; ``images`` are PNGs
    attached to the last user message. ``completion`` is injectable for tests (litellm.completion signature)."""
    model = normalize(model)
    provider = provider_of(model)
    if key_present(model) is False:
        raise LLMError(f"{KEY_ENV[provider]} is not set (needed for {model})")
    if provider == "openrouter":
        os.environ.setdefault("OR_APP_NAME", "latex-resume-agent")

    msgs: list[dict] = [{"role": "system", "content": system}] + [dict(m) for m in messages]
    if images:
        parts: list[dict] = [{"type": "text", "text": msgs[-1]["content"]}]
        for p in images:
            b64 = base64.standard_b64encode(Path(p).read_bytes()).decode()
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "format": "image/png"}})
        msgs[-1] = {"role": msgs[-1]["role"], "content": parts}

    call = completion or _litellm_completion()
    try:
        resp = call(model=model, messages=msgs, max_tokens=max_tokens)
    except LLMError:
        raise
    except Exception as exc:  # provider/auth/rate-limit errors surface as LiteLLM exceptions
        raise LLMError(f"{model}: {type(exc).__name__}: {str(exc)[:300]}") from exc

    try:
        text = _content_text(resp.choices[0].message.content)
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise LLMError(f"{model}: unexpected response shape") from exc
    if not text.strip():
        raise LLMError(f"{model}: empty response")
    usage = getattr(resp, "usage", None)
    return LLMResult(
        text=text,
        model=getattr(resp, "model", None) or model,
        provider=provider,
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cost_usd=None if completion else _cost(resp),
    )
