"""Minimal A2A (Agent2Agent) JSON-RPC endpoint.

Implements the parts of the A2A spec other agents need to delegate a resume:

* ``GET /.well-known/agent-card.json`` (and legacy ``agent.json``) — AgentCard
* ``POST /a2a`` JSON-RPC 2.0 methods ``message/send``, ``tasks/get``, ``tasks/cancel``

``message/send`` accepts a text part (a job URL, or pasted JD text) or a data part
``{"jd_url", "jd_text", "slug", "instructions"}``. With
``configuration.blocking: false`` it returns a ``working`` task immediately; poll
``tasks/get``. The completed task carries the PDF as a ``file`` artifact.
Server-side generation needs a model key on the server (ANTHROPIC_API_KEY or OPENROUTER_API_KEY,
via LiteLLM) because A2A callers send natural language.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, tracing
from .workspace import get_job

PROTOCOL_VERSION = "0.3.0"
URL_RE = re.compile(r"https?://\S+")

_tasks: dict[str, dict] = {}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def agent_card(base_url: str, *, auth: bool) -> dict:
    card: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "LaTeX Resume Agent",
        "description": "Tailors the owner's one-page LaTeX resume to a job posting, proofreads the compiled "
        "PDF (layout, invented-number and ownership guardrails, independent visual review) and returns it.",
        "url": f"{base_url}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/pdf", "text/markdown"],
        "skills": [
            {
                "id": "tailor_resume",
                "name": "Tailor resume to a job",
                "description": "Give a job posting URL or the JD text; get back a one-page tailored resume PDF "
                "plus the requirements-to-evidence analysis and proofreading verdict.",
                "tags": ["resume", "latex", "job-application", "pdf"],
                "examples": [
                    "https://job-boards.greenhouse.io/acme/jobs/1234567",
                    '{"jd_url": "https://jobs.lever.co/acme/abc", "instructions": "emphasize search"}',
                ],
                "inputModes": ["text/plain", "application/json"],
                "outputModes": ["application/pdf", "text/markdown"],
            },
            {
                "id": "record_application",
                "name": "Record a submitted application",
                "description": "After you submit an application, send a data part with action=record_application, "
                "job_url, slug (or resume_pdf_base64), cover_letter_text, questions=[{question, answer}], "
                "confirmation_id, notes. Also: action=update_application (application_id, status, note, at, "
                "confirmation_id, add_questions), list_applications (status, company, since), get_application "
                "(application_id), get_application_file (application_id, filename; omit filename to list).",
                "tags": ["job-application", "tracking"],
                "examples": [
                    '{"action": "record_application", "job_url": "https://jobs.lever.co/acme/abc", "slug": "acme-ml", '
                    '"questions": [{"question": "Why Acme?", "answer": "..."}]}',
                    '{"action": "update_application", "application_id": "2026-11-06_acme_ml-engineer_3f9a2c", '
                    '"status": "interview", "note": "Recruiter screen booked"}',
                ],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
        ],
    }
    if auth:
        card["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer"}}
        card["security"] = [{"bearer": []}]
    return card


def _rpc_error(id_, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def _agent_message(text: str, task_id: str, context_id: str) -> dict:
    return {
        "kind": "message",
        "role": "agent",
        "messageId": str(uuid.uuid4()),
        "taskId": task_id,
        "contextId": context_id,
        "parts": [{"kind": "text", "text": text}],
    }


def parse_request(message: dict) -> dict:
    """Turn A2A message parts into generate_resume kwargs."""
    args: dict[str, Any] = {}
    texts: list[str] = []
    for part in message.get("parts", []):
        kind = part.get("kind") or part.get("type")
        if kind == "data" and isinstance(part.get("data"), dict):
            d = part["data"]
            args.update({k: d[k] for k in ("jd_url", "jd_text", "slug", "instructions") if d.get(k)})
        elif kind == "text":
            texts.append(part.get("text", ""))
    text = "\n".join(texts).strip()
    if text and not (args.get("jd_url") or args.get("jd_text")):
        m = URL_RE.search(text)
        rest = re.sub(r"\s+", " ", URL_RE.sub("", text)).strip() if m else text
        if m:
            args["jd_url"] = m.group(0).rstrip(").,>")
            if rest:
                args["instructions"] = (args.get("instructions", "") + "\n" + rest).strip()
        else:
            args["jd_text"] = text
    return args


APP_ACTIONS = {"record_application", "update_application", "list_applications", "get_application",
               "get_application_file"}


def find_action(message: dict) -> dict | None:
    for part in message.get("parts", []):
        if (part.get("kind") or part.get("type")) == "data" and isinstance(part.get("data"), dict):
            if part["data"].get("action") in APP_ACTIONS:
                return part["data"]
    return None


def run_app_action(data: dict) -> Any:
    from . import applications as apps

    d = {k: v for k, v in data.items() if k != "action"}
    action = data["action"]
    if action == "record_application":
        import inspect

        allowed = set(inspect.signature(apps.record_application).parameters)
        res = apps.record_application(**{k: v for k, v in d.items() if k in allowed and k not in ("apps_dir", "jobs_dir")})
        return {k: v for k, v in res.items() if k != "path"}
    if action == "update_application":
        app_id = d.pop("application_id", None)
        if not app_id:
            raise apps.ApplicationError("application_id is required")
        keep = ("status", "note", "at", "by", "add_questions", "confirmation_id", "extra")
        return apps.summary(apps.update_application(app_id, **{k: d[k] for k in keep if k in d}))
    if action == "get_application_file":
        app_id, name = d.get("application_id", ""), d.get("filename")
        if not name:
            return {"files": apps.list_files(app_id)}
        data, mime = apps.read_file(app_id, name)
        return {"__file__": {"name": name.split("/")[-1], "mimeType": mime, "bytes": base64.b64encode(data).decode()}}
    if action == "list_applications":
        return apps.list_applications(**{k: d[k] for k in ("status", "company", "since") if k in d})
    rec = apps.get_application(d.get("application_id", ""))
    rec.pop("path", None)
    return rec


def _run_task(task: dict, args: dict, generate: Callable[..., Any]) -> None:
    tid, cid = task["id"], task["contextId"]
    try:
        res = generate(**args)
    except Exception as exc:  # report every failure through the task
        with _lock:
            task["status"] = {"state": "failed", "message": _agent_message(str(exc), tid, cid), "timestamp": _now()}
        return
    ws = get_job(res.workspace["slug"])
    artifacts = []
    if ws.pdf.exists():
        artifacts.append(
            {
                "artifactId": str(uuid.uuid4()),
                "name": "resume.pdf",
                "parts": [
                    {
                        "kind": "file",
                        "file": {
                            "name": f"resume-{ws.slug}.pdf",
                            "mimeType": "application/pdf",
                            "bytes": base64.b64encode(ws.pdf.read_bytes()).decode(),
                        },
                    }
                ],
            }
        )
    if ws.analysis.exists():
        artifacts.append(
            {"artifactId": str(uuid.uuid4()), "name": "analysis.md", "parts": [{"kind": "text", "text": ws.analysis.read_text()}]}
        )
    summary = f"Verdict: {res.verdict} after {res.rounds} round(s). Job slug: {ws.slug}."
    if res.remaining_issues:
        summary += "\nUnresolved:\n- " + "\n- ".join(res.remaining_issues)
    with _lock:
        task["artifacts"] = artifacts
        task["status"] = {
            "state": "completed" if ws.pdf.exists() else "failed",
            "message": _agent_message(summary, tid, cid),
            "timestamp": _now(),
        }


def make_handler(generate: Callable[..., Any] | None = None):
    def _default_generate(**kw):
        from .llm_pipeline import generate_resume

        return generate_resume(**kw)

    gen = generate or _default_generate

    async def handler(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return _rpc_error(None, -32700, "Parse error")
        method = body.get("method")
        action = find_action((body.get("params") or {}).get("message") or {}) if method == "message/send" else None
        with tracing.span("a2a.request", method=method or "", action=(action or {}).get("action", "tailor_resume"
                          if method == "message/send" else "")) as s:
            resp = await _handle(body)
            try:
                payload = json.loads(resp.body)
                state = ((payload.get("result") or {}).get("status") or {}).get("state")
                s.set(state=state, rpc_error=(payload.get("error") or {}).get("code"))
                if state == "failed" or payload.get("error"):
                    s.set_error((payload.get("error") or {}).get("message") or "task failed")
            except Exception:
                pass
            return resp

    async def _handle(body: dict) -> JSONResponse:
        id_, method, params = body.get("id"), body.get("method"), body.get("params") or {}

        if method == "message/send":
            message = params.get("message") or {}
            action = find_action(message)
            if action:
                from .applications import ApplicationError

                tid, cid = str(uuid.uuid4()), message.get("contextId") or str(uuid.uuid4())
                try:
                    import anyio

                    result = await anyio.to_thread.run_sync(run_app_action, action)
                    state, text = "completed", f"{action['action']} ok"
                    if isinstance(result, dict) and "__file__" in result:
                        f = result["__file__"]
                        artifacts = [{"artifactId": str(uuid.uuid4()), "name": f["name"],
                                      "parts": [{"kind": "file", "file": f}]}]
                    else:
                        artifacts = [{"artifactId": str(uuid.uuid4()), "name": f"{action['action']}.json",
                                      "parts": [{"kind": "data", "data": {"result": result}}]}]
                except (ApplicationError, TypeError) as exc:
                    state, text, artifacts = "failed", str(exc), []
                task = {"kind": "task", "id": tid, "contextId": cid, "artifacts": artifacts, "history": [message],
                        "status": {"state": state, "message": _agent_message(text, tid, cid), "timestamp": _now()}}
                with _lock:
                    _tasks[tid] = task
                return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": task})
            args = parse_request(message)
            if not (args.get("jd_url") or args.get("jd_text")):
                return _rpc_error(id_, -32602, "Send a job URL or JD text (text part) or a data part with jd_url/jd_text")
            task = {
                "kind": "task",
                "id": str(uuid.uuid4()),
                "contextId": message.get("contextId") or str(uuid.uuid4()),
                "status": {"state": "working", "timestamp": _now()},
                "artifacts": [],
                "history": [message],
            }
            with _lock:
                _tasks[task["id"]] = task
            blocking = (params.get("configuration") or {}).get("blocking", True)
            if blocking:
                import anyio

                await anyio.to_thread.run_sync(_run_task, task, args, gen)
            else:
                threading.Thread(target=_run_task, args=(task, args, gen), daemon=True).start()
            with _lock:
                return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": dict(task)})

        if method == "tasks/get":
            with _lock:
                task = _tasks.get(params.get("id", ""))
                if not task:
                    return _rpc_error(id_, -32001, "Task not found")
                return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": dict(task)})

        if method == "tasks/cancel":
            return _rpc_error(id_, -32002, "Task cannot be canceled")

        return _rpc_error(id_, -32601, f"Method not found: {method}")

    return handler
