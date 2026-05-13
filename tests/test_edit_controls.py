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
    assert "datasette-app-form" in response.text
    assert 'class="datasette-app-edit-layout"' in response.text
    assert 'class="datasette-app-edit-sidebar"' in response.text
    assert 'textarea id="app-description" name="description"' in response.text
    assert "App access" in response.text
    assert "Private (only me)" in response.text
    assert "Signed-in users" in response.text
    assert "Specific users" not in response.text
    assert "Specific actor IDs" not in response.text
    assert 'name="actor_ids"' not in response.text
    assert "Read-only data access" in response.text
    assert "Read-only SQL query databases" in response.text
    assert 'name="sql_databases"' in response.text
    assert 'value="_memory"' in response.text
    assert "Network access" in response.text
    assert "Enter exact https:// origins" in response.text
    assert "Capabilities" not in response.text
    assert "Capability grants JSON" not in response.text
    assert 'name="capability_grants"' not in response.text
    assert response.text.index('class="datasette-app-edit-sidebar"') < response.text.index(
        "App access"
    )
    assert response.text.index("App access") < response.text.index("Read-only data access")
    assert response.text.index("Read-only data access") < response.text.index("Save app")


@pytest.mark.asyncio
async def test_edit_form_saves_sql_database_csp_and_capability_grants():
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
            "sql_databases_present": "1",
            "sql_databases": "_memory",
            "csp_origins": "https://api.github.com\n",
            "capability_grants": json.dumps({"test.echo": {"mode": "friendly"}}),
        },
    )

    assert response.status_code == 302
    assert await registry.get_sql_databases(app["id"]) == ["_memory"]
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
            "sql_databases_present": "1",
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
            "sql_databases_present": "1",
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
