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


def _tools_by_name():
    return {tool.name: tool for tool in get_app_edit_tools(FakeAgentTool)}


@pytest.mark.asyncio
async def test_app_edit_agent_tools_are_registered():
    tools = _tools_by_name()
    assert set(tools) == {
        "app_list",
        "app_create",
        "app_view",
        "app_str_replace",
        "app_insert",
        "app_edit",
        "app_render",
        "app_debug",
    }
    assert tools["app_edit"].input_schema["required"] == ["app_id", "edits"]
    assert tools["app_list"].input_schema["properties"]["limit"] == {
        "type": "integer",
        "description": "Maximum number of apps to return; defaults to 10",
        "minimum": 1,
        "maximum": 20,
    }
    assert "params is an optional object of named SQL parameters" in (
        tools["app_create"].description
    )
    assert "{id: 1} for SQL containing :id" in tools["app_create"].description
    assert "{columns: [...], rows: [{...}, ...]}" in tools["app_create"].description
    assert "result.rows[0].count" in tools["app_create"].description
    assert "Do not use result[0]." in tools["app_create"].description
    assert "unless" not in tools["app_create"].description


@pytest.mark.asyncio
async def test_app_list_agent_tool_only_returns_editable_stored_apps():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"edit-app": {"id": "alice"}}},
    )
    registry = Registry(datasette)
    owned = await registry.create_stored_app(
        actor_id="alice",
        name="Owned app",
        description="Alice owns this",
        html="<h1>Owned</h1>",
    )
    shared = await registry.create_stored_app(
        actor_id="bob",
        name="Shared app",
        description="Alice has configured edit access",
        html="<h1>Shared</h1>",
    )
    await registry.set_access_mode(shared["id"], "not-private", actor_id="bob")
    private = await registry.create_stored_app(
        actor_id="bob",
        name="Bob's private app",
        description="Not editable by Alice",
        html="<h1>Private</h1>",
    )
    deleted = await registry.create_stored_app(
        actor_id="alice",
        name="Deleted app",
        description="No longer available",
        html="<h1>Deleted</h1>",
    )
    await registry.delete_stored_app(deleted["id"], actor_id="alice")
    await registry.add_app(
        id="plugin:external",
        name="External app",
        description="Registered by another plugin",
        path="/-/external",
        source="plugin",
    )
    await datasette.invoke_startup()

    tools = _tools_by_name()
    result = json.loads(
        await tools["app_list"].fn(
            datasette=datasette,
            actor={"id": "alice"},
        )
    )

    assert {app["app_id"] for app in result["apps"]} == {
        owned["id"],
        shared["id"],
    }
    by_id = {app["app_id"]: app for app in result["apps"]}
    assert by_id[owned["id"]] == {
        "app_id": owned["id"],
        "name": "Owned app",
        "description": "Alice owns this",
        "current_version": 1,
        "updated_at": owned["updated_at"],
        "owned_by_current_actor": True,
    }
    assert by_id[shared["id"]]["owned_by_current_actor"] is False
    assert private["id"] not in by_id
    assert deleted["id"] not in by_id
    assert "plugin:external" not in by_id
    assert result["query"] is None
    assert result["returned_count"] == 2
    assert result["has_more"] is False


@pytest.mark.asyncio
async def test_app_list_agent_tool_searches_and_ranks_exact_name_first():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    description_match = await registry.create_stored_app(
        actor_id="alice",
        name="Task board",
        description="A TODO list for the team",
        html="",
    )
    exact_match = await registry.create_stored_app(
        actor_id="alice",
        name="TODO",
        description="Personal tasks",
        html="",
    )
    await datasette.invoke_startup()

    result = json.loads(
        await _tools_by_name()["app_list"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            query="todo",
        )
    )

    assert [app["app_id"] for app in result["apps"]] == [
        exact_match["id"],
        description_match["id"],
    ]
    assert result["query"] == "todo"


@pytest.mark.asyncio
async def test_app_list_agent_tool_applies_limit_after_permission_filtering():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    for index in range(12):
        await registry.create_stored_app(
            actor_id="bob",
            name=f"Bob app {index}",
            description="",
            html="",
        )
    alice_apps = []
    for index in range(3):
        alice_apps.append(
            await registry.create_stored_app(
                actor_id="alice",
                name=f"Alice app {index}",
                description="",
                html="",
            )
        )
    await datasette.invoke_startup()

    result = json.loads(
        await _tools_by_name()["app_list"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            limit=2,
        )
    )

    assert len(result["apps"]) == 2
    assert {app["app_id"] for app in result["apps"]}.issubset(
        {app["id"] for app in alice_apps}
    )
    assert result["returned_count"] == 2
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_app_list_agent_tool_prioritizes_pinned_apps_without_query():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"edit-app": {"id": "alice"}}},
    )
    registry = Registry(datasette)
    pinned = await registry.create_stored_app(
        actor_id="bob",
        name="Pinned shared app",
        description="",
        html="",
    )
    await registry.set_access_mode(pinned["id"], "not-private", actor_id="bob")
    await registry.set_pinned("alice", pinned["id"], True)
    owned = await registry.create_stored_app(
        actor_id="alice",
        name="Newer owned app",
        description="",
        html="",
    )
    await datasette.invoke_startup()

    result = json.loads(
        await _tools_by_name()["app_list"].fn(
            datasette=datasette,
            actor={"id": "alice"},
        )
    )

    assert [app["app_id"] for app in result["apps"]] == [
        pinned["id"],
        owned["id"],
    ]


@pytest.mark.asyncio
async def test_app_create_agent_tool_creates_new_app():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"create-app": {"id": "alice"}}},
    )
    await datasette.invoke_startup()
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            name="New app",
            description="Built by the agent",
            html="<h1>Hello from a new app</h1>\n",
        )
    )
    assert result["status"] == (
        "Created app. The user can open it with the rendered View app link above."
    )
    assert result["app_id"]
    assert "url" not in result
    assert "edit_url" not in result
    assert (
        f'class="datasette-app-button" href="/-/apps/{result["app_id"]}"'
        in result["_html"]
    )
    assert (
        f'class="datasette-app-button" href="/-/apps/{result["app_id"]}/edit"'
        in result["_html"]
    )
    assert "New app" in result["_html"]

    app = await Registry(datasette).get_app(result["app_id"])
    version = await Registry(datasette).get_current_version(result["app_id"])
    assert app["actor_id"] == "alice"
    assert app["name"] == "New app"
    assert app["description"] == "Built by the agent"
    assert app["is_private"] == 1
    assert version["html"] == "<h1>Hello from a new app</h1>\n"


@pytest.mark.asyncio
async def test_app_create_agent_tool_requires_create_permission():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor=None,
            name="Denied app",
            html="<h1>Nope</h1>",
        )
    )
    assert result == {"error": "Permission denied: create-app"}

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            name="Denied app",
            html="<h1>Nope</h1>",
        )
    )
    assert result == {"error": "Permission denied: create-app"}


@pytest.mark.asyncio
async def test_app_str_replace_agent_tool_edits_app_html_revision():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Hello app",
        description="",
        html="<h1>Hello</h1>\n<p>World</p>\n",
    )
    await datasette.invoke_startup()
    tools = _tools_by_name()

    view = json.loads(
        await tools["app_view"].fn(
            datasette=datasette, actor={"id": "alice"}, app_id=app["id"]
        )
    )
    assert view["app_id"] == app["id"]
    assert "1:\t<h1>Hello</h1>" in view["content"]
    assert view["metadata"]["name"] == "Hello app"

    edit = json.loads(
        await tools["app_str_replace"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            app_id=app["id"],
            old_str="Hello",
            new_str="Updated",
        )
    )
    assert edit["version"] == 2
    assert edit["status"] == (
        "Replacement applied. Call app_render to display the updated content."
    )

    current = await registry.get_current_version(app["id"])
    assert current["version"] == 2
    assert "<h1>Updated</h1>" in current["html"]


@pytest.mark.asyncio
async def test_app_edit_agent_tool_batches_edits_into_one_revision_and_renders():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Batch app",
        description="",
        html="<h1>Hello</h1>\n<p>World</p>\n",
    )
    await datasette.invoke_startup()
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_edit"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            app_id=app["id"],
            edits=[
                {
                    "operation": "str_replace",
                    "old_str": "Hello",
                    "new_str": "Goodbye",
                },
                {"operation": "insert", "insert_line": 2, "insert_text": "<hr>\n"},
            ],
        )
    )
    assert result["version"] == 2
    assert result["edits_applied"] == 2
    assert "url" not in result
    assert "edit_url" not in result
    assert f'class="datasette-app-button" href="/-/apps/{app["id"]}"' in result["_html"]
    assert "Batch app" in result["_html"]

    versions = await registry.list_versions(app["id"])
    assert [version["version"] for version in versions] == [2, 1]
    assert "<h1>Goodbye</h1>\n<p>World</p>\n<hr>\n" in versions[0]["html"]


@pytest.mark.asyncio
async def test_app_edit_agent_tool_partial_failure_saves_nothing():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Atomic app",
        description="",
        html="<h1>Hello</h1>\n",
    )
    await datasette.invoke_startup()
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_edit"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            app_id=app["id"],
            edits=[
                {
                    "operation": "str_replace",
                    "old_str": "Hello",
                    "new_str": "Goodbye",
                },
                {"operation": "str_replace", "old_str": "Missing", "new_str": "Nope"},
            ],
        )
    )
    assert "Edit #2 failed" in result["error"]
    assert result["applied"] == ["str_replace #1: OK"]

    versions = await registry.list_versions(app["id"])
    assert [version["version"] for version in versions] == [1]
    assert versions[0]["html"] == "<h1>Hello</h1>\n"


@pytest.mark.asyncio
async def test_app_agent_tools_require_app_edit_permission():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Private app",
        description="",
        html="<h1>Hello</h1>\n",
    )
    await datasette.invoke_startup()
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_view"].fn(
            datasette=datasette, actor={"id": "bob"}, app_id=app["id"]
        )
    )
    assert result == {"error": "Permission denied: edit-app", "app_id": app["id"]}


@pytest.mark.asyncio
async def test_app_agent_tool_rendered_links_respect_base_url():
    datasette = Datasette(
        memory=True,
        settings={"base_url": "/prefix/"},
        config={"permissions": {"create-app": {"id": "alice"}}},
    )
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Prefixed app",
        description="",
        html="<h1>Hello</h1>\n",
    )
    await datasette.invoke_startup()
    tools = _tools_by_name()

    created = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            name="New prefixed app",
            html="<h1>Hello</h1>\n",
        )
    )
    assert f'href="/prefix/-/apps/{created["app_id"]}"' in created["_html"]
    assert f'href="/prefix/-/apps/{created["app_id"]}/edit"' in created["_html"]

    edited = json.loads(
        await tools["app_edit"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            app_id=app["id"],
            edits=[
                {
                    "operation": "str_replace",
                    "old_str": "Hello",
                    "new_str": "Updated",
                }
            ],
        )
    )
    assert f'href="/prefix/-/apps/{app["id"]}"' in edited["_html"]
    assert f'href="/prefix/-/apps/{app["id"]}/edit"' in edited["_html"]


@pytest.mark.asyncio
async def test_app_create_agent_tool_rejects_disallowed_csp_origin():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"create-app": {"id": "alice"}}},
    )
    await datasette.invoke_startup()
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            name="Sneaky app",
            html="<h1>Hi</h1>",
            csp_origins=["https://attacker.example.com"],
        )
    )
    assert "https://attacker.example.com" in result["error"]
    assert "apps-set-csp" in result["error"]


@pytest.mark.asyncio
async def test_app_create_agent_tool_allows_allowlisted_csp_origin():
    datasette = Datasette(
        memory=True,
        config={
            "plugins": {
                "datasette-apps": {"allowed_csp_origins": ["cdn.jsdelivr.net"]}
            },
            "permissions": {"create-app": {"id": "alice"}},
        },
    )
    await datasette.invoke_startup()
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            name="CDN app",
            html="<h1>Hi</h1>",
            csp_origins=["https://cdn.jsdelivr.net"],
        )
    )
    assert "error" not in result
    assert await Registry(datasette).get_csp_origins(result["app_id"]) == [
        "https://cdn.jsdelivr.net"
    ]


@pytest.mark.asyncio
async def test_app_create_agent_tool_allows_any_origin_with_permission():
    datasette = Datasette(
        memory=True,
        config={
            "permissions": {
                "apps-set-csp": {"id": "alice"},
                "create-app": {"id": "alice"},
            }
        },
    )
    await datasette.invoke_startup()
    tools = _tools_by_name()

    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            name="Privileged app",
            html="<h1>Hi</h1>",
            csp_origins=["https://api.github.com"],
        )
    )
    assert "error" not in result
    assert await Registry(datasette).get_csp_origins(result["app_id"]) == [
        "https://api.github.com"
    ]


def test_app_create_schema_mentions_allowlist_when_configured():
    datasette = Datasette(
        memory=True,
        config={
            "plugins": {"datasette-apps": {"allowed_csp_origins": ["cdn.jsdelivr.net"]}}
        },
    )
    tools = {tool.name: tool for tool in get_app_edit_tools(FakeAgentTool, datasette)}
    description = tools["app_create"].input_schema["properties"]["csp_origins"][
        "description"
    ]
    assert "https://cdn.jsdelivr.net" in description
    assert "apps-set-csp" in description


def test_app_create_schema_mentions_permission_when_no_allowlist():
    datasette = Datasette(memory=True)
    tools = {tool.name: tool for tool in get_app_edit_tools(FakeAgentTool, datasette)}
    description = tools["app_create"].input_schema["properties"]["csp_origins"][
        "description"
    ]
    assert "apps-set-csp" in description
