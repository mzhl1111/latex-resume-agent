import json
import types

import pytest
from starlette.testclient import TestClient

from resume_agent import llm_pipeline, paths
from resume_agent.a2a_server import parse_request
from resume_agent.compile import check_dependencies
from resume_agent.llm_pipeline import generate_resume
from resume_agent.server import create_app

needs_tex = pytest.mark.skipif(bool(check_dependencies()), reason="pdflatex/poppler not installed")
JD = "# ML Infra Engineer at Acme\n\nBuild retrieval and ML platform systems. " * 5


@pytest.fixture(autouse=True)
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "JOBS_DIR", tmp_path / "jobs")
    return tmp_path / "jobs"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ----------------------------------------------------------------- MCP (in-memory)


@needs_tex
@pytest.mark.anyio
async def test_mcp_caller_driven_flow():
    from mcp.client import Client

    from resume_agent.mcp_server import mcp

    async with Client(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"start_job", "get_resume_context", "write_resume", "get_resume_pdf", "generate_resume"} <= names

        r = await c.call_tool("start_job", {"jd_text": JD, "slug": "acme"})
        slug = r.structured_content["slug"]
        assert slug == "acme"

        ctx = (await c.call_tool("get_resume_context", {"slug": slug})).structured_content
        assert "owned by another engineer" in ctx["profile_rules_md"] and ctx["jd_md"].startswith("# ML Infra")

        tex = ctx["base_template_tex"].replace("Machine Learning Engineer building", "ML Platform Engineer building")
        r = await c.call_tool("write_resume", {"slug": slug, "resume_tex": tex, "analysis_md": "# a"})
        assert not r.is_error and r.structured_content["ok"]
        assert any(b.type == "image" for b in r.content)

        bad = tex.replace(r"\begin{document}", r"\begin{document}\input{/etc/passwd}")
        assert (await c.call_tool("write_resume", {"slug": slug, "resume_tex": bad})).is_error

        pdf = await c.call_tool("get_resume_pdf", {"slug": slug})
        res = next(b for b in pdf.content if b.type == "resource")
        assert res.resource.mime_type == "application/pdf"


# ----------------------------------------------------------------- HTTP app


def test_agent_card_and_auth():
    client = TestClient(create_app(token="s3cret", generate=lambda **kw: None))
    card = client.get("/.well-known/agent-card.json").json()
    assert card["url"].endswith("/a2a") and card["skills"][0]["id"] == "tailor_resume"
    assert client.post("/a2a", json={}).status_code == 401
    assert client.get("/healthz").status_code in (200, 503)


def test_parse_request_variants():
    assert parse_request({"parts": [{"kind": "text", "text": "Please do https://jobs.lever.co/a/b, emphasize search"}]}) == {
        "jd_url": "https://jobs.lever.co/a/b",
        "instructions": "Please do emphasize search",
    }
    assert parse_request({"parts": [{"kind": "data", "data": {"jd_text": "x", "slug": "s"}}]}) == {"jd_text": "x", "slug": "s"}
    assert parse_request({"parts": [{"kind": "text", "text": "Senior engineer role..."}]}) == {"jd_text": "Senior engineer role..."}


@needs_tex
def test_a2a_message_send_returns_pdf_artifact():
    from resume_agent.compile import compile_resume
    from resume_agent.workspace import new_job

    def fake_generate(**kw):
        ws = new_job("acme-a2a", kw.get("jd_text") or "# JD")
        compile_resume(ws.tex)
        return types.SimpleNamespace(workspace=ws.describe(), verdict="PASS", rounds=1, remaining_issues=[])

    client = TestClient(create_app(token="", generate=fake_generate))
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {"message": {"role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": JD}]}},
    }
    task = client.post("/a2a", json=body).json()["result"]
    assert task["status"]["state"] == "completed"
    file_part = task["artifacts"][0]["parts"][0]
    assert file_part["kind"] == "file" and file_part["file"]["mimeType"] == "application/pdf"
    got = client.post("/a2a", json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task["id"]}}).json()
    assert got["result"]["id"] == task["id"]
    assert client.post("/a2a", json={"jsonrpc": "2.0", "id": 3, "method": "nope"}).json()["error"]["code"] == -32601


# ----------------------------------------------------------------- LLM pipeline (fake client)


class FakeClient:
    """Writer returns a draft with an invented metric first, then a clean one; reviewer passes."""

    def __init__(self):
        self.calls = []
        self.verifier_calls = []
        base = paths.BASE_TEMPLATE.read_text()
        self.drafts = [base.replace(r"\textasciitilde60\%", r"\textasciitilde65\%"), base]
        self.messages = types.SimpleNamespace(create=self.create)

    def create(self, *, model, max_tokens, system, messages):
        self.calls.append(list(messages))
        if "independent resume verifier" in system:
            self.verifier_calls.append(messages)
            text = "VERDICT: PASS\n\nMUST FIX\n- none\n\nCONSIDER\n- none"
        else:
            text = f"<analysis>\n# Analysis WRITER-SECRET-REASONING\n</analysis>\n<resume_tex>\n{self.drafts.pop(0)}\n</resume_tex>"
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)])


@needs_tex
def test_pipeline_fixes_invented_metric_then_passes_review():
    fake = FakeClient()
    res = generate_resume(jd_text=JD, slug="acme-llm", client=fake)
    assert res.ok and res.verdict == "PASS", res.log
    assert len(fake.calls) == 3  # draft, fix after proofread, review
    assert "65%" in json.dumps(fake.calls[1][-1])  # proofread report was fed back
    # The verifier ran in a fresh single-message conversation with no writer context.
    (v_msgs,) = fake.verifier_calls
    assert len(v_msgs) == 1 and "WRITER-SECRET-REASONING" not in json.dumps(v_msgs)
    assert any(b.get("type") == "image" for b in v_msgs[0]["content"])
    assert (paths.JOBS_DIR / "acme-llm" / "review-1.md").exists()


def test_review_parsing():
    review = "VERDICT: NEEDS CHANGES\n\nMUST FIX\n- [Northwind 2] overstated\n- [Skills] add none\n\nCONSIDER\n- none"
    assert llm_pipeline._review_issues(review) == ["[Northwind 2] overstated", "[Skills] add none"]
