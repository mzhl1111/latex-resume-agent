import contextlib
import json
import sys
import types

import pytest

from resume_agent import tracing


@pytest.fixture
def local(tmp_path):
    t = tracing.LocalTracer(tmp_path)
    tracing.set_tracer(t)
    yield tmp_path
    tracing.set_tracer(None)


def spans(d):
    return [json.loads(line) for f in sorted(d.glob("spans-*.jsonl")) for line in f.read_text().splitlines()]


def test_local_nesting_attrs_events_errors(local):
    with tracing.span("outer", job="acme") as s:
        s.set(rounds=2, checks=["a", "b"], obj={"x": 1}, skip=None)
        s.event("retry", attempt=2)
        with tracing.span("inner"):
            pass
        with pytest.raises(ValueError):
            with tracing.span("boom"):
                raise ValueError("bad")
    out = {r["name"]: r for r in spans(local)}
    assert out["inner"]["parent_id"] == out["outer"]["span_id"] == out["boom"]["parent_id"]
    assert len({r["trace_id"] for r in out.values()}) == 1
    assert out["outer"]["attrs"] == {"job": "acme", "rounds": 2, "checks": ["a", "b"], "obj": "{'x': 1}"}
    assert out["outer"]["events"][0]["name"] == "retry"
    assert out["boom"]["status"] == "error" and "ValueError: bad" in out["boom"]["error"]
    assert out["outer"]["duration_ms"] >= 0 and out["outer"]["resource"]["service"] == tracing.SERVICE


def test_traced_decorator_sync_and_async(local):
    import anyio

    @tracing.traced("sync.fn", kind="s")
    def f(x):
        return x + 1

    @tracing.traced()
    async def g():
        with tracing.span("child"):
            return 3

    assert f(1) == 2 and anyio.run(g) == 3
    names = [r["name"] for r in spans(local)]
    assert "sync.fn" in names and "test_tracing.g" in names and "child" in names


class RecordingTracer(tracing.Tracer):
    seen: list = []

    @contextlib.contextmanager
    def span(self, name, attrs):
        RecordingTracer.seen.append(name)
        yield tracing.Span()


def test_selection_builtin_custom_multi_and_bad(monkeypatch, tmp_path):
    mod = types.ModuleType("my_tracer_plugin")
    mod.RecordingTracer = RecordingTracer
    monkeypatch.setitem(sys.modules, "my_tracer_plugin", mod)
    monkeypatch.setenv("RESUME_TRACES_DIR", str(tmp_path))

    for spec, cls in [("none", tracing.NoopTracer), ("local", tracing.LocalTracer),
                      ("my_tracer_plugin:RecordingTracer", RecordingTracer)]:
        monkeypatch.setenv("RESUME_TRACER", spec)
        tracing.set_tracer(None)
        assert isinstance(tracing.get_tracer(), cls)

    monkeypatch.setenv("RESUME_TRACER", "local,my_tracer_plugin:RecordingTracer")
    tracing.set_tracer(None)
    with tracing.span("fanout") as s:
        s.set(a=1)
    assert isinstance(tracing.get_tracer(), tracing.MultiTracer)
    assert "fanout" in RecordingTracer.seen and spans(tmp_path)[0]["name"] == "fanout"

    monkeypatch.setenv("RESUME_TRACER", "definitely-not-a-tracer")
    tracing.set_tracer(None)
    assert isinstance(tracing.get_tracer(), tracing.NoopTracer)  # bad config never breaks the pipeline
    tracing.set_tracer(None)


def test_otel_adapter_maps_attrs_events_and_errors():
    pytest.importorskip("opentelemetry")

    class FakeSpan:
        def __init__(self, name, attributes):
            self.name, self.attrs, self.events, self.status, self.exc = name, dict(attributes), [], None, None

        def set_attribute(self, k, v):
            self.attrs[k] = v

        def add_event(self, name, attrs):
            self.events.append((name, attrs))

        def record_exception(self, exc):
            self.exc = exc

        def set_status(self, status):
            self.status = status

    class FakeOtel:
        made = []

        @contextlib.contextmanager
        def start_as_current_span(self, name, attributes, **_):
            s = FakeSpan(name, attributes)
            FakeOtel.made.append(s)
            yield s

    tracing.set_tracer(tracing.OtelTracer(tracer=FakeOtel()))
    try:
        with tracing.span("llm.call", model="m") as s:
            s.set(input_tokens=10)
            s.event("retry", n=1)
        with pytest.raises(RuntimeError):
            with tracing.span("fail"):
                raise RuntimeError("x")
    finally:
        tracing.set_tracer(None)
    ok, bad = FakeOtel.made
    assert ok.attrs == {"resume.model": "m", "resume.input_tokens": 10} and ok.events == [("retry", {"resume.n": 1})]
    assert isinstance(bad.exc, RuntimeError) and bad.status.status_code.name == "ERROR"


def test_tracing_failure_never_breaks_work(tmp_path):
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x")
    tracing.set_tracer(tracing.LocalTracer(blocked / "sub"))
    try:
        with tracing.span("still-works"):
            value = 42
    finally:
        tracing.set_tracer(None)
    assert value == 42
