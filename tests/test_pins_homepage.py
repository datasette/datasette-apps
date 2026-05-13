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

    response = await datasette.client.post(
        "/-/apps/plugin:second/pin", actor={"id": "alice"}
    )
    assert response.status_code == 302

    index = await datasette.client.get("/-/apps", actor={"id": "alice"})
    assert index.status_code == 200
    assert index.text.index("Second app") < index.text.index("First app")

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
