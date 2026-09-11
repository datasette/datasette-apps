"""Tests for CSP origin grants that need the user's approval, via
context.ask_user(), in the app_create and app_set_csp_origins agent tools."""

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
    def __init__(self, *, answers=(), supports_questions=True):
        self.answers = list(answers)
        self.supports_questions = supports_questions
        self.questions = []

    async def ask_user(
        self, prompt, *, options=None, free_text=False, html=None, text=None
    ):
        if not self.supports_questions:
            raise QuestionsNotSupported()
        question = {"prompt": prompt, "options": options, "html": html, "text": text}
        self.questions.append(question)
        if not self.answers:
            raise FakeQuestionPending(question)
        return self.answers.pop(0)


def _tools_by_name(datasette=None):
    return {tool.name: tool for tool in get_app_edit_tools(FakeAgentTool, datasette)}


async def _datasette(**extra_permissions):
    permissions = {"create-app": {"id": "alice"}, "edit-app": {"id": "alice"}}
    permissions.update(extra_permissions)
    datasette = Datasette(
        memory=True,
        config={
            "plugins": {
                "datasette-apps": {"allowed_csp_origins": ["cdn.jsdelivr.net"]}
            },
            "permissions": permissions,
        },
    )
    await datasette.invoke_startup()
    return datasette


async def _app_count(datasette):
    return (
        await datasette.get_internal_database().execute(
            "SELECT count(*) AS n FROM apps"
        )
    ).first()["n"]


async def _make_app(datasette, **kwargs):
    kwargs.setdefault("actor_id", "alice")
    kwargs.setdefault("name", "Existing app")
    kwargs.setdefault("description", "")
    kwargs.setdefault("html", "<h1>Existing</h1>")
    return await Registry(datasette).create_stored_app(**kwargs)


@pytest.mark.asyncio
async def test_app_create_asks_user_to_approve_csp_origins():
    datasette = await _datasette()
    tools = _tools_by_name()
    context = FakeContext(answers=[True])

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            name="CDN app",
            html="<h1>Hi</h1>",
            csp_origins=["https://cdn.jsdelivr.net", "https://CDN.jsdelivr.net/"],
        )
    )

    assert "error" not in result
    assert result["csp_origins"] == ["https://cdn.jsdelivr.net"]
    assert await Registry(datasette).get_csp_origins(result["app_id"]) == [
        "https://cdn.jsdelivr.net"
    ]

    assert len(context.questions) == 1
    question = context.questions[0]
    assert question["prompt"] == "Allow CDN app to contact 1 external origin?"
    assert question["options"] is None
    html = question["html"]
    assert (
        "<strong>CDN app</strong> will be allowed to contact 1 external origin" in html
    )
    assert "<li><code>https://cdn.jsdelivr.net</code></li>" in html
    assert "could send data it can read to them" in html
    assert "Access will be removed" not in html
    text = question["text"]
    assert "  https://cdn.jsdelivr.net" in text
    assert "could send data it can read to them" in text


@pytest.mark.asyncio
async def test_app_create_asks_about_stored_queries_then_origins():
    datasette = await _datasette()
    await datasette.add_query(
        "_memory",
        "count_tables",
        "select count(*) from sqlite_master",
        source="user",
        owner_id="alice",
    )
    tools = _tools_by_name()
    context = FakeContext(answers=[True, True])

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            name="Both app",
            html="<h1>Hi</h1>",
            stored_queries=["_memory/count_tables"],
            csp_origins=["https://cdn.jsdelivr.net"],
        )
    )

    assert "error" not in result
    assert [q["prompt"] for q in context.questions] == [
        "Allow Both app to run 1 stored query?",
        "Allow Both app to contact 1 external origin?",
    ]
    assert result["stored_queries"] == ["_memory/count_tables"]
    assert result["csp_origins"] == ["https://cdn.jsdelivr.net"]


@pytest.mark.asyncio
async def test_app_create_declined_origins_does_not_create_app():
    datasette = await _datasette()
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(answers=[False]),
            name="CDN app",
            html="<h1>Hi</h1>",
            csp_origins=["https://cdn.jsdelivr.net"],
        )
    )

    assert result["ok"] is False
    assert result["cancelled"] is True
    assert "app was not created" in result["message"]
    assert "without csp_origins" in result["message"]
    assert await _app_count(datasette) == 0


@pytest.mark.asyncio
async def test_app_create_suspends_on_origins_before_creating_app():
    datasette = await _datasette()
    tools = _tools_by_name()

    with pytest.raises(FakeQuestionPending):
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(),
            name="CDN app",
            html="<h1>Hi</h1>",
            csp_origins=["https://cdn.jsdelivr.net"],
        )
    assert await _app_count(datasette) == 0


@pytest.mark.asyncio
async def test_app_create_rejects_disallowed_origin_before_asking():
    datasette = await _datasette()
    tools = _tools_by_name()
    context = FakeContext(answers=[True])

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            name="Sneaky app",
            html="<h1>Hi</h1>",
            csp_origins=["https://attacker.example.com"],
        )
    )
    assert "https://attacker.example.com" in result["error"]
    assert "apps-set-csp" in result["error"]
    assert context.questions == []
    assert await _app_count(datasette) == 0


@pytest.mark.asyncio
async def test_app_create_csp_origins_need_an_interactive_context():
    datasette = await _datasette()
    tools = _tools_by_name()

    for context in (None, FakeContext(supports_questions=False)):
        result = json.loads(
            await tools["app_create"].fn(
                datasette=datasette,
                actor={"id": "alice"},
                context=context,
                name="CDN app",
                html="<h1>Hi</h1>",
                csp_origins=["https://cdn.jsdelivr.net"],
            )
        )
        assert result["error"].startswith(
            "Allowing an app to contact external origins requires the user's approval"
        )
    assert await _app_count(datasette) == 0


@pytest.mark.asyncio
async def test_app_set_csp_origins_asks_only_for_new_origins_and_records_revision():
    datasette = await _datasette(**{"apps-set-csp": {"id": "alice"}})
    registry = Registry(datasette)
    app = await _make_app(datasette, csp_origins=["https://cdn.jsdelivr.net"])
    tools = _tools_by_name()
    context = FakeContext(answers=[True])

    result = json.loads(
        await tools["app_set_csp_origins"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            csp_origins=["https://api.github.com", "https://cdn.jsdelivr.net"],
        )
    )

    assert "error" not in result
    assert result["app_id"] == app["id"]
    assert result["version"] == 2
    assert result["csp_origins"] == [
        "https://api.github.com",
        "https://cdn.jsdelivr.net",
    ]
    assert result["added"] == ["https://api.github.com"]
    assert result["removed"] == []
    assert result["status"] == "Updated allowed origins; saved as app revision v2."
    assert "<strong>Existing app</strong> updated to v2." in result["_html"]
    assert f'href="/-/apps/{app["id"]}"' in result["_html"]

    assert len(context.questions) == 1
    question = context.questions[0]
    assert question["prompt"] == "Allow Existing app to contact 1 external origin?"
    assert "https://api.github.com" in question["html"]
    # Already-allowed origins are not re-approved
    assert "jsdelivr" not in question["html"]

    assert await registry.get_csp_origins(app["id"]) == [
        "https://api.github.com",
        "https://cdn.jsdelivr.net",
    ]
    version = await registry.get_current_version(app["id"])
    assert version["version"] == 2
    assert version["changed_fields"] == ["csp_origins"]
    assert version["actor_id"] == "alice"


@pytest.mark.asyncio
async def test_app_set_csp_origins_shows_removals_alongside_additions():
    datasette = await _datasette(**{"apps-set-csp": {"id": "alice"}})
    app = await _make_app(datasette, csp_origins=["https://old.example.com"])
    tools = _tools_by_name()
    context = FakeContext(answers=[True])

    result = json.loads(
        await tools["app_set_csp_origins"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            csp_origins=["https://new.example.com"],
        )
    )

    assert result["added"] == ["https://new.example.com"]
    assert result["removed"] == ["https://old.example.com"]
    html = context.questions[0]["html"]
    assert "Access will be removed for: <code>https://old.example.com</code>" in html
    assert "Access will be removed for: https://old.example.com" in (
        context.questions[0]["text"]
    )
    assert await Registry(datasette).get_csp_origins(app["id"]) == [
        "https://new.example.com"
    ]


@pytest.mark.asyncio
async def test_app_set_csp_origins_removal_only_does_not_ask():
    datasette = await _datasette()
    app = await _make_app(
        datasette,
        csp_origins=["https://cdn.jsdelivr.net", "https://api.github.com"],
    )
    tools = _tools_by_name()
    context = FakeContext()

    result = json.loads(
        await tools["app_set_csp_origins"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            csp_origins=[],
        )
    )

    assert "error" not in result
    assert result["added"] == []
    assert result["removed"] == ["https://api.github.com", "https://cdn.jsdelivr.net"]
    assert result["version"] == 2
    assert context.questions == []
    assert await Registry(datasette).get_csp_origins(app["id"]) == []


@pytest.mark.asyncio
async def test_app_set_csp_origins_keeps_existing_origin_outside_allowlist():
    """Alice lacks apps-set-csp, but an origin the app already has (set
    earlier by a privileged user) can be kept, matching the edit page."""
    datasette = await _datasette()
    app = await _make_app(datasette, csp_origins=["https://api.github.com"])
    tools = _tools_by_name()
    context = FakeContext(answers=[True])

    result = json.loads(
        await tools["app_set_csp_origins"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            csp_origins=["https://api.github.com", "https://cdn.jsdelivr.net"],
        )
    )
    assert "error" not in result
    assert result["added"] == ["https://cdn.jsdelivr.net"]
    assert result["removed"] == []

    result = json.loads(
        await tools["app_set_csp_origins"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(answers=[True]),
            app_id=app["id"],
            csp_origins=["https://api.github.com", "https://attacker.example.com"],
        )
    )
    assert "https://attacker.example.com" in result["error"]
    assert "https://api.github.com" not in result["error"]
    assert result["app_id"] == app["id"]


@pytest.mark.asyncio
async def test_app_set_csp_origins_no_change_is_a_no_op():
    datasette = await _datasette()
    registry = Registry(datasette)
    app = await _make_app(datasette, csp_origins=["https://cdn.jsdelivr.net"])
    tools = _tools_by_name()
    context = FakeContext()

    result = json.loads(
        await tools["app_set_csp_origins"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            csp_origins=["https://cdn.jsdelivr.net/", "https://cdn.jsdelivr.net"],
        )
    )

    assert result == {
        "app_id": app["id"],
        "csp_origins": ["https://cdn.jsdelivr.net"],
        "status": "No change: the app already had exactly these origins.",
    }
    assert context.questions == []
    assert (await registry.get_current_version(app["id"]))["version"] == 1


@pytest.mark.asyncio
async def test_app_set_csp_origins_declined_leaves_app_unchanged():
    datasette = await _datasette()
    registry = Registry(datasette)
    app = await _make_app(datasette)
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_set_csp_origins"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=FakeContext(answers=[False]),
            app_id=app["id"],
            csp_origins=["https://cdn.jsdelivr.net"],
        )
    )

    assert result["ok"] is False
    assert result["cancelled"] is True
    assert result["app_id"] == app["id"]
    assert "left unchanged" in result["message"]
    assert await registry.get_csp_origins(app["id"]) == []
    assert (await registry.get_current_version(app["id"]))["version"] == 1


@pytest.mark.asyncio
async def test_app_set_csp_origins_requires_edit_permission_and_valid_origins():
    datasette = await _datasette()
    registry = Registry(datasette)
    app = await _make_app(datasette)
    await registry.add_app(
        id="plugin:external",
        name="External app",
        description="",
        path="/-/external",
        source="plugin",
    )
    tools = _tools_by_name()
    context = FakeContext(answers=[True])

    result = json.loads(
        await tools["app_set_csp_origins"].fn(
            datasette=datasette,
            actor={"id": "bob"},
            context=context,
            app_id=app["id"],
            csp_origins=["https://cdn.jsdelivr.net"],
        )
    )
    assert result == {"error": "Permission denied: edit-app", "app_id": app["id"]}

    result = json.loads(
        await tools["app_set_csp_origins"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id="plugin:external",
            csp_origins=["https://cdn.jsdelivr.net"],
        )
    )
    assert result == {
        "error": "Permission denied: edit-app",
        "app_id": "plugin:external",
    }

    result = json.loads(
        await tools["app_set_csp_origins"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=context,
            app_id=app["id"],
            csp_origins=["http://insecure.example.com"],
        )
    )
    assert result == {
        "error": "Only https:// origins are allowed",
        "app_id": app["id"],
    }
    assert context.questions == []


def test_app_set_csp_origins_schema_describes_rules():
    datasette = Datasette(
        memory=True,
        config={
            "plugins": {"datasette-apps": {"allowed_csp_origins": ["cdn.jsdelivr.net"]}}
        },
    )
    tools = _tools_by_name(datasette)
    description = tools["app_set_csp_origins"].input_schema["properties"][
        "csp_origins"
    ]["description"]
    assert description.startswith("The complete list of exact https:// origins")
    assert "approve newly added origins" in description
    assert "https://cdn.jsdelivr.net" in description
    assert "apps-set-csp" in description
    assert tools["app_set_csp_origins"].input_schema["required"] == [
        "app_id",
        "csp_origins",
    ]
    create_description = tools["app_create"].input_schema["properties"]["csp_origins"][
        "description"
    ]
    assert "approve this access before the app is created" in create_description
    assert "https://cdn.jsdelivr.net" in create_description


@pytest.mark.asyncio
async def test_app_set_csp_origins_with_real_datasette_agent_tool_context(tmp_path):
    """Integration against the real question runtime, when datasette-agent
    is installed: the first call suspends with QuestionPending and changes
    nothing, and the replay after the user answers records the revision."""
    pytest.importorskip("datasette_agent")
    from datasette_agent.questions import QuestionPending
    from datasette_agent.schema import ensure_tables
    from datasette_agent.tools import ToolContext

    datasette = Datasette(
        memory=True,
        internal=str(tmp_path / "internal.db"),
        config={"permissions": {"apps-set-csp": {"id": "alice"}}},
    )
    await datasette.invoke_startup()
    await ensure_tables(datasette.get_internal_database())
    registry = Registry(datasette)
    app = await _make_app(datasette)
    tools = _tools_by_name()
    arguments = {"app_id": app["id"], "csp_origins": ["https://api.github.com"]}

    def make_context():
        return ToolContext(
            datasette=datasette,
            actor={"id": "alice"},
            conversation_id="01CONVERSATION00000000TEST",
            tool_name="app_set_csp_origins",
            arguments=arguments,
            tool_call_id="call_1",
            supports_questions=True,
        )

    with pytest.raises(QuestionPending) as excinfo:
        await tools["app_set_csp_origins"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=make_context(),
            **arguments,
        )
    question = excinfo.value.question
    assert question["question_type"] == "boolean"
    assert question["prompt"] == "Allow Existing app to contact 1 external origin?"
    assert "<code>https://api.github.com</code>" in question["html"]
    assert await registry.get_csp_origins(app["id"]) == []

    db = datasette.get_internal_database()
    await db.execute_write(
        "UPDATE agent_questions SET status = 'answered', answer_json = ? WHERE id = ?",
        [json.dumps(True), question["id"]],
    )

    result = json.loads(
        await tools["app_set_csp_origins"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            context=make_context(),
            **arguments,
        )
    )
    assert "error" not in result
    assert result["version"] == 2
    assert result["added"] == ["https://api.github.com"]
    assert await registry.get_csp_origins(app["id"]) == ["https://api.github.com"]
