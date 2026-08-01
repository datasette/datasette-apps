"""Tests for the app_debug agent tool, built on context.browser_task()."""

from dataclasses import dataclass
import json

import pytest
from datasette.app import Datasette

from datasette_apps import Registry
from datasette_apps.agent_tools import get_app_edit_tools
from datasette_apps.debug import get_debug_job, get_debug_job_for_call


@dataclass
class FakeAgentTool:
    name: str
    description: str
    input_schema: dict
    fn: object
    required_permission: str | None = None


class FakeBrowserTaskPending(Exception):
    """Stands in for datasette_agent's BrowserTaskPending - must propagate."""

    def __init__(self, task):
        super().__init__(task.get("label") or "Working in your browser")
        self.task = task


class BrowserTasksNotSupported(Exception):
    """Matched by class name, like datasette_agent's exception."""


class FakeContext:
    def __init__(
        self,
        *,
        conversation_id="conv-1",
        call_key="call-1",
        outcome=None,
        supports_browser_tasks=True,
    ):
        self.conversation_id = conversation_id
        self.call_key = call_key
        self.outcome = outcome
        self.supports_browser_tasks = supports_browser_tasks
        self.task = None

    async def browser_task(self, html, *, payload=None, label=None, timeout_ms=60000):
        if not self.supports_browser_tasks:
            raise BrowserTasksNotSupported()
        self.task = {
            "html": html,
            "payload": payload,
            "label": label,
            "timeout_ms": timeout_ms,
        }
        if self.outcome is not None:
            return self.outcome
        raise FakeBrowserTaskPending({"label": label, "html": html})


def _tools_by_name():
    return {tool.name: tool for tool in get_app_edit_tools(FakeAgentTool)}


async def _make_app(datasette, **kwargs):
    kwargs.setdefault("actor_id", "alice")
    kwargs.setdefault("name", "Debug target")
    kwargs.setdefault("description", "")
    kwargs.setdefault("html", "<h1>Hello</h1>")
    return await Registry(datasette).create_stored_app(**kwargs)


async def _job_count(datasette):
    return (
        await datasette.get_internal_database().execute(
            "SELECT count(*) AS n FROM _app_debug_jobs"
        )
    ).first()["n"]


COMPLETED_OUTCOME = {
    "ok": True,
    "result": {"title": "Hello"},
    "events": {
        "errors": [{"kind": "console-error", "message": "warned"}],
        "logs": [{"kind": "datasette-call", "message": "datasette.query(...)"}],
    },
    "duration_ms": 321,
    "timed_out": False,
    "outcome": "completed",
}


@pytest.mark.asyncio
async def test_app_debug_tool_is_registered():
    tools = _tools_by_name()
    assert "app_debug" in tools
    tool = tools["app_debug"]
    assert tool.input_schema["required"] == ["app_id", "javascript"]
    properties = tool.input_schema["properties"]
    assert set(properties) == {"app_id", "javascript", "viewport", "timeout_ms"}
    assert properties["viewport"]["type"] == "object"
    assert properties["timeout_ms"]["type"] == "integer"
    assert "debug.waitFor" in tool.description
    assert "async" in tool.description
    assert "JSON-serializable" in tool.description
    assert "isTrusted" in tool.description


@pytest.mark.asyncio
async def test_app_debug_requires_edit_permission():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "bob"},
            context=FakeContext(),
            app_id=app["id"],
            javascript="return 1;",
        )
    )
    assert result == {"error": "Permission denied: edit-app", "app_id": app["id"]}


@pytest.mark.asyncio
async def test_app_debug_suspends_turn_with_payload_and_pending_job():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    tools = _tools_by_name()
    context = FakeContext()

    with pytest.raises(FakeBrowserTaskPending):
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            javascript="return document.title;",
            viewport={"width": 375, "height": 812},
        )

    job = await get_debug_job_for_call(datasette, "conv-1", "call-1")
    assert job is not None
    assert job["status"] == "pending"
    assert job["app_id"] == app["id"]
    assert job["javascript"] == "return document.title;"
    assert job["config"]["viewport"] == {"width": 375, "height": 812}

    # Per-run specifics ride the one-shot claim payload, never the HTML
    payload = context.task["payload"]
    assert payload["javascript"] == "return document.title;"
    assert payload["viewport"] == {"width": 375, "height": 812}
    assert payload["timeout_ms"] == job["config"]["timeout_ms"]
    assert payload["channel_token"] == job["config"]["channel_token"]
    assert payload["frame_url"] == (
        f"/-/apps/debug/{job['id']}/frame?token={job['config']['frame_token']}"
    )
    assert payload["query_url"] == f"/-/apps/debug/{job['id']}/query"

    html = context.task["html"]
    assert "datasetteAgent" in html
    assert job["config"]["channel_token"] not in html
    assert job["config"]["frame_token"] not in html
    assert "return document.title;" not in html

    assert "Debug target" in context.task["label"]
    # The task deadline leaves slack beyond the job's own timeout
    assert context.task["timeout_ms"] == job["config"]["timeout_ms"] + 2000

    # Re-executing the same suspended call reuses the job instead of
    # creating an orphan row
    with pytest.raises(FakeBrowserTaskPending):
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(),
            app_id=app["id"],
            javascript="return document.title;",
            viewport={"width": 375, "height": 812},
        )
    assert await _job_count(datasette) == 1


@pytest.mark.asyncio
async def test_app_debug_returns_result_envelope_after_resume():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    tools = _tools_by_name()

    with pytest.raises(FakeBrowserTaskPending):
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(),
            app_id=app["id"],
            javascript="return document.title;",
        )
    job = await get_debug_job_for_call(datasette, "conv-1", "call-1")

    result = json.loads(
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(outcome=dict(COMPLETED_OUTCOME)),
            app_id=app["id"],
            javascript="return document.title;",
        )
    )
    assert result["ok"] is True
    assert result["outcome"] == "completed"
    assert result["app_id"] == app["id"]
    assert result["version"] == 1
    assert result["result"] == {"title": "Hello"}
    assert result["events"]["errors"] == [
        {"kind": "console-error", "message": "warned"}
    ]
    assert result["events"]["logs"] == [
        {"kind": "datasette-call", "message": "datasette.query(...)"}
    ]
    assert result["duration_ms"] == 321
    assert result["timed_out"] is False
    assert "error" not in result

    # The audit row records the envelope; no extra job rows appeared
    stored = await get_debug_job(datasette, job["id"])
    assert stored["status"] == "completed"
    assert stored["result"]["result"] == {"title": "Hello"}
    assert await _job_count(datasette) == 1


@pytest.mark.asyncio
async def test_app_debug_reports_expired_and_cancelled_outcomes():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    tools = _tools_by_name()

    for outcome_kind, message in [
        ("expired", "Browser task timed out before completing"),
        ("cancelled", "Cancelled by the user"),
    ]:
        context = FakeContext(
            call_key=f"call-{outcome_kind}",
            outcome={
                "ok": False,
                "error": {"message": message},
                "outcome": outcome_kind,
            },
        )
        result = json.loads(
            await tools["app_debug"].fn(
                datasette=datasette,
                actor={"id": "alice"},
                context=context,
                app_id=app["id"],
                javascript="return 1;",
            )
        )
        assert result["ok"] is False
        assert result["outcome"] == outcome_kind
        assert result["error"]["message"] == message


@pytest.mark.asyncio
async def test_app_debug_fresh_identical_call_creates_new_job():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    tools = _tools_by_name()

    with pytest.raises(FakeBrowserTaskPending):
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(),
            app_id=app["id"],
            javascript="return 1;",
        )
    await tools["app_debug"].fn(
        datasette=datasette,
        actor={"id": "alice"},
        context=FakeContext(outcome=dict(COMPLETED_OUTCOME)),
        app_id=app["id"],
        javascript="return 1;",
    )

    # A later identical call (its browser task marked consumed by the
    # runtime) runs fresh: new job, prior audit row untouched
    with pytest.raises(FakeBrowserTaskPending):
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(),
            app_id=app["id"],
            javascript="return 1;",
        )
    assert await _job_count(datasette) == 2


@pytest.mark.asyncio
async def test_app_debug_requires_browser_task_support():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(supports_browser_tasks=False),
            app_id=app["id"],
            javascript="return 1;",
        )
    )
    assert "interactive" in result["error"]


@pytest.mark.asyncio
async def test_app_debug_rejects_invalid_viewport():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(),
            app_id=app["id"],
            javascript="return 1;",
            viewport={"width": 10, "height": 10},
        )
    )
    assert "viewport" in result["error"].lower()


@pytest.mark.asyncio
async def test_app_debug_truncates_oversized_results_keeping_errors():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    tools = _tools_by_name()

    errors = [{"kind": "javascript-error", "message": f"error {i}"} for i in range(5)]
    result = json.loads(
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(
                outcome={
                    "ok": True,
                    "result": {"huge": "x" * 200000},
                    "events": {
                        "errors": errors,
                        "logs": [{"kind": "console-log", "message": "y" * 2000}] * 50,
                    },
                    "duration_ms": 5,
                    "timed_out": False,
                    "outcome": "completed",
                }
            ),
            app_id=app["id"],
            javascript="return 1;",
        )
    )
    # Errors survive truncation; the oversized result and logs do not
    assert result["events"]["errors"] == errors
    assert result["result"] != {"huge": "x" * 200000}
    assert "truncated" in result
    assert any("result" in item for item in result["truncated"])
    assert len(json.dumps(result)) < 60000


@pytest.mark.asyncio
async def test_app_debug_with_real_datasette_agent_tool_context(tmp_path):
    """Integration against the real browser-task runtime, when
    datasette-agent is installed: suspend raises BrowserTaskPending, the
    payload rides the one-shot claim, and replay returns the envelope."""
    pytest.importorskip("datasette_agent")
    from datasette_agent.browser_tasks import (
        BrowserTaskPending,
        claim_task,
        complete_task,
    )
    from datasette_agent.schema import ensure_tables
    from datasette_agent.tools import ToolContext

    datasette = Datasette(memory=True, internal=str(tmp_path / "internal.db"))
    await datasette.invoke_startup()
    await ensure_tables(datasette.get_internal_database())
    app = await _make_app(datasette)
    tools = _tools_by_name()

    def make_context():
        return ToolContext(
            datasette=datasette,
            actor={"id": "alice"},
            conversation_id="01CONVERSATION00000000TEST",
            tool_name="app_debug",
            arguments={"app_id": app["id"], "javascript": "return 1;"},
            tool_call_id="call_1",
            supports_browser_tasks=True,
        )

    with pytest.raises(BrowserTaskPending) as excinfo:
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=make_context(),
            app_id=app["id"],
            javascript="return 1;",
        )
    task = excinfo.value.task
    assert "Debug target" in task["label"]
    assert "datasetteAgent" in task["html"]

    db = datasette.get_internal_database()
    claimed, state = await claim_task(db, task["id"], "alice")
    assert state is None
    payload = json.loads(claimed["payload_json"])
    job = await get_debug_job_for_call(
        datasette, "01CONVERSATION00000000TEST", "id:call_1"
    )
    assert payload["channel_token"] == job["config"]["channel_token"]
    assert payload["javascript"] == "return 1;"

    assert (
        await complete_task(
            db,
            task["id"],
            {"ok": True, "result": {"answer": 1}, "duration_ms": 10},
            "alice",
        )
        is None
    )

    result = json.loads(
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=make_context(),
            app_id=app["id"],
            javascript="return 1;",
        )
    )
    assert result["ok"] is True
    assert result["outcome"] == "completed"
    assert result["result"] == {"answer": 1}
    assert (await get_debug_job(datasette, job["id"]))["status"] == "completed"
