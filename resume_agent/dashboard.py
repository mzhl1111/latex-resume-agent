"""Local dashboard over traces/ and applications/.

    python3 -m resume_agent dashboard                 # http://127.0.0.1:8050, re-reads data on every load
    python3 -m resume_agent dashboard --export d.html # one self-contained HTML file (no server, no network)

The page is a single HTML file (``dashboard.html`` next to this module) with the
data embedded as JSON; all aggregation and filtering happens in the browser, so the
exported file keeps working offline.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__, paths
from . import applications as apps_mod

TEMPLATE = Path(__file__).with_name("dashboard.html")
MAX_SPANS = 100_000
SERVABLE = {".pdf": "application/pdf", ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
MAX_BODY = 64 * 1024
SPAN_ATTRS = {
    "job", "company", "role", "source", "chars", "ok", "pages", "errors", "warnings", "checks", "fill_ratio",
    "verdict", "must_fix", "round", "rounds", "model", "input_tokens", "output_tokens", "tool", "decision",
    "reason", "deliverable", "agent_seconds", "status", "application_id", "platform", "verified", "created",
    "method", "action", "state", "is_error", "source_slug", "accepted_open_issues", "bounced", "provider", "cost_usd",
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
                "location": (r.get("job") or {}).get("location", ""),
                "confirmation_id": r.get("confirmation_id", ""),
                "notes": r.get("notes", ""),
                "verification_reason": ((r.get("resume") or {}).get("verification") or {}).get("reason", ""),
                "matches_generated": (r.get("resume") or {}).get("matches_generated"),
                "cover_file": (r.get("cover_letter") or {}).get("file"),
                "questions": [{"q": q.get("question", ""), "a": q.get("answer", "")} for q in r.get("questions") or []],
                "files": sorted(p.name for p in f.iterdir() if p.is_file() and p.suffix.lower() in SERVABLE),
                "events": [{"t": e.get("at", ""), "s": e.get("status", ""), "note": e.get("note", ""), "by": e.get("by", "")}
                           for e in r.get("events") or []],
            }
        )
    rows.sort(key=lambda a: a["t"])
    return rows


def build_payload(traces_dir: Path | None = None, apps_dir: Path | None = None, *, title: str | None = None,
                  editable: bool = False, token: str = "") -> dict:
    traces_dir = traces_dir or paths.TRACES_DIR
    apps_dir = apps_dir or paths.APPLICATIONS_DIR
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": __version__,
        "title": title or "Resume agent",
        "sources": {"traces": str(traces_dir), "applications": str(apps_dir)},
        "spans": load_spans(traces_dir),
        "applications": load_applications(apps_dir),
        "statuses": list(apps_mod.STATUSES),
        # Editing only works against the local server; an exported HTML file is read-only.
        "editable": editable,
        "token": token,
    }


def render(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")  # keep </script> inside strings from closing the tag
    return TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", data, 1)


def export(out: Path, traces_dir: Path | None = None, apps_dir: Path | None = None, *, title: str | None = None) -> Path:
    out.write_text(render(build_payload(traces_dir, apps_dir, title=title)), encoding="utf-8")
    return out


def serve(host: str = "127.0.0.1", port: int = 8050, traces_dir: Path | None = None, apps_dir: Path | None = None,
          *, open_browser: bool = False, allow_hosts: list[str] | None = None, read_only: bool = False) -> None:
    """Serve the dashboard. Unless read_only, it can update application status/notes through
    ``applications.update_application`` (same code path as the CLI, MCP and A2A)."""
    token = secrets.token_urlsafe(24)
    apps_root = (apps_dir or paths.APPLICATIONS_DIR)
    allowed = {"127.0.0.1", "localhost", "[::1]"} | set(allow_hosts or [])

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in allowed

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._json(403, {"error": "unexpected Host header"})
            path = self.path.split("?")[0]
            if path in ("/", "/index.html", "/data.json"):
                payload = build_payload(traces_dir, apps_dir, editable=not read_only, token=token)
                if path == "/data.json":
                    return self._json(200, payload)
                return self._send(200, render(payload).encode(), "text/html; charset=utf-8")
            m = re.fullmatch(r"/files/([A-Za-z0-9][A-Za-z0-9._-]*)/((?:attachments/)?[A-Za-z0-9][A-Za-z0-9._-]*)", path)
            if m:
                try:
                    data, mime = apps_mod.read_file(m.group(1), m.group(2), apps_dir=apps_dir)
                except apps_mod.ApplicationError:
                    return self._json(404, {"error": "not found"})
                if mime not in SERVABLE.values() and not mime.startswith("text/markdown"):
                    # only types a browser can show safely; others download
                    return self._send(200, data, "application/octet-stream",
                                      {"Content-Disposition": f'attachment; filename="{Path(m.group(2)).name}"'})
                ctype = "text/plain; charset=utf-8" if mime.startswith("text/") else mime
                return self._send(200, data, ctype, {"Content-Disposition": f'inline; filename="{Path(m.group(2)).name}"'})
            return self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            # CSRF: localhost Host, same-origin (if the browser sends Origin), per-process token, JSON body.
            if read_only:
                return self._json(403, {"error": "dashboard is read-only"})
            if not self._host_ok():
                return self._json(403, {"error": "unexpected Host header"})
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host')}":
                return self._json(403, {"error": "cross-origin request refused"})
            if not hmac.compare_digest(self.headers.get("X-Dashboard-Token", ""), token):
                return self._json(403, {"error": "missing or bad dashboard token (reload the page)"})
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(415, {"error": "JSON body required"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "body too large"})
            m = re.fullmatch(r"/api/applications/([A-Za-z0-9][A-Za-z0-9._-]*)/update", self.path.split("?")[0])
            if not m:
                return self._json(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be an object")
                qa = body.get("add_question")
                rec = apps_mod.update_application(
                    m.group(1),
                    status=body.get("status") or None,
                    note=(body.get("note") or "").strip() or None,
                    at=body.get("at") or None,
                    by="dashboard",
                    confirmation_id=(body.get("confirmation_id") or "").strip() or None,
                    add_questions=[qa] if isinstance(qa, dict) and str(qa.get("question", "")).strip() else None,
                    apps_dir=apps_dir,
                )
            except (apps_mod.ApplicationError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, {"ok": True, "summary": apps_mod.summary(rec)})

        def log_message(self, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{httpd.server_address[1]}/"
    print(f"Dashboard at {url}  (traces: {traces_dir or paths.TRACES_DIR}, applications: {apps_root})"
          + ("  [read-only]" if read_only else ""))
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
