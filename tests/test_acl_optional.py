"""Fallback access model when datasette-acl is not installed.

datasette-acl is an optional dependency. When it is absent the plugin must fall
back to the pre-acl model: owners always have access, and ``is_private`` acts as
a *filter* in restriction_sql (it never grants access on its own) — non-owners
can only view when an instance ``view-app`` config rule allows them.

These tests force ``ACL_AVAILABLE`` off in every module that reads it, so the
fallback paths are exercised even in the default test environment (where acl is
installed). For true end-to-end coverage of an acl-free install, also run the
suite in an environment without the ``acl`` dependency group, e.g.
``uv run --no-group acl pytest`` — there test_acl_sharing.py is skipped and
these tests still pass.
"""

import pytest
from datasette.app import Datasette

from datasette_apps import Registry
from datasette_apps.permissions import AppResource


@pytest.fixture
def no_acl(monkeypatch):
    """Pretend datasette-acl is not installed.

    Each module binds its own ``ACL_AVAILABLE`` name at import, so all three
    must be patched. The permission SQL builder and views read it at call time.
    """
    for module in ("datasette_apps.acl", "datasette_apps.permissions", "datasette_apps.views"):
        monkeypatch.setattr(f"{module}.ACL_AVAILABLE", False)


async def make_app(datasette, *, actor_id="alice", access_mode=None, name="Test app"):
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id=actor_id,
        name=name,
        description="",
        html="<h1>Test</h1>",
    )
    if access_mode is not None:
        await registry.set_access_mode(app["id"], access_mode)
    return app


@pytest.mark.asyncio
async def test_owner_can_view_edit_delete_without_acl(no_acl):
    datasette = Datasette(memory=True)
    app = await make_app(datasette)
    await datasette.invoke_startup()

    for action in ("view-app", "edit-app", "delete-app", "manage-app-access"):
        assert await datasette.allowed(
            action=action, resource=AppResource(app["id"]), actor={"id": "alice"}
        ), action


@pytest.mark.asyncio
async def test_private_app_is_owner_only_even_with_config(no_acl):
    # is_private=1 lives in restriction_sql, which clips even a broad config
    # view-app rule: only the owner passes the filter.
    datasette = Datasette(
        memory=True, config={"permissions": {"view-app": {"id": "*"}}}
    )
    app = await make_app(datasette)  # private by default
    await datasette.invoke_startup()

    assert await datasette.allowed(
        action="view-app", resource=AppResource(app["id"]), actor={"id": "alice"}
    )
    assert not await datasette.allowed(
        action="view-app", resource=AppResource(app["id"]), actor={"id": "bob"}
    )


@pytest.mark.asyncio
async def test_non_private_app_grants_nothing_without_config(no_acl):
    # Without acl there is no audience grant to supply the ALLOW, so is_private=0
    # on its own does not let a non-owner in.
    datasette = Datasette(memory=True)
    app = await make_app(datasette, access_mode="not-private")
    await datasette.invoke_startup()

    assert await datasette.allowed(
        action="view-app", resource=AppResource(app["id"]), actor={"id": "alice"}
    )
    assert not await datasette.allowed(
        action="view-app", resource=AppResource(app["id"]), actor={"id": "bob"}
    )


@pytest.mark.asyncio
async def test_non_private_app_viewable_with_config_rule(no_acl):
    datasette = Datasette(
        memory=True, config={"permissions": {"view-app": {"id": "*"}}}
    )
    app = await make_app(datasette, access_mode="not-private")
    await datasette.invoke_startup()

    assert await datasette.allowed(
        action="view-app", resource=AppResource(app["id"]), actor={"id": "bob"}
    )


@pytest.mark.asyncio
async def test_share_dialog_and_assets_absent(no_acl):
    from datasette_apps.acl import datasette_share_assets

    datasette = Datasette(memory=True)
    app = await make_app(datasette)
    await datasette.invoke_startup()

    # No share dialog rendered for the owner, even though they can manage.
    owner = await datasette.client.get(f"/-/apps/{app['id']}", actor={"id": "alice"})
    assert owner.status_code == 200
    assert "<datasette-acl-share-dialog" not in owner.text

    # No acl-share static assets contributed.
    assert datasette_share_assets(datasette) == {"css": [], "js": []}
    assert "datasette-acl-share" not in owner.text


@pytest.mark.asyncio
async def test_toggle_is_private_without_acl(no_acl):
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await make_app(datasette)
    await datasette.invoke_startup()

    # Toggling access mode must succeed (and not touch acl tables / raise).
    await registry.set_access_mode(app["id"], "not-private")
    assert await registry.get_access_mode(app["id"]) == "not-private"
    await registry.set_access_mode(app["id"], "private")
    assert await registry.get_access_mode(app["id"]) == "private"
