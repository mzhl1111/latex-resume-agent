"""Local dashboard over traces/ and applications/.

    python3 -m resume_agent dashboard                 # http://127.0.0.1:8050, re-reads data on every load
    python3 -m resume_agent dashboard --export d.html # one self-contained HTML file (no server, no network)

The page is a single HTML file (``dashboard.html`` next to this module) with the
data embedded as JSON; all aggregation and filtering happens in the browser, so the
exported file keeps working offline.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__, paths

TEMPLATE = Path(__file__).with_name("dashboard.html")
MAX_SPANS = 100_000
SPAN_ATTRS = {
    "job", "company", "role", "source", "chars", "ok", "pages", "errors", "warnings", "checks", "fill_ratio",
    "verdict", "must_fix", "round", "rounds", "model", "input_tokens", "output_tokens", "tool", "decision",
    "reason", "deliverable", "agent_seconds", "status", "application_id", "platform", "verified", "created",
    "method", "action", "state", "is_error", "source_slug", "accepted_open_issues", "bounced",
}


def load_spans(traces_dir: Path) -> list[dict]:
    out: list[dict] = []
    if not traces_dir.exists():
        return out
    for f in sorted(traces_dir.glob("spans-*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            attrs = r.get("attrs") or {}
            role = attrs.get("role")
            out.append(
                {
                    "n": r.get("name", ""),
                    "t": r.get("start", ""),
                    "d": r.get("duration_ms", 0),
                    "s": r.get("status", "ok"),
                    "e": (r.get("error") or "")[:200],
                    "tr": r.get("trace_id", ""),
                    "id": r.get("span_id", ""),
                    "p": r.get("parent_id"),
                    "m": (r.get("resource") or {}).get("mode", ""),
                    "a": {k: v for k, v in attrs.items() if k in SPAN_ATTRS or (k == "role" and role)},
                }
            )
    out.sort(key=lambda s: s["t"])
    return out[-MAX_SPANS:]


def load_applications(apps_dir: Path) -> list[dict]:
    rows = []
    if not apps_dir.exists():
        return rows
    for f in apps_dir.iterdir():
        p = f / "application.json"
        if not p.exists():
            continue
        try:
            r = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        rows.append(
            {
                "id": r.get("id"),
                "t": r.get("submitted_at", ""),
                "status": r.get("status", ""),
                "company": (r.get("job") or {}).get("company", ""),
                "role": (r.get("job") or {}).get("role", ""),
                "platform": (r.get("job") or {}).get("platform", ""),
                "url": (r.get("job") or {}).get("url", ""),
                "slug": (r.get("job") or {}).get("source_slug", ""),
                "verified": ((r.get("resume") or {}).get("verification") or {}).get("deliverable"),
                "cover": bool(r.get("cover_letter")),
                "qa": len(r.get("questions") or []),
                "by": r.get("submitted_by", ""),
                "events": [{"t": e.get("at", ""), "s": e.get("status", "")} for e in r.get("events") or []],
            }
        )
    rows.sort(key=lambda a: a["t"])
    return rows


def build_payload(traces_dir: Path | None = None, apps_dir: Path | None = None, *, title: str | None = None) -> dict:
    traces_dir = traces_dir or paths.TRACES_DIR
    apps_dir = apps_dir or paths.APPLICATIONS_DIR
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": __version__,
        "title": title or "Resume agent",
        "sources": {"traces": str(traces_dir), "applications": str(apps_dir)},
        "spans": load_spans(traces_dir),
        "applications": load_applications(apps_dir),
    }


def render(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")  # keep </script> inside strings from closing the tag
    return TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", data, 1)


def export(out: Path, traces_dir: Path | None = None, apps_dir: Path | None = None, *, title: str | None = None) -> Path:
    out.write_text(render(build_payload(traces_dir, apps_dir, title=title)), encoding="utf-8")
    return out


def serve(host: str = "127.0.0.1", port: int = 8050, traces_dir: Path | None = None, apps_dir: Path | None = None,
          *, open_browser: bool = False) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] not in ("/", "/index.html", "/data.json"):
                self.send_error(404)
                return
            payload = build_payload(traces_dir, apps_dir)
            if self.path.startswith("/data.json"):
                body, ctype = json.dumps(payload).encode(), "application/json"
            else:
                body, ctype = render(payload).encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"Dashboard at {url}  (traces: {traces_dir or paths.TRACES_DIR}, applications: {apps_dir or paths.APPLICATIONS_DIR})")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
