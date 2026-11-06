import base64
import csv
import json

import pytest
from starlette.testclient import TestClient

from resume_agent import __main__ as cli
from resume_agent import applications as apps
from resume_agent import gate, harness, paths
from resume_agent.compile import check_dependencies, compile_resume
from resume_agent.server import create_app
from resume_agent.workspace import new_job

needs_tex = pytest.mark.skipif(bool(check_dependencies()), reason="pdflatex/poppler not installed")
URL = "https://job-boards.greenhouse.io/acme/jobs/4012345"
JD = "# Senior ML Engineer, Search\n\n- **Company:** Acme\n- **Location:** Remote\n\nBuild search."
QA = [
    {"question": "Why do you want to work at Acme?", "answer": "Search quality at scale."},
    {"question": "Are you authorized to work in Canada?", "answer": "Yes"},
]


@pytest.fixture(autouse=True)
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(paths, "APPLICATIONS_DIR", tmp_path / "applications")
    return tmp_path


@pytest.fixture
def job():
    ws = new_job("acme-search", JD + "\n\nSource: " + URL)
    compile_resume(ws.tex)
    return ws


@pytest.fixture
def anyio_backend():
    return "asyncio"


@needs_tex
def test_record_from_slug_snapshots_everything(job, dirs):
    res = apps.record_application(
        job_url=URL, slug="acme-search", questions=QA, cover_letter_text="Dear Acme,\n...",
        confirmation_id="GH-123", submitted_by="test-agent", extra={"salary_expectation": "200k"},
        submitted_at="2026-11-06T13:45:00-08:00",
    )
    rec, folder = res["record"], dirs / "applications" / res["id"]
    assert res["created"] and res["id"].startswith("2026-11-06_acme_senior-ml-engineer-search_")
    assert rec["job"] == {
        "url": URL, "company": "Acme", "role": "Senior ML Engineer, Search", "location": "Remote",
        "platform": "greenhouse", "jd_file": "jd.md", "source_slug": "acme-search",
    }
    assert rec["resume"]["sha256"] == gate.tex_hash(job.pdf) and rec["resume"]["matches_generated"] is True
    # never verified -> recorded as such, with a warning
    assert rec["resume"]["verification"]["deliverable"] is False
    assert any("not verifier-approved" in w for w in res["warnings"])
    assert {"application.json", "resume.pdf", "resume.tex", "jd.md", "cover_letter.md", "answers.md"} <= {
        p.name for p in folder.iterdir()}
    assert "## 2. Are you authorized to work in Canada?" in (folder / "answers.md").read_text()
    assert rec["submitted_at"] == "2026-11-06T13:45:00-08:00"
    rows = list(csv.DictReader((dirs / "applications" / "index.csv").open()))
    assert rows[0]["id"] == res["id"] and rows[0]["resume_verified"] == "False"
    assert "| 2026-11-06 | Acme |" in (dirs / "applications" / "index.md").read_text()


@needs_tex
def test_verified_resume_and_different_pdf(job):
    gate.record_review(job.dir, "VERDICT: PASS\n\nMUST FIX\n- none\n\nCONSIDER\n- none", source="test")
    ok = apps.record_application(job_url=URL, slug="acme-search")["record"]
    assert ok["resume"]["verification"]["deliverable"] is True

    other = b"%PDF-1.4 edited by hand"
    res = apps.record_application(job_url=URL + "?x=1", slug="acme-search",
                                  resume_pdf_base64=base64.b64encode(other).decode())
    assert res["record"]["resume"]["matches_generated"] is False
    assert any("differs from the generated" in w for w in res["warnings"])


@needs_tex
def test_idempotency_and_duplicates(job):
    a = apps.record_application(job_url=URL, slug="acme-search", idempotency_key="k1")
    b = apps.record_application(job_url=URL, slug="acme-search", idempotency_key="k1")
    assert b["created"] is False and b["id"] == a["id"]
    c = apps.record_application(job_url=URL + "/", slug="acme-search")
    assert c["created"] and any("Possible duplicate" in w for w in c["warnings"])
    assert len(apps.list_applications()) == 2


@needs_tex
def test_update_timeline_and_filters(job):
    app_id = apps.record_application(job_url=URL, slug="acme-search", submitted_at="2026-11-01T10:00:00Z")["id"]
    apps.update_application(app_id, status="screen", note="Recruiter emailed", at="2026-11-04T09:00:00Z")
    rec = apps.update_application(app_id, add_questions=[{"question": "Notice period?", "answer": "2 weeks"}])
    assert rec["status"] == "screen" and [e["status"] for e in rec["events"]] == ["submitted", "screen"]
    assert len(rec["questions"]) == 1
    assert [r["id"] for r in apps.list_applications(status="screen")] == [app_id]
    assert apps.list_applications(since="2026-11-02") == []
    assert apps.list_applications(company="acm")[0]["id"] == app_id
    with pytest.raises(apps.ApplicationError):
        apps.update_application(app_id, status="hired-ish")
    with pytest.raises(apps.ApplicationError):
        apps.get_application("../etc")


def test_input_validation(tmp_path):
    with pytest.raises(apps.ApplicationError, match="http"):
        apps.record_application(job_url="acme.com", resume_pdf_base64="x")
    with pytest.raises(apps.ApplicationError, match="slug"):
        apps.record_application(job_url=URL)
    with pytest.raises(apps.ApplicationError, match="PDF"):
        apps.record_application(job_url=URL, resume_pdf_base64=base64.b64encode(b"hello").decode(), fetch_jd_snapshot=False)
    pdf = base64.b64encode(b"%PDF-1.4 x").decode()
    with pytest.raises(apps.ApplicationError, match="not allowed"):
        apps.record_application(job_url=URL, resume_pdf_base64=pdf, fetch_jd_snapshot=False,
                                attachments=[{"filename": "run.sh", "content_base64": pdf}])
    # failed record leaves no half-written folder
    assert not (paths.APPLICATIONS_DIR.exists() and any(p.is_dir() for p in paths.APPLICATIONS_DIR.iterdir()))


def test_snapshot_failure_is_a_warning_not_an_error(monkeypatch):
    import resume_agent.jd_fetch as jf

    def boom(url):
        raise jf.FetchError("blocked")

    monkeypatch.setattr(jf, "fetch_jd", boom)
    res = apps.record_application(job_url=URL, resume_pdf_base64=base64.b64encode(b"%PDF-1.4 x").decode(),
                                  company="Acme", role="MLE")
    assert res["created"] and any("snapshot" in w for w in res["warnings"])
    assert res["record"]["job"]["jd_file"] is None


# ----------------------------------------------------------------- entry points


@needs_tex
@pytest.mark.anyio
async def test_mcp_tools(job):
    from mcp.client import Client

    from resume_agent.mcp_server import mcp

    async with Client(mcp) as c:
        r = await c.call_tool("record_application", {
            "job_url": URL, "slug": "acme-search", "questions": QA, "cover_letter_text": "Hi",
            "submitted_by": "other-agent", "idempotency_key": "mcp-1",
        })
        assert not r.is_error, r.content
        app_id = r.structured_content["id"]
        r = await c.call_tool("update_application", {"application_id": app_id, "status": "interview", "note": "onsite"})
        assert r.structured_content["status"] == "interview"
        rows = (await c.call_tool("list_applications", {"status": "interview"})).structured_content
        assert rows["result"][0]["id"] == app_id if isinstance(rows, dict) else rows[0]["id"] == app_id
        full = (await c.call_tool("get_application", {"application_id": app_id})).structured_content
        assert len(full["questions"]) == 2 and "resume.pdf" in full["files"]
        bad = await c.call_tool("update_application", {"application_id": app_id, "status": "nope"})
        assert bad.is_error


@needs_tex
def test_a2a_actions(job):
    client = TestClient(create_app(token="", generate=lambda **kw: None))

    def send(data):
        body = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
                "params": {"message": {"role": "user", "messageId": "m", "parts": [{"kind": "data", "data": data}]}}}
        return client.post("/a2a", json=body).json()["result"]

    card = client.get("/.well-known/agent-card.json").json()
    assert {s["id"] for s in card["skills"]} == {"tailor_resume", "record_application"}

    t = send({"action": "record_application", "job_url": URL, "slug": "acme-search", "questions": QA,
              "apps_dir": "/etc"})  # unknown/unsafe keys are ignored
    assert t["status"]["state"] == "completed", t
    app_id = t["artifacts"][0]["parts"][0]["data"]["result"]["id"]
    assert paths.APPLICATIONS_DIR.joinpath(app_id, "answers.md").exists()
    t = send({"action": "update_application", "application_id": app_id, "status": "rejected"})
    assert t["artifacts"][0]["parts"][0]["data"]["result"]["status"] == "rejected"
    t = send({"action": "list_applications"})
    assert len(t["artifacts"][0]["parts"][0]["data"]["result"]) == 1
    t = send({"action": "update_application", "status": "rejected"})
    assert t["status"]["state"] == "failed"


@needs_tex
def test_cli_record_with_markdown_qa(job, tmp_path, capsys):
    qa = tmp_path / "qa.md"
    qa.write_text("## Why Acme?\nBecause search.\n\n## 2. Visa sponsorship needed?\nNo\n")
    cl = tmp_path / "cl.md"
    cl.write_text("Dear team")
    assert cli.main(["app", "record", "--url", URL, "--slug", "acme-search", "--qa", str(qa),
                     "--cover-letter", str(cl), "--confirmation-id", "C-9"]) == 0
    app_id = capsys.readouterr().out.split("Recorded: ")[1].split()[0]
    rec = apps.get_application(app_id)
    assert rec["questions"] == [{"question": "Why Acme?", "answer": "Because search."},
                                {"question": "Visa sponsorship needed?", "answer": "No"}]
    assert rec["confirmation_id"] == "C-9" and rec["cover_letter"]["file"] == "cover_letter.md"
    assert cli.main(["app", "update", app_id, "--status", "ghosted"]) == 0
    assert cli.main(["app", "list", "--status", "ghosted"]) == 0
    assert app_id in capsys.readouterr().out


def test_harness_routes_application_writes_through_the_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    ev = lambda tool, ti, agent=None: {"hook_event_name": "PreToolUse", "cwd": str(tmp_path), "tool_name": tool,  # noqa: E731
                                       "tool_input": ti, **({"agent_type": agent, "agent_id": "a"} if agent else {})}
    deny = lambda out: (out or {}).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"  # noqa: E731
    assert deny(harness.handle(ev("Write", {"file_path": "applications/x/application.json"})))
    assert deny(harness.handle(ev("Bash", {"command": "rm -rf applications/x"})))
    assert harness.handle(ev("Bash", {"command": "python3 -m resume_agent app list"})) is None
    assert deny(harness.handle(ev("Bash", {"command": "python3 -m resume_agent app record --url u --slug s"}, harness.WRITER)))
