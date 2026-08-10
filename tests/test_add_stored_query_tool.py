"""Tests for the app_add_stored_query agent tool, built on context.ask_user()."""

from dataclasses import dataclass
import json
import sqlite3

import pytest
from datasette.app import Datasette

from datasette_apps import Registry
from datasette_apps.agent_tools import get_app_edit_tools


@dataclass
class FakeAgentTool:
    name: str
    description: str
    input_schema: dict
    fn: object
    required_permission: str | None = None


class QuestionsNotSupported(Exception):
    """Matched by class name, like datasette_agent's exception."""


class FakeContext:
    def __init__(self, *, answer=True, supports_questions=True):
        self.answer = answer
        self.supports_questions = supports_questions
        self.questions = []

    async def ask_user(self, prompt, *, options=None, free_text=False, html=None):
        if not self.supports_questions:
            raise QuestionsNotSupported()
        self.questions.append({"prompt": prompt, "html": html})
        return self.answer


def _tool():
    return {tool.name: tool for tool in get_app_edit_tools(FakeAgentTool)}[
        "app_add_stored_query"
    ]


def create_database(tmp_path):
    db_path = tmp_path / "content.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        create table news (id integer primary key, title text);
        insert into news (title) values ('First'), ('Second');
        """)
    conn.close()
    return db_path


async def _make_datasette(tmp_path, **kwargs):
    datasette = Datasette([str(create_database(tmp_path))], **kwargs)
    await datasette.invoke_startup()
    await datasette.add_query(
        "content",
        "news_by_id",
        "select title from news where id = :id",
        title="News by ID",
        source="user",
        owner_id="alice",
    )
    return datasette


async def _make_app(datasette, **kwargs):
    kwargs.setdefault("actor_id", "alice")
    kwargs.setdefault("name", "News app")
    kwargs.setdefault("description", "")
    kwargs.setdefault("html", "")
    return await Registry(datasette).create_stored_app(**kwargs)


@pytest.mark.asyncio
async def test_app_add_stored_query_tool_is_registered():
    tool = _tool()
    assert tool.input_schema["required"] == ["app_id", "database", "query", "rationale"]
    assert set(tool.input_schema["properties"]) == {
        "app_id",
        "database",
        "query",
        "rationale",
    }
    assert "rationale" in tool.description
    assert "approve" in tool.description
    assert "datasette.storedQuery" in tool.description


@pytest.mark.asyncio
async def test_app_add_stored_query_requires_edit_permission(tmp_path):
    datasette = await _make_datasette(
        tmp_path, config={"permissions": {"edit-app": {"id": "alice"}}}
    )
    app = await _make_app(datasette)
    context = FakeContext()

    result = json.loads(
        await _tool().fn(
            datasette=datasette,
            actor={"id": "bob"},
            context=context,
            app_id=app["id"],
            database="content",
            query="news_by_id",
            rationale="Show news items",
        )
    )

    assert result == {"error": "Permission denied: edit-app", "app_id": app["id"]}
    assert context.questions == []


@pytest.mark.asyncio
async def test_app_add_stored_query_missing_query_errors_without_asking(tmp_path):
    datasette = await _make_datasette(tmp_path)
    app = await _make_app(datasette)
    context = FakeContext()

    result = json.loads(
        await _tool().fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            database="content",
            query="nope",
            rationale="Show news items",
        )
    )

    assert result["error"] == "Stored query not found: content/nope"
    assert context.questions == []


@pytest.mark.asyncio
async def test_app_add_stored_query_denies_query_actor_cannot_view(tmp_path):
    datasette = await _make_datasette(tmp_path)
    await datasette.add_query(
        "content",
        "secret",
        "select 1",
        source="user",
        owner_id="bob",
        is_private=True,
    )
    app = await _make_app(datasette)
    context = FakeContext()

    result = json.loads(
        await _tool().fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            database="content",
            query="secret",
            rationale="Peek at secrets",
        )
    )

    assert result["error"] == (
        "Permission denied: you cannot view the stored query content/secret"
    )
    assert context.questions == []
    assert await Registry(datasette).get_stored_queries(app["id"]) == []


@pytest.mark.asyncio
async def test_app_add_stored_query_approved_assigns_query(tmp_path):
    datasette = await _make_datasette(tmp_path)
    app = await _make_app(datasette)
    context = FakeContext(answer=True)

    result = json.loads(
        await _tool().fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            database="content",
            query="news_by_id",
            rationale="The app lists news stories by their ID",
        )
    )

    assert result["ok"] is True
    assert result["stored_query"] == "content/news_by_id"
    assert result["stored_queries"] == ["content/news_by_id"]
    assert "datasette.storedQuery" in result["message"]
    assert app["path"] in result["_html"]

    registry = Registry(datasette)
    assert await registry.get_stored_queries(app["id"]) == ["content/news_by_id"]
    # The change is recorded as a revision attributed to the actor
    version = await registry.get_current_version(app["id"])
    assert version["changed_fields"] == ["stored_queries"]
    assert version["actor_id"] == "alice"

    # The user saw the query details and the agent's rationale
    (question,) = context.questions
    assert question["prompt"] == (
        'Allow app "News app" to run the stored query "content/news_by_id"?'
    )
    assert "News by ID" in question["html"]
    assert "read-only query" in question["html"]
    assert "The app lists news stories by their ID" in question["html"]
    assert "select title from news where id = :id" in question["html"]


@pytest.mark.asyncio
async def test_app_add_stored_query_declined_makes_no_changes(tmp_path):
    datasette = await _make_datasette(tmp_path)
    app = await _make_app(datasette)
    context = FakeContext(answer=False)

    result = json.loads(
        await _tool().fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            database="content",
            query="news_by_id",
            rationale="Show news items",
        )
    )

    assert result["ok"] is False
    assert result["cancelled"] is True
    assert "declined" in result["message"]
    registry = Registry(datasette)
    assert await registry.get_stored_queries(app["id"]) == []
    version = await registry.get_current_version(app["id"])
    assert version["version"] == 1


@pytest.mark.asyncio
async def test_app_add_stored_query_already_assigned_skips_approval(tmp_path):
    datasette = await _make_datasette(tmp_path)
    app = await _make_app(datasette, stored_queries=["content/news_by_id"])
    context = FakeContext()

    result = json.loads(
        await _tool().fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            database="content",
            query="news_by_id",
            rationale="Show news items",
        )
    )

    assert result["ok"] is True
    assert result["message"] == "That stored query is already assigned to this app."
    assert context.questions == []


@pytest.mark.asyncio
async def test_app_add_stored_query_rationale_is_escaped(tmp_path):
    datasette = await _make_datasette(tmp_path)
    app = await _make_app(datasette)
    context = FakeContext(answer=False)

    await _tool().fn(
        datasette=datasette,
        actor={"id": "alice"},
        context=context,
        app_id=app["id"],
        database="content",
        query="news_by_id",
        rationale='<script>alert("boom")</script>',
    )

    (question,) = context.questions
    assert "<script>" not in question["html"]
    assert "&lt;script&gt;" in question["html"]


@pytest.mark.asyncio
async def test_app_add_stored_query_without_questions_support(tmp_path):
    datasette = await _make_datasette(tmp_path)
    app = await _make_app(datasette)
    context = FakeContext(supports_questions=False)

    result = json.loads(
        await _tool().fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            database="content",
            query="news_by_id",
            rationale="Show news items",
        )
    )

    assert "interactive conversation" in result["error"]
    assert await Registry(datasette).get_stored_queries(app["id"]) == []


@pytest.mark.asyncio
async def test_app_add_stored_query_missing_app(tmp_path):
    datasette = await _make_datasette(tmp_path)
    context = FakeContext()

    result = json.loads(
        await _tool().fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id="nosuchapp",
            database="content",
            query="news_by_id",
            rationale="Show news items",
        )
    )

    # Unknown apps fail the edit-app permission check, like the other tools
    assert result["error"] == "Permission denied: edit-app"
    assert context.questions == []
