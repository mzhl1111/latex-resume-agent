import importlib.util
import plistlib
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy" / "macos"
UUID = "2f6a1c9e-1b2d-4c3e-9f80-1234567890ab"

spec = importlib.util.spec_from_file_location("render", DEPLOY / "render.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)


@pytest.mark.parametrize("script", ["resumectl", "run.sh"])
def test_scripts_parse_and_avoid_bash4(script):
    path = DEPLOY / script
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    src = path.read_text()
    assert "mapfile" not in src and "declare -A" not in src  # macOS ships bash 3.2
    assert path.stat().st_mode & 0o111, "must be executable"


def test_plists_render_valid_with_awkward_paths(tmp_path):
    repo = "/Users/jane doe/code/resume & co"
    out = render.render_plists(tmp_path, repo, "/Users/jane doe/Library/Logs/resume-agent", "/opt/homebrew/bin:/Library/TeX/texbin:/usr/bin")
    assert {p.name for p in out} == {f"com.resume-agent.{n}.plist" for n in ("server", "tunnel", "dashboard")}
    for p in out:
        d = plistlib.loads(p.read_bytes())
        name = p.name.split(".")[2]
        assert d["Label"] == f"com.resume-agent.{name}"
        assert d["ProgramArguments"] == ["/bin/bash", f"{repo}/deploy/macos/run.sh", name]
        assert d["WorkingDirectory"] == repo and d["KeepAlive"] is True and d["RunAtLoad"] is True
        assert "/Library/TeX/texbin" in d["EnvironmentVariables"]["PATH"]
        assert "TOKEN" not in p.read_text()  # secrets never land in plists


def _rules(tmp_path):
    out = render.render_tunnel(tmp_path / "cf.yml", UUID, "/Users/j/.cloudflared/x.json", "resume.example.com", 8080)
    cfg = yaml.safe_load(out.read_text())
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    return cfg


def test_tunnel_config_only_routes_agent_endpoints(tmp_path):
    cfg = _rules(tmp_path)
    assert cfg["tunnel"] == UUID and cfg["credentials-file"].endswith("x.json")
    rules = cfg["ingress"]
    assert rules[-1] == {"service": "http_status:404"}, "catch-all must be a 404"
    assert all(r["service"] in ("http://127.0.0.1:8080", "http_status:404") for r in rules)
    assert "8050" not in yaml.safe_dump(cfg)  # the dashboard port is never a tunnel target

    def routed(path):  # cloudflared uses RE2; these patterns behave the same in Python's re
        for r in rules[:-1]:
            if r.get("hostname") == "resume.example.com" and re.search(r["path"], path):
                return True
        return False

    for p in ("/mcp", "/mcp/", "/a2a", "/healthz", "/.well-known/agent-card.json", "/.well-known/agent.json"):
        assert routed(p), p
    for p in ("/", "/index.html", "/data.json", "/files/app/resume.pdf", "/api/applications/x/update",
              "/mcpx", "/a2a/../files/x", "/.well-known/agent-card.jsonx", "/healthz/extra"):
        assert not routed(p), p


@pytest.mark.parametrize("kw,err", [
    ({"tunnel_id": "not-a-uuid"}, "UUID"),
    ({"hostname": "resume example.com"}, "hostname"),
    ({"hostname": "localhost"}, "hostname"),
    ({"port": 0}, "port"),
])
def test_tunnel_render_validates(tmp_path, kw, err):
    args = {"tunnel_id": UUID, "credentials": "/c.json", "hostname": "resume.example.com", "port": 8080, **kw}
    with pytest.raises(SystemExit, match=err):
        render.render_tunnel(tmp_path / "cf.yml", **args)


def test_resumectl_render_and_mac_guard(tmp_path):
    r = subprocess.run([str(DEPLOY / "resumectl"), "render", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert len(list((tmp_path / "LaunchAgents").glob("*.plist"))) == 3
    r = subprocess.run([str(DEPLOY / "resumectl"), "install"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode != 0 and "macOS" in r.stderr
    usage = subprocess.run([str(DEPLOY / "resumectl")], capture_output=True, text=True)
    assert "tunnel-setup" in usage.stdout


def test_env_example_has_no_values_for_secrets():
    env = dict(line.split("=", 1) for line in (DEPLOY / "env.example").read_text().splitlines()
               if line and not line.startswith("#"))
    for key in ("RESUME_AGENT_TOKEN", "ANTHROPIC_API_KEY", "TUNNEL_TOKEN", "TUNNEL_HOSTNAME"):
        assert env[key] == "", key
    out = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "deploy/macos/.env"])
    assert out.returncode == 0, "deploy/macos/.env must be gitignored"


def test_server_refuses_public_start_without_token(monkeypatch):
    from resume_agent.server import InsecureConfig, create_app

    monkeypatch.setenv("RESUME_REQUIRE_TOKEN", "1")
    monkeypatch.delenv("RESUME_AGENT_TOKEN", raising=False)
    with pytest.raises(InsecureConfig):
        create_app()
    with pytest.raises(InsecureConfig):
        create_app(token="short")
    assert create_app(token="x" * 32) is not None
