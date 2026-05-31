import pytest
from datasette.app import Datasette

from datasette_apps import Registry


@pytest.mark.asyncio
async def test_create_form_shows_access_data_and_network_controls():
    datasette = Datasette(memory=True)

    response = await datasette.client.get("/-/apps/create", actor={"id": "alice"})

    assert response.status_code == 200
    assert "datasette-app-form" in response.text
    assert 'class="datasette-app-edit-layout"' in response.text
    assert 'class="datasette-app-edit-sidebar"' in response.text
    assert 'textarea id="app-description" name="description"' in response.text
    assert "App access" in response.text
    assert "Private (only me)" in response.text
    assert 'type="checkbox" name="is_private" value="1" checked' in response.text
    assert 'name="access_mode"' not in response.text
    assert "Specific users" not in response.text
    assert "Specific actor IDs" not in response.text
    assert 'name="actor_ids"' not in response.text
    assert "Read-only data access" in response.text
    assert "Read-only SQL query databases" in response.text
    assert 'name="sql_databases"' in response.text
    assert 'value="_memory"' in response.text
    assert "Network access" in response.text
    assert "Enter exact https:// origins" in response.text
    assert "external scripts" in response.text
    assert 'name="csp_origins"' in response.text
    assert response.text.index(
        'class="datasette-app-edit-sidebar"'
    ) < response.text.index("App access")
    assert response.text.index("App access") < response.text.index(
        "Read-only data access"
    )
    assert response.text.index("Read-only data access") < response.text.index(
        ">Create app</button>"
    )


@pytest.mark.asyncio
async def test_create_form_saves_access_data_and_network_controls():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
    registry = Registry(datasette)

    response = await datasette.client.post(
        "/-/apps/create",
        actor={"id": "alice"},
        data={
            "name": "Shared app",
            "description": "",
            "html": "<h1>Shared</h1>",
            "is_private": "0",
            "sql_databases_present": "1",
            "sql_databases": "_memory",
            "csp_origins": "https://api.github.com\n",
        },
    )

    assert response.status_code == 302
    app_id = response.headers["location"].rsplit("/", 1)[-1]
    assert await registry.get_access_mode(app_id) == "not-private"
    assert await registry.get_sql_databases(app_id) == ["_memory"]
    assert await registry.get_csp_origins(app_id) == ["https://api.github.com"]

    bob = await datasette.client.get(response.headers["location"], actor={"id": "bob"})
    assert bob.status_code == 200


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
    assert 'type="checkbox" name="is_private" value="1" checked' in response.text
    assert 'name="access_mode"' not in response.text
    assert "Specific users" not in response.text
    assert "Specific actor IDs" not in response.text
    assert 'name="actor_ids"' not in response.text
    assert "Read-only data access" in response.text
    assert "Read-only SQL query databases" in response.text
    assert 'name="sql_databases"' in response.text
    assert 'value="_memory"' in response.text
    assert "Network access" in response.text
    assert "Enter exact https:// origins" in response.text
    assert "external scripts" in response.text
    assert "Capabilities" not in response.text
    assert "Capability grants JSON" not in response.text
    assert 'name="capability_grants"' not in response.text
    assert response.text.index(
        'class="datasette-app-edit-sidebar"'
    ) < response.text.index("App access")
    assert response.text.index("App access") < response.text.index(
        "Read-only data access"
    )
    assert response.text.index("Read-only data access") < response.text.index(
        "Save app"
    )


@pytest.mark.asyncio
async def test_edit_form_saves_sql_database_and_csp():
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
            "is_private": "1",
            "sql_databases_present": "1",
            "sql_databases": "_memory",
            "csp_origins": "https://api.github.com\n",
        },
    )

    assert response.status_code == 302
    assert await registry.get_sql_databases(app["id"]) == ["_memory"]
    assert await registry.get_csp_origins(app["id"]) == ["https://api.github.com"]


@pytest.mark.asyncio
async def test_edit_form_records_one_revision_for_one_save():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Controlled app",
        description="",
        html="<h1>Before</h1>",
    )

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/edit",
        actor={"id": "alice"},
        data={
            "name": "Controlled app",
            "description": "",
            "html": "<h1>After</h1>",
            "is_private": "0",
            "sql_databases_present": "1",
            "csp_origins": "",
        },
    )

    assert response.status_code == 302
    revisions = await registry.list_versions(app["id"])
    assert [revision["version"] for revision in revisions] == [2, 1]
    assert revisions[0]["changed_fields"] == ["html", "is_private"]


@pytest.mark.asyncio
async def test_edit_form_not_private_access_mode_allows_actor_with_view_app():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
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
            "is_private": "0",
            "sql_databases_present": "1",
            "csp_origins": "",
        },
    )

    response = await datasette.client.get(f"/-/apps/{app['id']}", actor={"id": "bob"})
    assert response.status_code == 200
