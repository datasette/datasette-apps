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
async def test_owner_can_delete_stored_app_until_it_is_deleted():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Owned",
        description="",
        html="",
    )
    await datasette.invoke_startup()

    assert await datasette.allowed(
        action="delete-app",
        resource=AppResource(app["id"]),
        actor={"id": "alice"},
    )
    assert not await datasette.allowed(
        action="delete-app",
        resource=AppResource(app["id"]),
        actor={"id": "bob"},
    )

    await registry.delete_stored_app(app["id"], actor_id="alice")

    assert not await datasette.allowed(
        action="delete-app",
        resource=AppResource(app["id"]),
        actor={"id": "alice"},
    )


@pytest.mark.asyncio
async def test_create_app_requires_explicit_permission_grant():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()

    assert not await datasette.allowed(
        action="create-app",
        resource=AppsResource(),
        actor={"id": "alice"},
    )

    response = await datasette.client.get("/-/apps/create", actor={"id": "alice"})
    assert response.status_code == 403

    index = await datasette.client.get("/-/apps", actor={"id": "alice"})
    assert 'href="/-/apps/create"' not in index.text


@pytest.mark.asyncio
async def test_create_app_allows_explicit_permission_grant():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"create-app": {"id": "alice"}}},
    )
    await datasette.invoke_startup()

    assert await datasette.allowed(
        action="create-app",
        resource=AppsResource(),
        actor={"id": "alice"},
    )
    assert not await datasette.allowed(
        action="create-app",
        resource=AppsResource(),
        actor={"id": "bob"},
    )

    response = await datasette.client.get("/-/apps/create", actor={"id": "alice"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_actors_with_view_app_can_view_external_apps():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
    await Registry(datasette).add_app(
        id="plugin:one",
        name="Plugin One",
        description="",
        path="/-/plugin-one",
        source="plugin",
    )
    await datasette.invoke_startup()

    assert not await datasette.allowed(
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
async def test_non_private_app_viewable_by_signed_in_actors():
    # With datasette-acl installed, "not private" maps to a Viewer grant for
    # the "authenticated" public audience: any signed-in actor can view, no
    # instance-level view-app configuration required. Anonymous actors still
    # need an explicit config grant.
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Shared app",
        description="",
        html="<h1>Shared</h1>",
    )
    await registry.set_access_mode(app["id"], "not-private")
    await datasette.invoke_startup()

    assert await datasette.allowed(
        action="view-app",
        resource=AppResource(app["id"]),
        actor={"id": "bob"},
    )
    assert not await datasette.allowed(
        action="view-app",
        resource=AppResource(app["id"]),
        actor=None,
    )

    datasette_with_permission = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
    registry_with_permission = Registry(datasette_with_permission)
    app_with_permission = await registry_with_permission.create_stored_app(
        actor_id="alice",
        name="Shared app",
        description="",
        html="<h1>Shared</h1>",
    )
    await registry_with_permission.set_access_mode(
        app_with_permission["id"], "not-private"
    )
    await datasette_with_permission.invoke_startup()

    assert await datasette_with_permission.allowed(
        action="view-app",
        resource=AppResource(app_with_permission["id"]),
        actor={"id": "bob"},
    )


@pytest.mark.asyncio
async def test_private_app_blocks_broad_view_app_permission():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Private app",
        description="",
        html="<h1>Private</h1>",
    )
    await datasette.invoke_startup()

    assert await datasette.allowed(
        action="view-app",
        resource=AppResource(app["id"]),
        actor={"id": "alice"},
    )
    assert not await datasette.allowed(
        action="view-app",
        resource=AppResource(app["id"]),
        actor={"id": "bob"},
    )


@pytest.mark.asyncio
async def test_anonymous_can_view_non_private_app_if_permission_allows():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": True}},
    )
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Anonymous app",
        description="",
        html="<h1>Anonymous</h1>",
    )
    await registry.set_access_mode(app["id"], "not-private")
    await datasette.invoke_startup()

    response = await datasette.client.get(f"/-/apps/{app['id']}")

    assert response.status_code == 200
    assert "Anonymous app" in response.text
    assert "datasette-app-pin-form" not in response.text


@pytest.mark.asyncio
async def test_edit_app_can_be_granted_to_non_owner_for_non_private_app():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"edit-app": {"id": "*"}}},
    )
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Owned",
        description="",
        html="",
    )
    await registry.set_access_mode(app["id"], "not-private")
    await datasette.invoke_startup()

    assert await datasette.allowed(
        action="edit-app",
        resource=AppResource(app["id"]),
        actor={"id": "alice"},
    )
    assert await datasette.allowed(
        action="edit-app",
        resource=AppResource(app["id"]),
        actor={"id": "bob"},
    )

    response = await datasette.client.get(
        f"/-/apps/{app['id']}/edit", actor={"id": "bob"}
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_private_app_edit_delete_and_manage_access_remain_owner_only():
    datasette = Datasette(
        memory=True,
        config={
            "permissions": {
                "edit-app": {"id": "*"},
                "delete-app": {"id": "*"},
                "manage-app-access": {"id": "*"},
            }
        },
    )
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Private",
        description="",
        html="",
    )
    await datasette.invoke_startup()

    for action in ("edit-app", "delete-app", "manage-app-access"):
        assert await datasette.allowed(
            action=action,
            resource=AppResource(app["id"]),
            actor={"id": "alice"},
        )
        assert not await datasette.allowed(
            action=action,
            resource=AppResource(app["id"]),
            actor={"id": "bob"},
        )

    edit = await datasette.client.get(
        f"/-/apps/{app['id']}/edit", actor={"id": "bob"}
    )
    assert edit.status_code == 403

    delete = await datasette.client.get(
        f"/-/apps/{app['id']}/delete", actor={"id": "bob"}
    )
    assert delete.status_code == 403


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


@pytest.mark.asyncio
async def test_shared_app_only_shows_edit_button_to_owner():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Shared app",
        description="",
        html="<h1>Shared</h1>",
    )
    await registry.set_access_mode(app["id"], "not-private")

    owner = await datasette.client.get(f"/-/apps/{app['id']}", actor={"id": "alice"})
    viewer = await datasette.client.get(f"/-/apps/{app['id']}", actor={"id": "bob"})

    assert owner.status_code == 200
    assert viewer.status_code == 200
    assert f'href="/-/apps/{app["id"]}/edit"' in owner.text
    assert f'href="/-/apps/{app["id"]}/edit"' not in viewer.text
    assert "Edit app" not in viewer.text


@pytest.mark.asyncio
async def test_apps_set_csp_is_denied_by_default():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()

    assert not await datasette.allowed(
        action="apps-set-csp",
        resource=AppsResource(),
        actor={"id": "alice"},
    )


@pytest.mark.asyncio
async def test_apps_set_csp_can_be_granted_via_config():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"apps-set-csp": {"id": "admin"}}},
    )
    await datasette.invoke_startup()

    assert await datasette.allowed(
        action="apps-set-csp",
        resource=AppsResource(),
        actor={"id": "admin"},
    )
    assert not await datasette.allowed(
        action="apps-set-csp",
        resource=AppsResource(),
        actor={"id": "alice"},
    )
