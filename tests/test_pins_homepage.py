import pytest
from datasette.app import Datasette

from datasette_apps import Registry


@pytest.mark.asyncio
async def test_pin_routes_and_catalog_order():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    await registry.add_app(
        id="plugin:first",
        name="First app",
        description="",
        path="/-/first",
        source="plugin",
    )
    await registry.add_app(
        id="plugin:second",
        name="Second app",
        description="",
        path="/-/second",
        source="plugin",
    )

    index = await datasette.client.get("/-/apps", actor={"id": "alice"})
    assert index.status_code == 200
    assert 'action="/-/apps/plugin:second/pin"' in index.text
    assert 'class="datasette-app-button datasette-app-pin-button"' in index.text
    assert 'aria-label="Pin Second app"' in index.text

    response = await datasette.client.post(
        "/-/apps/plugin:second/pin",
        actor={"id": "alice"},
        data={"next": "/-/apps?q=app"},
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/-/apps?q=app"

    index = await datasette.client.get("/-/apps", actor={"id": "alice"})
    assert index.status_code == 200
    assert index.text.index("Second app") < index.text.index("First app")
    assert 'action="/-/apps/plugin:second/unpin"' in index.text
    assert 'class="datasette-app-button datasette-app-pin-button"' in index.text
    assert 'aria-label="Unpin Second app"' in index.text

    await datasette.client.post("/-/apps/plugin:second/unpin", actor={"id": "alice"})
    state = await registry.get_user_state("alice", "plugin:second")
    assert state["pinned_at"] is None


@pytest.mark.asyncio
async def test_homepage_shows_three_recent_pinned_apps():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    for i in range(4):
        app_id = f"plugin:{i}"
        await registry.add_app(
            id=app_id,
            name=f"App {i}",
            description=f"Description {i}",
            path=f"/-/plugin-{i}",
            source="plugin",
        )
        await registry.set_pinned("alice", app_id, True)
        await registry.record_access("alice", app_id)

    response = await datasette.client.get("/", actor={"id": "alice"})
    assert response.status_code == 200
    assert "Pinned apps" in response.text
    assert "App 3" in response.text
    assert "App 2" in response.text
    assert "App 1" in response.text
    assert "App 0" not in response.text


@pytest.mark.asyncio
async def test_stored_app_view_includes_pin_controls():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Pinned tool",
        description="Can be pinned from its app page",
        html="<!DOCTYPE html><title>Pinned tool</title>",
    )

    view = await datasette.client.get(app["path"], actor={"id": "alice"})
    assert view.status_code == 200
    assert 'href="/-/apps"' in view.text
    assert ">All apps</a>" in view.text
    assert f'href="/-/apps/{app["id"]}/edit"' in view.text
    assert "datasette-app-button" in view.text
    assert f'action="/-/apps/{app["id"]}/pin"' in view.text
    assert 'aria-label="Pin Pinned tool"' in view.text

    response = await datasette.client.post(
        f'/-/apps/{app["id"]}/pin',
        actor={"id": "alice"},
        data={"next": app["path"]},
    )
    assert response.status_code == 302
    assert response.headers["location"] == app["path"]

    view = await datasette.client.get(app["path"], actor={"id": "alice"})
    assert view.status_code == 200
    assert f'action="/-/apps/{app["id"]}/unpin"' in view.text
    assert 'aria-label="Unpin Pinned tool"' in view.text
