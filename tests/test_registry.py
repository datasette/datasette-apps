import pytest

from datasette.app import Datasette
from datasette_apps import Registry


@pytest.mark.asyncio
async def test_registry_add_get_list_and_remove_external_app():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)

    await registry.add_app(
        id="myplugin:one",
        name="Plugin app",
        description="A useful app",
        path="/-/plugin-app",
        source="myplugin",
        metadata={"color": "blue"},
    )

    app = await registry.get_app("myplugin:one")
    assert app["id"] == "myplugin:one"
    assert app["external"] == 1
    assert app["name"] == "Plugin app"
    assert app["description"] == "A useful app"
    assert app["path"] == "/-/plugin-app"
    assert app["source"] == "myplugin"
    assert app["metadata"] == {"color": "blue"}

    apps = await registry.list_apps()
    assert [app["id"] for app in apps] == ["myplugin:one"]

    await registry.remove_app("myplugin:one")
    assert await registry.get_app("myplugin:one") is None


@pytest.mark.asyncio
async def test_registry_search_uses_fts5():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)

    await registry.add_apps(
        [
            {
                "id": "plugin:weather",
                "name": "Weather dashboard",
                "description": "Forecasts and climate",
                "path": "/-/weather",
            },
            {
                "id": "plugin:news",
                "name": "News reader",
                "description": "Headlines and authors",
                "path": "/-/news",
            },
        ],
        source="plugin",
    )

    apps = await registry.list_apps(q="forecast")
    assert [app["id"] for app in apps] == ["plugin:weather"]


@pytest.mark.asyncio
async def test_registry_records_access_and_pins_apps():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    await registry.add_app(
        id="plugin:one",
        name="One",
        description="",
        path="/-/one",
        source="plugin",
    )

    await registry.record_access("alice", "plugin:one")
    await registry.record_access("alice", "plugin:one")
    await registry.set_pinned("alice", "plugin:one", True)

    state = await registry.get_user_state("alice", "plugin:one")
    assert state["access_count"] == 2
    assert state["last_accessed_at"] is not None
    assert state["pinned_at"] is not None

    await registry.set_pinned("alice", "plugin:one", False)
    state = await registry.get_user_state("alice", "plugin:one")
    assert state["pinned_at"] is None


@pytest.mark.asyncio
async def test_registry_create_stored_app_and_save_versions():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)

    app = await registry.create_stored_app(
        actor_id="alice",
        name="My app",
        description="An HTML app",
        html="<h1>Hello</h1>",
    )

    assert app["external"] == 0
    assert len(app["id"]) == 26
    assert app["id"] == app["id"].lower()
    assert app["path"] == f"/-/apps/{app['id']}"
    assert app["current_version"] == 1
    assert app["actor_id"] == "alice"

    version = await registry.get_current_version(app["id"])
    assert version["version"] == 1
    assert version["html"] == "<h1>Hello</h1>"

    await registry.save_new_version(app["id"], "<h1>Updated</h1>")
    app = await registry.get_app(app["id"])
    version = await registry.get_current_version(app["id"])
    assert app["current_version"] == 2
    assert version["version"] == 2
    assert version["html"] == "<h1>Updated</h1>"
