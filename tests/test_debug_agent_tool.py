"""Tests for the app_debug agent tool."""

from dataclasses import dataclass
import json

import pytest
from datasette.app import Datasette

from datasette_apps import Registry
from datasette_apps.agent_tools import get_app_edit_tools
from datasette_apps.debug import get_debug_job_for_call


@dataclass
class FakeAgentTool:
    name: str
    description: str
    input_schema: dict
    fn: object
    required_permission: str | None = None


class FakeQuestionPending(Exception):
    """Stands in for datasette_agent's QuestionPending - must propagate."""

    def __init__(self, question):
        super().__init__(question["prompt"])
        self.question = question


class QuestionsNotSupported(Exception):
    """Matched by class name, like datasette_agent's exception."""


class FakeContext:
    def __init__(
        self,
        *,
        conversation_id="conv-1",
        call_key="call-1",
        answer=None,
        supports_questions=True,
    ):
        self.conversation_id = conversation_id
        self.call_key = call_key
        self.answer = answer
        self.supports_questions = supports_questions
        self.asked = None

    async def ask_user(
        self, prompt, *, options=None, free_text=False, html=None, text=None
    ):
        if not self.supports_questions:
            raise QuestionsNotSupported()
        self.asked = {
            "prompt": prompt,
            "options": options,
            "free_text": free_text,
            "html": html,
        }
        if self.answer is not None:
            return self.answer
        raise FakeQuestionPending(
            {"prompt": prompt, "html": html, "question_type": "text"}
        )


def _tools_by_name():
    return {tool.name: tool for tool in get_app_edit_tools(FakeAgentTool)}


async def _make_app(datasette, **kwargs):
    kwargs.setdefault("actor_id", "alice")
    kwargs.setdefault("name", "Debug target")
    kwargs.setdefault("description", "")
    kwargs.setdefault("html", "<h1>Hello</h1>")
    return await Registry(datasette).create_stored_app(**kwargs)


async def _run_browser_side(datasette, job_id, envelope):
    claim = await datasette.client.post(
        f"/-/apps/debug/{job_id}/claim", actor={"id": "alice"}
    )
    assert claim.json()["ok"] is True
    result = await datasette.client.post(
        f"/-/apps/debug/{job_id}/result", actor={"id": "alice"}, json=envelope
    )
    assert result.json()["ok"] is True


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
async def test_app_debug_suspends_turn_and_creates_pending_job():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    tools = _tools_by_name()
    context = FakeContext()

    with pytest.raises(FakeQuestionPending):
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

    assert context.asked["free_text"] is True
    assert job["id"] in context.asked["html"]
    assert "debug" in context.asked["prompt"].lower()

    # Re-executing the same suspended call reuses the job instead of
    # creating an orphan row
    with pytest.raises(FakeQuestionPending):
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(),
            app_id=app["id"],
            javascript="return document.title;",
            viewport={"width": 375, "height": 812},
        )
    count = (
        await datasette.get_internal_database().execute(
            "SELECT count(*) AS n FROM _app_debug_jobs"
        )
    ).first()["n"]
    assert count == 1


@pytest.mark.asyncio
async def test_app_debug_returns_result_envelope_after_resume():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    tools = _tools_by_name()

    with pytest.raises(FakeQuestionPending):
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(),
            app_id=app["id"],
            javascript="return document.title;",
        )
    job = await get_debug_job_for_call(datasette, "conv-1", "call-1")
    await _run_browser_side(
        datasette,
        job["id"],
        {
            "ok": True,
            "result": {"title": "Hello"},
            "events": {
                "errors": [{"kind": "console-error", "message": "warned"}],
                "logs": [{"kind": "datasette-call", "message": "datasette.query(...)"}],
            },
            "duration_ms": 321,
            "timed_out": False,
        },
    )

    result = json.loads(
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(answer="completed"),
            app_id=app["id"],
            javascript="return document.title;",
        )
    )
    assert result["ok"] is True
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

    # Resuming created no extra job rows
    count = (
        await datasette.get_internal_database().execute(
            "SELECT count(*) AS n FROM _app_debug_jobs"
        )
    ).first()["n"]
    assert count == 1


@pytest.mark.asyncio
async def test_app_debug_reports_run_that_never_completed():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    tools = _tools_by_name()

    with pytest.raises(FakeQuestionPending):
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(),
            app_id=app["id"],
            javascript="return 1;",
        )

    # The user answered the question by hand; the harness never ran
    result = json.loads(
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(answer="whatever"),
            app_id=app["id"],
            javascript="return 1;",
        )
    )
    assert "did not complete" in result["error"]
    assert result["app_id"] == app["id"]


@pytest.mark.asyncio
async def test_app_debug_requires_interactive_conversation():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(supports_questions=False),
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

    with pytest.raises(FakeQuestionPending):
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(),
            app_id=app["id"],
            javascript="return 1;",
        )
    job = await get_debug_job_for_call(datasette, "conv-1", "call-1")
    errors = [{"kind": "javascript-error", "message": f"error {i}"} for i in range(5)]
    await _run_browser_side(
        datasette,
        job["id"],
        {
            "ok": True,
            "result": {"huge": "x" * 200000},
            "events": {
                "errors": errors,
                "logs": [{"kind": "console-log", "message": "y" * 2000}] * 50,
            },
            "duration_ms": 5,
            "timed_out": False,
        },
    )

    result = json.loads(
        await tools["app_debug"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(answer="completed"),
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
