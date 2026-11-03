from dataclasses import dataclass
from typing import Callable

import pytest
from datasette.app import Datasette

from datasette_apps import Registry
from datasette_apps.agent_references import get_app_reference_types


@dataclass
class FakeReference:
    type: str
    id: str
    name: str
    description: str = ""


@dataclass
class FakeReferenceType:
    type: str
    label: str
    search: Callable
    url: Callable
    instructions: str
    context: Callable


@pytest.fixture
def reference_type():
    return get_app_reference_types(FakeReferenceType, FakeReference)[0]


async def _datasette_with_apps():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
    registry = Registry(datasette)
    apps = {}
    apps["own"] = await registry.create_stored_app(
        actor_id="alice", name="Sales dashboard", description="Mine", html="<h1>A</h1>"
    )
    apps["shared"] = await registry.create_stored_app(
        actor_id="bob", name="Shared chart", description="Bob's", html="<h1>B</h1>"
    )
    await registry.set_access_mode(apps["shared"]["id"], "not-private", actor_id="bob")
    apps["private"] = await registry.create_stored_app(
        actor_id="bob", name="Secret sales", description="Hidden", html="<h1>C</h1>"
    )
    apps["deleted"] = await registry.create_stored_app(
        actor_id="alice", name="Deleted sales", description="Gone", html="<h1>D</h1>"
    )
    await registry.delete_stored_app(apps["deleted"]["id"], actor_id="alice")
    await registry.add_app(
        id="plugin:external",
        name="External sales",
        description="From a plugin",
        path="/-/external",
        source="plugin",
    )
    await datasette.invoke_startup()
    return datasette, apps


@pytest.mark.asyncio
async def test_search_returns_only_apps_actor_can_view(reference_type):
    datasette, apps = await _datasette_with_apps()
    refs = await reference_type.search(datasette, {"id": "alice"}, "", 20)
    assert {ref.name for ref in refs} == {
        "Sales dashboard",
        "Shared chart",
        "External sales",
    }
    assert (
        FakeReference(
            type="app", id=apps["own"]["id"], name="Sales dashboard", description="Mine"
        )
        in refs
    )
    assert await reference_type.search(datasette, None, "", 20) == []


@pytest.mark.asyncio
async def test_search_filters_on_query_and_applies_limit(reference_type):
    datasette, _ = await _datasette_with_apps()
    actor = {"id": "alice"}
    refs = await reference_type.search(datasette, actor, "sale", 20)
    assert [ref.name for ref in refs] == ["Sales dashboard", "External sales"]
    assert len(await reference_type.search(datasette, actor, "", 1)) == 1
    assert await reference_type.search(datasette, actor, "!!!", 20) == []


@pytest.mark.asyncio
async def test_context_reports_url_and_edit_access(reference_type):
    datasette, apps = await _datasette_with_apps()

    async def context(key, actor_id, id=None):
        ref = FakeReference(type="app", id=id or apps[key]["id"], name="x")
        return await reference_type.context(datasette, {"id": actor_id}, ref)

    own = await context("own", "alice")
    assert f"URL: /-/apps/{apps['own']['id']}" in own
    assert "The user can edit this app." in own
    assert "not edit it" in await context("shared", "alice")
    external = await context(None, "alice", id="plugin:external")
    assert "URL: /-/external" in external
    assert "cannot be edited" in external


@pytest.mark.asyncio
async def test_context_is_none_for_forged_references(reference_type):
    datasette, apps = await _datasette_with_apps()

    async def context(id):
        ref = FakeReference(type="app", id=id, name="x")
        return await reference_type.context(datasette, {"id": "alice"}, ref)

    assert await context(apps["private"]["id"]) is None
    assert await context(apps["deleted"]["id"]) is None
    assert await context("does-not-exist") is None


def test_url_uses_launch_route_and_escapes_id(reference_type):
    datasette = Datasette(memory=True)
    ref = FakeReference(type="app", id="plugin:a/b", name="x")
    assert reference_type.url(datasette, ref) == "/-/apps/plugin:a%2Fb/launch?full=1"


@pytest.mark.asyncio
async def test_registered_with_datasette_agent():
    try:
        from datasette_agent.references import (
            Reference,
            get_reference_types,
            references_note,
            search_references,
        )
    except ImportError:
        pytest.skip("Installed datasette-agent lacks register_agent_reference_types")

    datasette, apps = await _datasette_with_apps()
    actor = {"id": "alice"}
    app_id = apps["own"]["id"]
    results = await search_references(datasette, actor, "sales dash")
    assert results == [
        {
            "type": "app",
            "id": app_id,
            "name": "Sales dashboard",
            "description": "Mine",
            "label": "app",
            "url": f"/-/apps/{app_id}/launch?full=1",
        }
    ]
    types = await get_reference_types(datasette)
    note = await references_note(
        datasette,
        actor,
        [Reference(type="app", id=app_id, name="Sales dashboard")],
        types,
    )
    assert f"- #Sales dashboard (app id: {app_id})" in note
    assert "The user can edit this app." in note
    assert "app_view" in note
