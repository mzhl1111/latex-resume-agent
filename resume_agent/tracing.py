"""Pluggable tracing.

Application code only uses the module-level API:

    from resume_agent import tracing

    with tracing.span("proofread", job="acme-ml") as s:
        ...
        s.set(errors=2, ok=False)
        s.event("retry", attempt=2)

    @tracing.traced("jd.fetch")            # sync or async functions
    def fetch_jd(url): ...

Which backend receives the spans is chosen by ``RESUME_TRACER`` (comma-separated
to fan out to several):

    local   (default) JSON lines under RESUME_TRACES_DIR (default ./traces/), one file per day.
            The dashboard reads these.
    otel    OpenTelemetry API spans. If the OpenTelemetry SDK + OTLP exporter are installed and
            OTEL_EXPORTER_OTLP_ENDPOINT is set, a provider is configured automatically; otherwise
            spans go to whatever provider the host process configured.
    none    disable tracing
    pkg.module:ClassName   your own Tracer subclass (constructed with no arguments)

e.g. ``RESUME_TRACER=local,otel`` keeps the local dashboard working while also exporting.

Spans carry metadata only (ids, timings, counts, verdicts, company/role, model, tokens),
never resume text, JD text or application answers.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import importlib
import inspect
import json
import os
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import __version__

SERVICE = "latex-resume-agent"


# --------------------------------------------------------------------------- span handle


class Span:
    """What a ``with tracing.span(...) as s`` block receives."""

    def set(self, **attrs: Any) -> None:  # noqa: D401
        pass

    def event(self, name: str, **attrs: Any) -> None:
        pass

    def set_error(self, exc: BaseException | str) -> None:
        pass


class Tracer:
    """Base class for tracer plugins: implement ``span`` as a context manager yielding a Span."""

    def span(self, name: str, attrs: dict[str, Any]) -> contextlib.AbstractContextManager[Span]:
        return contextlib.nullcontext(Span())

    def flush(self) -> None:
        pass


class NoopTracer(Tracer):
    pass


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in attrs.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v[:500] if isinstance(v, str) else v
        elif isinstance(v, (list, tuple)) and all(isinstance(x, (str, int, float, bool)) for x in v):
            out[k] = list(v)[:50]
        else:
            out[k] = str(v)[:500]
    return out


# --------------------------------------------------------------------------- local JSONL


_ctx: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar("resume_trace_ctx", default=None)


class _LocalSpan(Span):
    def __init__(self, name: str, attrs: dict[str, Any], trace_id: str, parent_id: str | None):
        self.rec: dict[str, Any] = {
            "trace_id": trace_id,
            "span_id": secrets.token_hex(8),
            "parent_id": parent_id,
            "name": name,
            "start": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "attrs": _clean(attrs),
            "events": [],
            "status": "ok",
        }
        self._t0 = time.perf_counter()

    def set(self, **attrs: Any) -> None:
        self.rec["attrs"].update(_clean(attrs))

    def event(self, name: str, **attrs: Any) -> None:
        self.rec["events"].append(
            {"name": name, "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "attrs": _clean(attrs)}
        )

    def set_error(self, exc: BaseException | str) -> None:
        self.rec["status"] = "error"
        self.rec["error"] = (f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc))[:500]


class LocalTracer(Tracer):
    """Appends one JSON object per finished span to ``<dir>/spans-YYYY-MM-DD.jsonl``."""

    def __init__(self, directory: str | Path | None = None):
        from . import paths

        env = os.environ.get("RESUME_TRACES_DIR")
        self.dir = Path(directory) if directory else (Path(env) if env else paths.TRACES_DIR)
        self._lock = threading.Lock()
        self.resource = {
            "service": SERVICE,
            "version": __version__,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "mode": os.environ.get("RESUME_TRACE_MODE", "cli"),
        }

    @contextlib.contextmanager
    def span(self, name: str, attrs: dict[str, Any]) -> Iterator[Span]:
        parent = _ctx.get()
        trace_id = parent[0] if parent else secrets.token_hex(16)
        s = _LocalSpan(name, attrs, trace_id, parent[1] if parent else None)
        token = _ctx.set((trace_id, s.rec["span_id"]))
        try:
            yield s
        except BaseException as exc:
            s.set_error(exc)
            raise
        finally:
            _ctx.reset(token)
            s.rec["duration_ms"] = round((time.perf_counter() - s._t0) * 1000, 2)
            s.rec["resource"] = self.resource
            self._write(s.rec)

    def _write(self, rec: dict) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            day = rec["start"][:10]
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            with self._lock, open(self.dir / f"spans-{day}.jsonl", "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # tracing must never break the pipeline


# --------------------------------------------------------------------------- OpenTelemetry


class _OtelSpan(Span):
    def __init__(self, span: Any):
        self._s = span

    def set(self, **attrs: Any) -> None:
        for k, v in _clean(attrs).items():
            self._s.set_attribute(f"resume.{k}", v)

    def event(self, name: str, **attrs: Any) -> None:
        self._s.add_event(name, {f"resume.{k}": v for k, v in _clean(attrs).items()})

    def set_error(self, exc: BaseException | str) -> None:
        from opentelemetry.trace import Status, StatusCode

        if isinstance(exc, BaseException):
            self._s.record_exception(exc)
        self._s.set_status(Status(StatusCode.ERROR, str(exc)[:500]))


class OtelTracer(Tracer):
    """Adapter onto the OpenTelemetry API. Attribute keys are prefixed with ``resume.``."""

    _configured = False

    def __init__(self, tracer: Any = None):
        try:
            from opentelemetry import trace
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("RESUME_TRACER=otel needs `pip install opentelemetry-api` (see the 'otel' extra)") from exc
        self._maybe_configure_sdk(trace)
        self._tracer = tracer or trace.get_tracer(SERVICE, __version__)

    @classmethod
    def _maybe_configure_sdk(cls, trace: Any) -> None:
        if cls._configured or not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            return
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            return  # API only: the host process is responsible for the provider
        provider = TracerProvider(resource=Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", SERVICE)}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        cls._configured = True

    @contextlib.contextmanager
    def span(self, name: str, attrs: dict[str, Any]) -> Iterator[Span]:
        with self._tracer.start_as_current_span(
            name, attributes={f"resume.{k}": v for k, v in _clean(attrs).items()}, record_exception=False,
            set_status_on_exception=False,
        ) as raw:
            s = _OtelSpan(raw)
            try:
                yield s
            except BaseException as exc:
                s.set_error(exc)
                raise

    def flush(self) -> None:
        try:
            from opentelemetry import trace

            provider = trace.get_tracer_provider()
            if hasattr(provider, "force_flush"):
                provider.force_flush()
        except Exception:
            pass


# --------------------------------------------------------------------------- fan-out


class _MultiSpan(Span):
    def __init__(self, spans: list[Span]):
        self._spans = spans

    def set(self, **attrs: Any) -> None:
        for s in self._spans:
            s.set(**attrs)

    def event(self, name: str, **attrs: Any) -> None:
        for s in self._spans:
            s.event(name, **attrs)

    def set_error(self, exc: BaseException | str) -> None:
        for s in self._spans:
            s.set_error(exc)


class MultiTracer(Tracer):
    def __init__(self, tracers: list[Tracer]):
        self.tracers = tracers

    @contextlib.contextmanager
    def span(self, name: str, attrs: dict[str, Any]) -> Iterator[Span]:
        with contextlib.ExitStack() as stack:
            yield _MultiSpan([stack.enter_context(t.span(name, attrs)) for t in self.tracers])

    def flush(self) -> None:
        for t in self.tracers:
            t.flush()


# --------------------------------------------------------------------------- selection


BUILTIN: dict[str, Callable[[], Tracer]] = {"local": LocalTracer, "otel": OtelTracer, "none": NoopTracer}
_tracer: Tracer | None = None
_tracer_lock = threading.Lock()


def _build(spec: str) -> Tracer:
    parts = [p.strip() for p in spec.split(",") if p.strip()] or ["local"]
    tracers: list[Tracer] = []
    for p in parts:
        if p in BUILTIN:
            tracers.append(BUILTIN[p]())
        elif ":" in p:
            mod, cls = p.split(":", 1)
            obj = getattr(importlib.import_module(mod), cls)
            tracer = obj() if callable(obj) else obj
            if not hasattr(tracer, "span"):
                raise TypeError(f"{p} is not a Tracer (needs a span(name, attrs) context manager)")
            tracers.append(tracer)
        else:
            raise ValueError(f"Unknown RESUME_TRACER entry {p!r}; use local, otel, none or module:Class")
    return tracers[0] if len(tracers) == 1 else MultiTracer(tracers)


def get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        with _tracer_lock:
            if _tracer is None:
                try:
                    _tracer = _build(os.environ.get("RESUME_TRACER", "local"))
                except Exception as exc:  # a bad tracer config must not break resume generation
                    import sys

                    print(f"[resume_agent] tracing disabled: {exc}", file=sys.stderr)
                    _tracer = NoopTracer()
    return _tracer


def set_tracer(tracer: Tracer | None) -> None:
    """Install a tracer programmatically (tests, embedding). ``None`` re-reads RESUME_TRACER."""
    global _tracer
    _tracer = tracer


@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Iterator[Span]:
    with get_tracer().span(name, attrs) as s:
        yield s


def traced(name: str | None = None, **static: Any):
    """Decorator: run the function inside a span named ``name`` (default: module.func)."""

    def deco(fn):
        span_name = name or f"{fn.__module__.rsplit('.', 1)[-1]}.{fn.__name__}"
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def aw(*a, **kw):
                with span(span_name, **static):
                    return await fn(*a, **kw)

            return aw

        @functools.wraps(fn)
        def w(*a, **kw):
            with span(span_name, **static):
                return fn(*a, **kw)

        return w

    return deco


def current_trace_id() -> str | None:
    ctx = _ctx.get()
    return ctx[0] if ctx else None
