import json

import pytest
from datasette.app import Datasette

from datasette_apps import Registry


@pytest.mark.asyncio
async def test_edit_form_shows_access_data_network_and_capability_controls():
    datasette = Datasette(memory=True)
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Controlled app",
        description="",
        html="",
    )

    response = await datasette.client.get(
        f"/-/apps/{app['id']}/edit", actor={"id": "alice"}
    )

    assert response.status_code == 200
    assert "App access" in response.text
    assert "Data access" in response.text
    assert "Network access" in response.text
    assert "Capabilities" in response.text


@pytest.mark.asyncio
async def test_edit_form_saves_data_csp_and_capability_grants():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Controlled app",
        description="",
        html="",
    )

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/edit",
        actor={"id": "alice"},
        data={
            "name": "Controlled app",
            "description": "",
            "html": "",
            "access_mode": "private",
            "data_permissions": json.dumps(
                [
                    {
                        "database_name": "content",
                        "resource_type": "table",
                        "resource_name": "news",
                        "columns": ["title"],
                    }
                ]
            ),
            "csp_origins": "https://api.github.com\n",
            "capability_grants": json.dumps({"test.echo": {"mode": "friendly"}}),
        },
    )

    assert response.status_code == 302
    assert (await registry.get_data_permissions(app["id"]))[0]["columns"] == ["title"]
    assert await registry.get_csp_origins(app["id"]) == ["https://api.github.com"]
    assert (await registry.get_capability_grant(app["id"], "test.echo"))["config"] == {
        "mode": "friendly"
    }


@pytest.mark.asyncio
async def test_edit_form_signed_in_access_mode_allows_other_actor():
    datasette = Datasette(memory=True)
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Shared app",
        description="",
        html="<h1>Shared</h1>",
    )

    await datasette.client.post(
        f"/-/apps/{app['id']}/edit",
        actor={"id": "alice"},
        data={
            "name": "Shared app",
            "description": "",
            "html": "<h1>Shared</h1>",
            "access_mode": "signed-in",
            "data_permissions": "[]",
            "csp_origins": "",
            "capability_grants": "{}",
        },
    )

    response = await datasette.client.get(f"/-/apps/{app['id']}", actor={"id": "bob"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_edit_form_specific_users_access_mode():
    datasette = Datasette(memory=True)
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Team app",
        description="",
        html="<h1>Team</h1>",
    )

    await datasette.client.post(
        f"/-/apps/{app['id']}/edit",
        actor={"id": "alice"},
        data={
            "name": "Team app",
            "description": "",
            "html": "<h1>Team</h1>",
            "access_mode": "specific",
            "actor_ids": "bob\ncarol",
            "data_permissions": "[]",
            "csp_origins": "",
            "capability_grants": "{}",
        },
    )

    bob = await datasette.client.get(f"/-/apps/{app['id']}", actor={"id": "bob"})
    mallory = await datasette.client.get(
        f"/-/apps/{app['id']}", actor={"id": "mallory"}
    )
    assert bob.status_code == 200
    assert mallory.status_code == 403
