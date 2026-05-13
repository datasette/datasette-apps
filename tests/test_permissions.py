import pytest
from datasette.app import Datasette

from datasette_apps import Registry
from datasette_apps.permissions import AppResource, AppsResource


@pytest.mark.asyncio
async def test_owner_can_view_and_edit_stored_app():
    datasette = Datasette(memory=True)
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Owned",
        description="",
        html="",
    )
    await datasette.invoke_startup()

    assert await datasette.allowed(
        action="view-app",
        resource=AppResource(app["id"]),
        actor={"id": "alice"},
    )
    assert await datasette.allowed(
        action="edit-app",
        resource=AppResource(app["id"]),
        actor={"id": "alice"},
    )
    assert not await datasette.allowed(
        action="view-app",
        resource=AppResource(app["id"]),
        actor={"id": "bob"},
    )


@pytest.mark.asyncio
async def test_signed_in_users_can_create_and_view_external_apps():
    datasette = Datasette(memory=True)
    await Registry(datasette).add_app(
        id="plugin:one",
        name="Plugin One",
        description="",
        path="/-/plugin-one",
        source="plugin",
    )
    await datasette.invoke_startup()

    assert await datasette.allowed(
        action="create-app",
        resource=AppsResource(),
        actor={"id": "alice"},
    )
    assert await datasette.allowed(
        action="view-app",
        resource=AppResource("plugin:one"),
        actor={"id": "alice"},
    )
    assert not await datasette.allowed(
        action="view-app",
        resource=AppResource("plugin:one"),
        actor=None,
    )


@pytest.mark.asyncio
async def test_routes_enforce_app_permissions():
    datasette = Datasette(memory=True)
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Private app",
        description="",
        html="<h1>Private</h1>",
    )

    denied = await datasette.client.get(f"/-/apps/{app['id']}", actor={"id": "bob"})
    assert denied.status_code == 403

    allowed = await datasette.client.get(f"/-/apps/{app['id']}", actor={"id": "alice"})
    assert allowed.status_code == 200
