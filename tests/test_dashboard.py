import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from resume_agent import dashboard, demo, tracing


@pytest.fixture(scope="module")
def demo_dirs(tmp_path_factory):
    out = tmp_path_factory.mktemp("demo")
    res = demo.generate(out, days=30, jobs=20, seed=3)
    return out, res


def test_demo_is_deterministic_and_fictional(tmp_path, demo_dirs):
    out, res = demo_dirs
    again = demo.generate(tmp_path, days=30, jobs=20, seed=3, end=None)
    assert res["spans"] > 100 and res["applications_count"] > 0
    assert again["applications_count"] == res["applications_count"]
    names = {json.loads(line)["name"] for f in (out / "traces").glob("*.jsonl") for line in f.read_text().splitlines()}
    assert {"jd.fetch", "job.create", "proofread", "review.record", "resume.deliver", "application.record"} <= names
    assert (out / "applications" / "index.md").exists()


def test_payload_and_export(demo_dirs, tmp_path):
    out, res = demo_dirs
    p = dashboard.build_payload(out / "traces", out / "applications", title="Demo")
    assert len(p["spans"]) == res["spans"] and len(p["applications"]) == res["applications_count"]
    s = p["spans"][0]
    assert set(s) == {"n", "t", "d", "s", "e", "tr", "id", "p", "m", "a"}
    html = dashboard.export(tmp_path / "d.html", out / "traces", out / "applications", title="Demo").read_text()
    assert "/*__DATA__*/null" not in html and '"title":"Demo"' in html


def test_render_escapes_script_close():
    html = dashboard.render({"title": "x</script><script>alert(1)</script>", "spans": [], "applications": [], "sources": {}})
    assert "</script><script>alert(1)" not in html


def test_real_tracer_output_is_readable(tmp_path):
    tracing.set_tracer(tracing.LocalTracer(tmp_path))
    try:
        with tracing.span("review.record", job="acme", verdict="PASS", round=1, secret_text="x" * 10):
            pass
    finally:
        tracing.set_tracer(None)
    (s,) = dashboard.load_spans(tmp_path)
    assert s["n"] == "review.record" and s["a"] == {"job": "acme", "verdict": "PASS", "round": 1}  # unknown attrs dropped


def test_server_serves_fresh_data(demo_dirs, monkeypatch):
    out, _ = demo_dirs
    captured = {}

    class FakeServer(ThreadingHTTPServer):
        def serve_forever(self, *a, **kw):
            captured["srv"] = self
            threading.Thread(target=super().serve_forever, daemon=True).start()

    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", FakeServer)
    dashboard.serve("127.0.0.1", 0, out / "traces", out / "applications")
    srv = captured["srv"]
    port = srv.server_address[1]
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "Resume agent" in body and '"spans":[' in body
        data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/data.json").read())
        assert data["applications"]
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/../etc/passwd")
    finally:
        srv.shutdown()


def test_dashboard_renders_without_js_errors(demo_dirs, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    import shutil

    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        pytest.skip("no headless browser")
    out, _ = demo_dirs
    f = dashboard.export(tmp_path / "d.html", out / "traces", out / "applications")
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1300, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"file://{f}")
        pg.wait_for_timeout(300)
        for days in ("7", "30", "0"):
            pg.click(f"#range button[data-days='{days}']")
        pg.select_option("#mode", "server")
        pg.locator("#c-funnel .toggle").click()
        tiles = pg.locator("#kpis .tile").count()
        b.close()
    assert not errs and tiles == 6
