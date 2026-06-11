"""Per-app sharing via datasette-acl grants + the acl-share dialog."""

import pytest
from datasette.app import Datasette
from datasette_acl.grants import grant, list_grants, revoke

from datasette_apps import Registry
from datasette_apps import acl as apps_acl
from datasette_apps.permissions import AppResource

ALICE = {"id": "alice"}
BOB = {"id": "bob"}
CAROL = {"id": "carol"}


async def started_datasette(**kwargs):
    datasette = Datasette(memory=True, **kwargs)
    await datasette.invoke_startup()
    return datasette


async def create_app(datasette, actor_id="alice", **kwargs):
    kwargs.setdefault("name", "Test app")
    return await Registry(datasette).create_stored_app(
        actor_id=actor_id,
        description="",
        html="<h1>Test</h1>",
        **kwargs,
    )


def actor_grant(grants, actor_id):
    for entry in grants:
        if entry["actor_id"] == actor_id:
            return entry
    return None


async def allowed(datasette, action, app_id, actor):
    return await datasette.allowed(
        action=action, resource=AppResource(app_id), actor=actor
    )


@pytest.mark.asyncio
async def test_create_seeds_owner_manager_grant():
    datasette = await started_datasette()
    app = await create_app(datasette)

    grants = await list_grants(datasette, "app", "apps", app["id"])
    owner = actor_grant(grants, "alice")
    assert owner is not None
    assert owner["actions"] == [
        "delete-app",
        "edit-app",
        "manage-app-access",
        "view-app",
    ]


@pytest.mark.asyncio
async def test_viewer_grant_allows_view_only_and_revoke_removes_access():
    datasette = await started_datasette()
    app = await create_app(datasette)

    assert not await allowed(datasette, "view-app", app["id"], BOB)

    await grant(
        datasette, "app", "apps", app["id"], actor_id="bob", role="Viewer", by_actor="alice"
    )
    assert await allowed(datasette, "view-app", app["id"], BOB)
    assert not await allowed(datasette, "edit-app", app["id"], BOB)
    assert not await allowed(datasette, "manage-app-access", app["id"], BOB)

    await revoke(datasette, "app", "apps", app["id"], actor_id="bob", by_actor="alice")
    assert not await allowed(datasette, "view-app", app["id"], BOB)


@pytest.mark.asyncio
async def test_editor_grant_allows_edit_but_not_manage():
    datasette = await started_datasette()
    app = await create_app(datasette)
    await grant(
        datasette, "app", "apps", app["id"], actor_id="bob", role="Editor", by_actor="alice"
    )

    assert await allowed(datasette, "edit-app", app["id"], BOB)
    assert not await allowed(datasette, "manage-app-access", app["id"], BOB)

    edit_page = await datasette.client.get(f"/-/apps/{app['id']}/edit", actor=BOB)
    assert edit_page.status_code == 200


@pytest.mark.asyncio
async def test_acl_api_recognizes_apps_and_gates_on_manage():
    # The dialog's read endpoint 403s unless the resource is enumerated by
    # resources_sql AND the caller holds the manage-only action.
    datasette = await started_datasette()
    app = await create_app(datasette)
    await grant(
        datasette, "app", "apps", app["id"], actor_id="bob", role="Editor", by_actor="alice"
    )

    owner = await datasette.client.get(
        f"/-/acl/api/resource/app/apps/{app['id']}", actor=ALICE
    )
    assert owner.status_code == 200
    data = owner.json()
    assert data["can_manage"] is True
    assert {role["name"] for role in data["roles"]} == {"Viewer", "Editor", "Manager"}

    editor = await datasette.client.get(
        f"/-/acl/api/resource/app/apps/{app['id']}", actor=BOB
    )
    assert editor.status_code == 403


@pytest.mark.asyncio
async def test_access_mode_toggle_syncs_signed_in_wildcard_grant():
    datasette = await started_datasette()
    registry = Registry(datasette)
    app = await create_app(datasette)

    assert not await allowed(datasette, "view-app", app["id"], CAROL)

    await registry.set_access_mode(app["id"], "not-private", actor_id="alice")
    assert await allowed(datasette, "view-app", app["id"], CAROL)
    assert not await allowed(datasette, "view-app", app["id"], None)
    grants = await list_grants(datasette, "app", "apps", app["id"])
    assert actor_grant(grants, "_signed_in")["actions"] == ["view-app"]

    await registry.set_access_mode(app["id"], "private", actor_id="alice")
    assert not await allowed(datasette, "view-app", app["id"], CAROL)
    grants = await list_grants(datasette, "app", "apps", app["id"])
    assert actor_grant(grants, "_signed_in") is None


@pytest.mark.asyncio
async def test_update_stored_app_is_private_change_syncs_wildcard_grant():
    datasette = await started_datasette()
    registry = Registry(datasette)
    app = await create_app(datasette)

    await registry.update_stored_app(
        app["id"],
        app["name"],
        "",
        "<h1>Test</h1>",
        actor_id="alice",
        is_private=False,
    )
    assert await allowed(datasette, "view-app", app["id"], CAROL)

    await registry.update_stored_app(
        app["id"],
        app["name"],
        "",
        "<h1>Test</h1>",
        actor_id="alice",
        is_private=True,
    )
    assert not await allowed(datasette, "view-app", app["id"], CAROL)


@pytest.mark.asyncio
async def test_startup_backfills_grants_for_pre_acl_apps():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    private_app = await create_app(datasette, name="Private")
    shared_app = await create_app(datasette, name="Shared")
    await registry.set_access_mode(shared_app["id"], "not-private")

    # Nothing was seeded pre-startup; the startup backfill converts owners
    # and the is_private flag into grants.
    await datasette.invoke_startup()

    for app in (private_app, shared_app):
        grants = await list_grants(datasette, "app", "apps", app["id"])
        assert actor_grant(grants, "alice")["actions"] == [
            "delete-app",
            "edit-app",
            "manage-app-access",
            "view-app",
        ]
    assert await allowed(datasette, "view-app", shared_app["id"], BOB)
    assert not await allowed(datasette, "view-app", private_app["id"], BOB)

    # Marker guards reruns
    stats = await apps_acl.backfill_acl_grants(datasette)
    assert stats == {"owners": 0, "wildcards": 0, "skipped": True}


@pytest.mark.asyncio
async def test_revoked_grants_do_not_apply_to_deleted_apps():
    datasette = await started_datasette()
    registry = Registry(datasette)
    app = await create_app(datasette)
    await grant(
        datasette, "app", "apps", app["id"], actor_id="bob", role="Viewer", by_actor="alice"
    )

    await registry.delete_stored_app(app["id"], actor_id="alice")

    assert not await allowed(datasette, "view-app", app["id"], ALICE)
    assert not await allowed(datasette, "view-app", app["id"], BOB)


@pytest.mark.asyncio
async def test_share_trigger_rendered_for_managers_only():
    datasette = await started_datasette()
    app = await create_app(datasette)
    await grant(
        datasette, "app", "apps", app["id"], actor_id="bob", role="Editor", by_actor="alice"
    )

    owner = await datasette.client.get(f"/-/apps/{app['id']}", actor=ALICE)
    assert owner.status_code == 200
    assert "<datasette-acl-share-dialog" in owner.text
    assert f'child="{app["id"]}"' in owner.text

    editor = await datasette.client.get(f"/-/apps/{app['id']}", actor=BOB)
    assert editor.status_code == 200
    assert "<datasette-acl-share-dialog" not in editor.text


@pytest.mark.asyncio
async def test_shared_app_appears_in_catalog():
    datasette = await started_datasette()
    app = await create_app(datasette, name="Catalog app")

    before = await datasette.client.get("/-/apps", actor=BOB)
    assert "Catalog app" not in before.text

    await grant(
        datasette, "app", "apps", app["id"], actor_id="bob", role="Viewer", by_actor="alice"
    )
    after = await datasette.client.get("/-/apps", actor=BOB)
    assert "Catalog app" in after.text
