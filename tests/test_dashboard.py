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


# ----------------------------------------------------------------- editing API


@pytest.fixture
def live(tmp_path, demo_dirs, monkeypatch):
    import shutil

    out, _ = demo_dirs
    apps_dir = tmp_path / "applications"
    shutil.copytree(out / "applications", apps_dir)
    captured = {}

    class FakeServer(ThreadingHTTPServer):
        def serve_forever(self, *a, **kw):
            captured["srv"] = self
            threading.Thread(target=super().serve_forever, daemon=True).start()

    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", FakeServer)
    dashboard.serve("127.0.0.1", 0, out / "traces", apps_dir)
    srv = captured["srv"]
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    token = json.loads(urllib.request.urlopen(base + "/data.json").read())["token"]
    yield base, token, apps_dir
    srv.shutdown()


def _post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        r = urllib.request.urlopen(req)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_update_via_dashboard_api(live):
    base, token, apps_dir = live
    app_id = sorted(p.name for p in apps_dir.iterdir() if p.is_dir())[0]
    url = f"{base}/api/applications/{app_id}/update"
    good = {"Content-Type": "application/json", "X-Dashboard-Token": token}

    code, out = _post(url, {"status": "interview", "note": "panel booked", "confirmation_id": "C-1",
                            "add_question": {"question": "Start date?", "answer": "January"}}, good)
    assert code == 200 and out["summary"]["status"] == "interview"
    rec = json.loads((apps_dir / app_id / "application.json").read_text())
    assert rec["events"][-1] == {**rec["events"][-1], "status": "interview", "note": "panel booked", "by": "dashboard"}
    assert rec["confirmation_id"] == "C-1" and rec["questions"][-1]["question"] == "Start date?"

    # CSRF / validation
    assert _post(url, {"status": "offer"}, {"Content-Type": "application/json"})[0] == 403          # no token
    assert _post(url, {"status": "offer"}, {**good, "X-Dashboard-Token": "nope"})[0] == 403
    assert _post(url, {"status": "offer"}, {**good, "Origin": "https://evil.example"})[0] == 403  # cross-site
    assert _post(url, {"status": "offer"}, {**good, "Host": "evil.example:80"})[0] == 403         # DNS rebinding
    assert _post(url, {"status": "offer"}, {**good, "Content-Type": "text/plain"})[0] == 415
    assert _post(url, {"status": "hired"}, good)[0] == 400
    assert _post(f"{base}/api/applications/..%2Fx/update", {"status": "offer"}, good)[0] == 404


def test_file_serving_is_confined(live):
    base, _, apps_dir = live
    app_id = sorted(p.name for p in apps_dir.iterdir() if p.is_dir())[0]
    body = urllib.request.urlopen(f"{base}/files/{app_id}/answers.md").read().decode()
    assert body.startswith("# Application questions")
    for bad in (f"/files/{app_id}/application.json", f"/files/{app_id}/..%2F..%2Fetc%2Fpasswd", "/files/../index.md"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + bad)
        assert e.value.code == 404


def test_export_is_read_only(demo_dirs, tmp_path):
    out, _ = demo_dirs
    html = dashboard.export(tmp_path / "d.html", out / "traces", out / "applications").read_text()
    assert '"editable":false' in html and '"token":""' in html
