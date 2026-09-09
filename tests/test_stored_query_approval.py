"""Tests for stored query access grants that need the user's approval,
via context.ask_user(), in the app_create and app_set_stored_queries
agent tools."""

from dataclasses import dataclass
import json

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


class FakeQuestionPending(Exception):
    """Stands in for datasette_agent's QuestionPending - must propagate."""

    def __init__(self, question):
        super().__init__(question["prompt"])
        self.question = question


class QuestionsNotSupported(Exception):
    """Matched by class name, like datasette_agent's exception."""


class FakeContext:
    def __init__(self, *, answer=None, supports_questions=True):
        self.answer = answer
        self.supports_questions = supports_questions
        self.questions = []

    async def ask_user(
        self, prompt, *, options=None, free_text=False, html=None, text=None
    ):
        if not self.supports_questions:
            raise QuestionsNotSupported()
        question = {
            "prompt": prompt,
            "options": options,
            "free_text": free_text,
            "html": html,
            "text": text,
        }
        self.questions.append(question)
        if self.answer is None:
            raise FakeQuestionPending(question)
        return self.answer


def _tools_by_name():
    return {tool.name: tool for tool in get_app_edit_tools(FakeAgentTool)}


async def _datasette_with_queries():
    datasette = Datasette(
        memory=True,
        config={
            "permissions": {
                "create-app": {"id": "alice"},
                "edit-app": {"id": "alice"},
            }
        },
    )
    await datasette.invoke_startup()
    await datasette.add_query(
        "_memory",
        "count_tables",
        "select count(*) as count from sqlite_master",
        title="Count tables",
        description="How many tables are there?",
        source="user",
        owner_id="alice",
    )
    await datasette.add_query(
        "_memory",
        "table_by_name",
        "select * from sqlite_master where name = :name",
        parameters=["name"],
        source="user",
        owner_id="alice",
    )
    await datasette.add_query(
        "_memory",
        "bobs_secret",
        "select 'secret'",
        is_private=True,
        source="user",
        owner_id="bob",
    )
    return datasette


async def _app_count(datasette):
    return (
        await datasette.get_internal_database().execute(
            "SELECT count(*) AS n FROM apps"
        )
    ).first()["n"]


@pytest.mark.asyncio
async def test_app_create_asks_user_to_approve_stored_queries():
    datasette = await _datasette_with_queries()
    tools = _tools_by_name()
    context = FakeContext(answer=True)

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            name="Query app",
            html="<h1>Queries</h1>",
            stored_queries=[
                "_memory/table_by_name",
                "_memory/count_tables",
                " _memory/count_tables ",
            ],
        )
    )

    assert "error" not in result
    assert result["stored_queries"] == ["_memory/count_tables", "_memory/table_by_name"]
    assert await Registry(datasette).get_stored_queries(result["app_id"]) == [
        "_memory/count_tables",
        "_memory/table_by_name",
    ]

    assert len(context.questions) == 1
    question = context.questions[0]
    assert question["prompt"] == "Allow Query app to run 2 stored queries?"
    assert question["options"] is None
    assert question["free_text"] is False
    html = question["html"]
    assert "<strong>Query app</strong>" in html
    assert "<code>_memory/count_tables</code> Count tables" in html
    assert "How many tables are there?" in html
    assert "<pre>select count(*) as count from sqlite_master</pre>" in html
    assert "<code>_memory/table_by_name</code>" in html
    assert "read-only query, parameters: name" in html
    assert "Access will be removed" not in html
    text = question["text"]
    assert "_memory/count_tables - Count tables (read-only query)" in text
    assert "  select count(*) as count from sqlite_master" in text


@pytest.mark.asyncio
async def test_app_create_declined_approval_does_not_create_app():
    datasette = await _datasette_with_queries()
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(answer=False),
            name="Query app",
            html="<h1>Queries</h1>",
            stored_queries=["_memory/count_tables"],
        )
    )

    assert result["ok"] is False
    assert result["cancelled"] is True
    assert "app was not created" in result["message"]
    assert "without stored_queries" in result["message"]
    assert await _app_count(datasette) == 0


@pytest.mark.asyncio
async def test_app_create_suspends_before_creating_app():
    datasette = await _datasette_with_queries()
    tools = _tools_by_name()

    with pytest.raises(FakeQuestionPending):
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(),
            name="Query app",
            html="<h1>Queries</h1>",
            stored_queries=["_memory/count_tables"],
        )
    assert await _app_count(datasette) == 0


@pytest.mark.asyncio
async def test_app_create_without_stored_queries_does_not_ask():
    datasette = await _datasette_with_queries()
    tools = _tools_by_name()
    context = FakeContext()

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            name="Plain app",
            html="<h1>Plain</h1>",
            stored_queries=[],
        )
    )
    assert "error" not in result
    assert result["stored_queries"] == []
    assert context.questions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_queries,expected_error",
    (
        (
            ["_memory/nope"],
            'Stored query "_memory/nope" does not exist or you cannot view it',
        ),
        (
            ["_memory/bobs_secret"],
            'Stored query "_memory/bobs_secret" does not exist or you cannot view it',
        ),
        (
            ["count_tables"],
            'Stored query "count_tables" must be a database/query string',
        ),
        (
            ["_memory/", "_memory/nope"],
            'Stored query "_memory/" must be a database/query string; '
            'Stored query "_memory/nope" does not exist or you cannot view it',
        ),
    ),
)
async def test_app_create_rejects_invalid_stored_queries_before_asking(
    stored_queries, expected_error
):
    datasette = await _datasette_with_queries()
    tools = _tools_by_name()
    context = FakeContext(answer=True)

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            name="Query app",
            html="<h1>Queries</h1>",
            stored_queries=["_memory/count_tables"] + stored_queries,
        )
    )
    assert result == {"error": expected_error}
    assert context.questions == []
    assert await _app_count(datasette) == 0


@pytest.mark.asyncio
async def test_app_create_stored_queries_need_an_interactive_context():
    datasette = await _datasette_with_queries()
    tools = _tools_by_name()

    for context in (None, FakeContext(supports_questions=False)):
        result = json.loads(
            await tools["app_create"].fn(
                datasette=datasette,
                actor={"id": "alice"},
                context=context,
                name="Query app",
                html="<h1>Queries</h1>",
                stored_queries=["_memory/count_tables"],
            )
        )
        assert result["error"].startswith(
            "Granting an app access to stored queries requires the user's approval"
        )
    assert await _app_count(datasette) == 0


@pytest.mark.asyncio
async def test_app_set_stored_queries_asks_only_for_new_grants_and_records_revision():
    datasette = await _datasette_with_queries()
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Existing app",
        description="",
        html="<h1>Existing</h1>",
        stored_queries=["_memory/count_tables"],
    )
    tools = _tools_by_name()
    context = FakeContext(answer=True)

    result = json.loads(
        await tools["app_set_stored_queries"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            stored_queries=["_memory/table_by_name", "_memory/count_tables"],
        )
    )

    assert "error" not in result
    assert result["app_id"] == app["id"]
    assert result["version"] == 2
    assert result["stored_queries"] == ["_memory/count_tables", "_memory/table_by_name"]
    assert result["granted"] == ["_memory/table_by_name"]
    assert result["removed"] == []
    assert result["status"] == (
        "Updated stored query access; saved as app revision v2."
    )
    assert "<strong>Existing app</strong> updated to v2." in result["_html"]
    assert f'href="/-/apps/{app["id"]}"' in result["_html"]

    assert len(context.questions) == 1
    question = context.questions[0]
    assert question["prompt"] == "Allow Existing app to run 1 stored query?"
    assert "_memory/table_by_name" in question["html"]
    # Already-granted queries are not re-approved
    assert "count_tables" not in question["html"]

    assert await registry.get_stored_queries(app["id"]) == [
        "_memory/count_tables",
        "_memory/table_by_name",
    ]
    version = await registry.get_current_version(app["id"])
    assert version["version"] == 2
    assert version["changed_fields"] == ["stored_queries"]
    assert version["actor_id"] == "alice"


@pytest.mark.asyncio
async def test_app_set_stored_queries_shows_removals_alongside_grants():
    datasette = await _datasette_with_queries()
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Existing app",
        description="",
        html="",
        stored_queries=["_memory/count_tables"],
    )
    tools = _tools_by_name()
    context = FakeContext(answer=True)

    result = json.loads(
        await tools["app_set_stored_queries"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            stored_queries=["_memory/table_by_name"],
        )
    )

    assert result["granted"] == ["_memory/table_by_name"]
    assert result["removed"] == ["_memory/count_tables"]
    html = context.questions[0]["html"]
    assert "Access will be removed for: <code>_memory/count_tables</code>" in html
    assert "Access will be removed for: _memory/count_tables" in (
        context.questions[0]["text"]
    )
    assert await registry.get_stored_queries(app["id"]) == ["_memory/table_by_name"]


@pytest.mark.asyncio
async def test_app_set_stored_queries_removal_only_does_not_ask():
    datasette = await _datasette_with_queries()
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Existing app",
        description="",
        html="",
        stored_queries=["_memory/count_tables", "_memory/table_by_name"],
    )
    tools = _tools_by_name()
    context = FakeContext()

    result = json.loads(
        await tools["app_set_stored_queries"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            stored_queries=[],
        )
    )

    assert "error" not in result
    assert result["granted"] == []
    assert result["removed"] == ["_memory/count_tables", "_memory/table_by_name"]
    assert result["version"] == 2
    assert context.questions == []
    assert await registry.get_stored_queries(app["id"]) == []


@pytest.mark.asyncio
async def test_app_set_stored_queries_no_change_is_a_no_op():
    datasette = await _datasette_with_queries()
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Existing app",
        description="",
        html="",
        stored_queries=["_memory/count_tables"],
    )
    tools = _tools_by_name()
    context = FakeContext()

    result = json.loads(
        await tools["app_set_stored_queries"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            stored_queries=["_memory/count_tables", "_memory/count_tables"],
        )
    )

    assert result == {
        "app_id": app["id"],
        "stored_queries": ["_memory/count_tables"],
        "status": "No change: the app already had exactly these stored queries.",
    }
    assert context.questions == []
    assert (await registry.get_current_version(app["id"]))["version"] == 1


@pytest.mark.asyncio
async def test_app_set_stored_queries_declined_leaves_app_unchanged():
    datasette = await _datasette_with_queries()
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Existing app",
        description="",
        html="",
        stored_queries=["_memory/count_tables"],
    )
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_set_stored_queries"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(answer=False),
            app_id=app["id"],
            stored_queries=["_memory/table_by_name"],
        )
    )

    assert result["ok"] is False
    assert result["cancelled"] is True
    assert result["app_id"] == app["id"]
    assert "left unchanged" in result["message"]
    assert await registry.get_stored_queries(app["id"]) == ["_memory/count_tables"]
    assert (await registry.get_current_version(app["id"]))["version"] == 1


@pytest.mark.asyncio
async def test_app_set_stored_queries_requires_edit_permission_and_stored_app():
    datasette = await _datasette_with_queries()
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Alice's app",
        description="",
        html="",
    )
    await registry.add_app(
        id="plugin:external",
        name="External app",
        description="",
        path="/-/external",
        source="plugin",
    )
    tools = _tools_by_name()
    context = FakeContext(answer=True)

    result = json.loads(
        await tools["app_set_stored_queries"].fn(
            datasette=datasette,
            actor={"id": "bob"},
            context=context,
            app_id=app["id"],
            stored_queries=["_memory/count_tables"],
        )
    )
    assert result == {"error": "Permission denied: edit-app", "app_id": app["id"]}

    result = json.loads(
        await tools["app_set_stored_queries"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id="plugin:external",
            stored_queries=["_memory/count_tables"],
        )
    )
    assert result == {
        "error": "Permission denied: edit-app",
        "app_id": "plugin:external",
    }

    result = json.loads(
        await tools["app_set_stored_queries"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            stored_queries=["_memory/bobs_secret"],
        )
    )
    assert result == {
        "error": 'Stored query "_memory/bobs_secret" does not exist or you cannot view it',
        "app_id": app["id"],
    }
    assert context.questions == []


@pytest.mark.asyncio
async def test_app_create_with_real_datasette_agent_tool_context(tmp_path):
    """Integration against the real question runtime, when datasette-agent
    is installed: the first call suspends with QuestionPending and creates
    nothing, and the replay after the user answers creates the app."""
    pytest.importorskip("datasette_agent")
    from datasette_agent.questions import QuestionPending
    from datasette_agent.schema import ensure_tables
    from datasette_agent.tools import ToolContext

    datasette = Datasette(
        memory=True,
        internal=str(tmp_path / "internal.db"),
        config={"permissions": {"create-app": {"id": "alice"}}},
    )
    await datasette.invoke_startup()
    await ensure_tables(datasette.get_internal_database())
    await datasette.add_query(
        "_memory",
        "count_tables",
        "select count(*) as count from sqlite_master",
        source="user",
        owner_id="alice",
    )
    tools = _tools_by_name()
    arguments = {
        "name": "Query app",
        "html": "<h1>Queries</h1>",
        "stored_queries": ["_memory/count_tables"],
    }

    def make_context():
        return ToolContext(
            datasette=datasette,
            actor={"id": "alice"},
            conversation_id="01CONVERSATION00000000TEST",
            tool_name="app_create",
            arguments=arguments,
            tool_call_id="call_1",
            supports_questions=True,
        )

    with pytest.raises(QuestionPending) as excinfo:
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=make_context(),
            **arguments,
        )
    question = excinfo.value.question
    assert question["question_type"] == "boolean"
    assert question["prompt"] == "Allow Query app to run 1 stored query?"
    assert "<pre>select count(*) as count from sqlite_master</pre>" in question["html"]
    assert await _app_count(datasette) == 0

    db = datasette.get_internal_database()
    await db.execute_write(
        "UPDATE agent_questions SET status = 'answered', answer_json = ? WHERE id = ?",
        [json.dumps(True), question["id"]],
    )

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=make_context(),
            **arguments,
        )
    )
    assert "error" not in result
    assert result["stored_queries"] == ["_memory/count_tables"]
    assert await Registry(datasette).get_stored_queries(result["app_id"]) == [
        "_memory/count_tables"
    ]
    assert await _app_count(datasette) == 1
