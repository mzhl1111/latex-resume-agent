#!/usr/bin/env python3
"""Render the deploy templates (stdlib only, runs with the system python3).

    render.py plists  <out_dir> --repo DIR --log-dir DIR --path PATH
    render.py tunnel  <out_file> --tunnel-id ID --credentials FILE --hostname HOST --port N

Values are XML-escaped for plists and validated for the tunnel config, so paths with
spaces or odd characters can't break either file.
"""

from __future__ import annotations

import argparse
import plistlib
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
PLISTS = ("server", "tunnel", "dashboard")


def render_plists(out_dir: Path, repo: str, log_dir: str, path: str, only: list[str] | None = None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in only or PLISTS:
        src = (HERE / "launchd" / f"com.resume-agent.{name}.plist.template").read_text()
        text = src.replace("__REPO_DIR__", escape(repo)).replace("__LOG_DIR__", escape(log_dir)).replace("__PATH__", escape(path))
        plistlib.loads(text.encode())  # fail loudly if the result is not a valid plist
        dst = out_dir / f"com.resume-agent.{name}.plist"
        dst.write_text(text)
        written.append(dst)
    return written


def render_tunnel(out_file: Path, tunnel_id: str, credentials: str, hostname: str, port: int) -> Path:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", tunnel_id):
        sys.exit(f"tunnel id must be a UUID, got {tunnel_id!r}")
    if not re.fullmatch(r"(?=.{1,253}$)([a-zA-Z0-9-]{1,63}\.)+[a-zA-Z]{2,63}", hostname):
        sys.exit(f"TUNNEL_HOSTNAME must be a hostname like resume.example.com, got {hostname!r}")
    if not 1 <= port <= 65535:
        sys.exit(f"bad port {port}")
    if any(c in credentials for c in "\n\"'"):
        sys.exit("credentials path contains unsupported characters")
    text = (HERE / "cloudflared" / "config.example.yml").read_text()
    text = (text.replace("__TUNNEL_ID__", tunnel_id).replace("__CREDENTIALS_FILE__", f'"{credentials}"')
            .replace("__TUNNEL_HOSTNAME__", hostname).replace("__RESUME_PORT__", str(port)))
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(text)
    out_file.chmod(0o600)
    return out_file


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plists")
    p.add_argument("out_dir")
    p.add_argument("--repo", required=True)
    p.add_argument("--log-dir", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--only", action="append", choices=PLISTS)
    t = sub.add_parser("tunnel")
    t.add_argument("out_file")
    t.add_argument("--tunnel-id", required=True)
    t.add_argument("--credentials", required=True)
    t.add_argument("--hostname", required=True)
    t.add_argument("--port", type=int, default=8080)
    a = ap.parse_args(argv)
    if a.cmd == "plists":
        for f in render_plists(Path(a.out_dir), a.repo, a.log_dir, a.path, a.only):
            print(f)
    else:
        print(render_tunnel(Path(a.out_file), a.tunnel_id, a.credentials, a.hostname, a.port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
