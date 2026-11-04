"""Tests for granting stored queries to apps from agent tools.

Read-only queries are granted without prompting; write queries require
approval via context.ask_user().
"""

from dataclasses import dataclass
import json
import sqlite3

import pytest
from datasette.app import Datasette

from datasette_apps import Registry
from datasette_apps.agent_tools import get_app_edit_tools

ALICE = {"id": "alice"}


@dataclass
class FakeAgentTool:
    name: str
    description: str
    input_schema: dict
    fn: object
    required_permission: str | None = None


class FakeQuestionPending(Exception):
    """Stands in for datasette_agent's QuestionPending - must propagate."""


class QuestionsNotSupported(Exception):
    """Matched by class name, like datasette_agent's exception."""


class FakeContext:
    def __init__(self, answer=None, supports_questions=True):
        self.answer = answer
        self.supports_questions = supports_questions
        self.questions = []

    async def ask_user(self, prompt, *, html=None, text=None, **kwargs):
        if not self.supports_questions:
            raise QuestionsNotSupported()
        self.questions.append({"prompt": prompt, "html": html, "text": text})
        if self.answer is None:
            raise FakeQuestionPending(prompt)
        return self.answer


def _tools():
    return {tool.name: tool for tool in get_app_edit_tools(FakeAgentTool)}


async def _setup(tmp_path, config=None):
    db_path = tmp_path / "content.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "create table news (id integer primary key, title text);"
        "insert into news (title) values ('First');"
    )
    conn.close()
    datasette = Datasette(
        [str(db_path)],
        config=config
        or {
            "permissions": {
                "create-app": {"id": "alice"},
                "edit-app": {"id": "alice"},
            }
        },
    )
    await datasette.invoke_startup()
    await datasette.add_query(
        "content", "news_by_id", "select title from news where id = :id"
    )
    await datasette.add_query(
        "content",
        "add_news",
        "insert into news (title) values (:title)",
        title="Add news",
        is_write=True,
    )
    await datasette.add_query(
        "content",
        "delete_news",
        "delete from news where id = :id",
        is_write=True,
    )
    app = await Registry(datasette).create_stored_app(
        actor_id="alice", name="News app", description="", html=""
    )
    return datasette, app


@pytest.mark.asyncio
async def test_add_read_only_query_does_not_prompt(tmp_path):
    datasette, app = await _setup(tmp_path)
    context = FakeContext()
    result = json.loads(
        await _tools()["app_add_stored_queries"].fn(
            datasette=datasette,
            actor=ALICE,
            context=context,
            app_id=app["id"],
            stored_queries=["content/news_by_id"],
        )
    )
    assert result == {
        "app_id": app["id"],
        "added": ["content/news_by_id"],
        "stored_queries": ["content/news_by_id"],
    }
    assert context.questions == []
    assert await Registry(datasette).get_stored_queries(app["id"]) == [
        "content/news_by_id"
    ]


@pytest.mark.asyncio
async def test_add_write_query_asks_once_and_saves_nothing_until_answered(
    tmp_path,
):
    datasette, app = await _setup(tmp_path)
    context = FakeContext()
    with pytest.raises(FakeQuestionPending):
        await _tools()["app_add_stored_queries"].fn(
            datasette=datasette,
            actor=ALICE,
            context=context,
            app_id=app["id"],
            stored_queries=[
                "content/news_by_id",
                "content/add_news",
                "content/delete_news",
            ],
        )
    # Nothing saved - not even the read-only query - while pending
    assert await Registry(datasette).get_stored_queries(app["id"]) == []
    assert len(context.questions) == 1
    question = context.questions[0]
    assert question["prompt"] == 'Allow app "News app" to run 2 write queries?'
    assert "insert into news (title) values (:title)" in question["html"]
    assert "delete from news where id = :id" in question["html"]
    assert "Add news" in question["html"]
    assert "news_by_id" not in question["html"]
    assert "delete from news where id = :id" in question["text"]


@pytest.mark.asyncio
async def test_add_write_query_approved(tmp_path):
    datasette, app = await _setup(tmp_path)
    context = FakeContext(answer=True)
    result = json.loads(
        await _tools()["app_add_stored_queries"].fn(
            datasette=datasette,
            actor=ALICE,
            context=context,
            app_id=app["id"],
            stored_queries=["content/add_news", "content/news_by_id"],
        )
    )
    assert context.questions[0]["prompt"] == (
        'Allow app "News app" to run write query "content/add_news"?'
    )
    assert result["stored_queries"] == ["content/add_news", "content/news_by_id"]
    registry = Registry(datasette)
    assert await registry.get_stored_queries(app["id"]) == [
        "content/add_news",
        "content/news_by_id",
    ]
    latest = (await registry.list_versions(app["id"]))[0]
    assert latest["version"] == 2
    assert latest["changed_fields"] == ["stored_queries"]


@pytest.mark.asyncio
async def test_add_write_query_declined(tmp_path):
    datasette, app = await _setup(tmp_path)
    result = json.loads(
        await _tools()["app_add_stored_queries"].fn(
            datasette=datasette,
            actor=ALICE,
            context=FakeContext(answer=False),
            app_id=app["id"],
            stored_queries=["content/add_news", "content/news_by_id"],
        )
    )
    assert result["cancelled"] is True
    assert "Do not retry" in result["message"]
    assert await Registry(datasette).get_stored_queries(app["id"]) == []


@pytest.mark.asyncio
async def test_already_granted_write_query_does_not_prompt_again(tmp_path):
    datasette, app = await _setup(tmp_path)
    await Registry(datasette).set_stored_queries(app["id"], ["content/add_news"])
    context = FakeContext()
    result = json.loads(
        await _tools()["app_add_stored_queries"].fn(
            datasette=datasette,
            actor=ALICE,
            context=context,
            app_id=app["id"],
            stored_queries=["content/add_news", "content/news_by_id"],
        )
    )
    assert context.questions == []
    assert result["added"] == ["content/news_by_id"]


@pytest.mark.asyncio
async def test_write_query_without_question_support_errors(tmp_path):
    datasette, app = await _setup(tmp_path)
    result = json.loads(
        await _tools()["app_add_stored_queries"].fn(
            datasette=datasette,
            actor=ALICE,
            context=FakeContext(supports_questions=False),
            app_id=app["id"],
            stored_queries=["content/add_news"],
        )
    )
    assert "requires user approval" in result["error"]
    assert await Registry(datasette).get_stored_queries(app["id"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    ["content/missing", "missing/news_by_id", "content", "/news_by_id"],
)
async def test_add_rejects_unknown_or_invalid_queries(tmp_path, key):
    datasette, app = await _setup(tmp_path)
    result = json.loads(
        await _tools()["app_add_stored_queries"].fn(
            datasette=datasette,
            actor=ALICE,
            context=FakeContext(),
            app_id=app["id"],
            stored_queries=["content/news_by_id", key],
        )
    )
    assert "error" in result
    assert await Registry(datasette).get_stored_queries(app["id"]) == []


@pytest.mark.asyncio
async def test_add_rejects_query_actor_cannot_view(tmp_path):
    datasette, app = await _setup(
        tmp_path,
        config={
            "permissions": {"edit-app": {"id": "alice"}},
            "databases": {
                "content": {
                    "permissions": {"view-query": {"id": "bob"}},
                }
            },
        },
    )
    result = json.loads(
        await _tools()["app_add_stored_queries"].fn(
            datasette=datasette,
            actor=ALICE,
            context=FakeContext(answer=True),
            app_id=app["id"],
            stored_queries=["content/news_by_id"],
        )
    )
    assert result["error"] == "Stored query not found: content/news_by_id"


@pytest.mark.asyncio
async def test_add_and_remove_require_edit_permission(tmp_path):
    datasette, app = await _setup(tmp_path)
    tools = _tools()
    added = json.loads(
        await tools["app_add_stored_queries"].fn(
            datasette=datasette,
            actor={"id": "mallory"},
            context=FakeContext(answer=True),
            app_id=app["id"],
            stored_queries=["content/news_by_id"],
        )
    )
    removed = json.loads(
        await tools["app_remove_stored_queries"].fn(
            datasette=datasette,
            actor={"id": "mallory"},
            app_id=app["id"],
            stored_queries=["content/news_by_id"],
        )
    )
    assert added["error"] == "Permission denied: edit-app"
    assert removed["error"] == "Permission denied: edit-app"


@pytest.mark.asyncio
async def test_remove_stored_queries_never_prompts(tmp_path):
    datasette, app = await _setup(tmp_path)
    registry = Registry(datasette)
    await registry.set_stored_queries(
        app["id"], ["content/add_news", "content/news_by_id"]
    )
    result = json.loads(
        await _tools()["app_remove_stored_queries"].fn(
            datasette=datasette,
            actor=ALICE,
            app_id=app["id"],
            stored_queries=["content/add_news", "content/not_granted"],
        )
    )
    assert result == {
        "app_id": app["id"],
        "removed": ["content/add_news"],
        "stored_queries": ["content/news_by_id"],
    }
    assert await registry.get_stored_queries(app["id"]) == ["content/news_by_id"]


@pytest.mark.asyncio
async def test_app_create_read_only_query_does_not_prompt(tmp_path):
    datasette, _ = await _setup(tmp_path)
    context = FakeContext()
    result = json.loads(
        await _tools()["app_create"].fn(
            datasette=datasette,
            actor=ALICE,
            context=context,
            name="Reader",
            html="<p>hi</p>",
            stored_queries=["content/news_by_id"],
        )
    )
    assert context.questions == []
    assert await Registry(datasette).get_stored_queries(result["app_id"]) == [
        "content/news_by_id"
    ]


@pytest.mark.asyncio
async def test_app_create_write_query_requires_approval(tmp_path):
    datasette, _ = await _setup(tmp_path)
    registry = Registry(datasette)
    before = len(await registry.list_apps())
    context = FakeContext()
    with pytest.raises(FakeQuestionPending):
        await _tools()["app_create"].fn(
            datasette=datasette,
            actor=ALICE,
            context=context,
            name="Writer",
            html="<p>hi</p>",
            stored_queries=["content/add_news"],
        )
    assert context.questions[0]["prompt"] == (
        'Allow app "Writer" to run write query "content/add_news"?'
    )
    # No app is created while approval is pending
    assert len(await registry.list_apps()) == before

    declined = json.loads(
        await _tools()["app_create"].fn(
            datasette=datasette,
            actor=ALICE,
            context=FakeContext(answer=False),
            name="Writer",
            html="<p>hi</p>",
            stored_queries=["content/add_news"],
        )
    )
    assert declined["cancelled"] is True
    assert len(await registry.list_apps()) == before

    created = json.loads(
        await _tools()["app_create"].fn(
            datasette=datasette,
            actor=ALICE,
            context=FakeContext(answer=True),
            name="Writer",
            html="<p>hi</p>",
            stored_queries=["content/add_news"],
        )
    )
    assert await registry.get_stored_queries(created["app_id"]) == ["content/add_news"]


@pytest.mark.asyncio
async def test_app_create_rejects_unknown_stored_query(tmp_path):
    datasette, _ = await _setup(tmp_path)
    result = json.loads(
        await _tools()["app_create"].fn(
            datasette=datasette,
            actor=ALICE,
            name="Broken",
            html="<p>hi</p>",
            stored_queries=["content/missing"],
        )
    )
    assert result == {"error": "Stored query not found: content/missing"}


@pytest.mark.asyncio
async def test_add_write_query_with_real_datasette_agent_tool_context(tmp_path):
    """The real ask_user() runtime: first call suspends with a pending
    question, answering it lets the re-executed call save the grant."""
    pytest.importorskip("datasette_agent")
    from datasette_agent.questions import QuestionPending
    from datasette_agent.schema import ensure_tables
    from datasette_agent.tools import ToolContext

    datasette, app = await _setup(tmp_path)
    internal = datasette.get_internal_database()
    await ensure_tables(internal)
    arguments = {"app_id": app["id"], "stored_queries": ["content/add_news"]}

    def make_context():
        return ToolContext(
            datasette=datasette,
            actor=ALICE,
            conversation_id="01CONVERSATION00000000TEST",
            tool_name="app_add_stored_queries",
            arguments=arguments,
            tool_call_id="call_1",
            supports_questions=True,
        )

    tool = _tools()["app_add_stored_queries"]
    with pytest.raises(QuestionPending) as excinfo:
        await tool.fn(
            datasette=datasette, actor=ALICE, context=make_context(), **arguments
        )
    question = excinfo.value.question
    assert "insert into news" in question["html"]
    assert await Registry(datasette).get_stored_queries(app["id"]) == []

    await internal.execute_write(
        "UPDATE agent_questions SET status = 'answered', answer_json = ? "
        "WHERE id = ?",
        [json.dumps(True), question["id"]],
    )
    result = json.loads(
        await tool.fn(
            datasette=datasette, actor=ALICE, context=make_context(), **arguments
        )
    )
    assert result["added"] == ["content/add_news"]
    assert await Registry(datasette).get_stored_queries(app["id"]) == [
        "content/add_news"
    ]
