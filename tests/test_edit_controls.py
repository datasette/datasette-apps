import sqlite3

import pytest
from datasette.app import Datasette

from datasette_apps import Registry


def create_table_preview_database(tmp_path):
    db_path = tmp_path / "table_preview.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        create table _audit (id integer primary key);
        create table alpha (id integer primary key);
        create table beta (id integer primary key);
        create table charlie (id integer primary key);
        create table delta (id integer primary key);
        create table echo (id integer primary key);
        create table foxtrot (id integer primary key);
    """)
    conn.close()
    return db_path


@pytest.mark.asyncio
async def test_create_form_shows_access_data_and_network_controls():
    datasette = Datasette(
        memory=True,
        config={
            "permissions": {
                "apps-set-csp": {"id": "alice"},
                "create-app": {"id": "alice"},
            }
        },
    )

    response = await datasette.client.get("/-/apps/create", actor={"id": "alice"})

    assert response.status_code == 200
    assert "datasette-app-form" in response.text
    assert 'class="datasette-app-edit-layout"' in response.text
    assert 'class="datasette-app-edit-sidebar"' in response.text
    assert 'textarea id="app-description" name="description"' in response.text
    assert "Visibility" in response.text
    assert "Private (only me)" in response.text
    assert (
        "If Private is unchecked, this app will be visible to other users of this site."
        in response.text
    )
    assert 'type="checkbox" name="is_private" value="1" checked' in response.text
    assert 'name="access_mode"' not in response.text
    assert "Specific users" not in response.text
    assert "Specific actor IDs" not in response.text
    assert 'name="actor_ids"' not in response.text
    assert "Data access" in response.text
    assert "Read-only SQL query databases" in response.text
    assert (
        "The app will only be able to access data from the selected databases."
        in response.text
    )
    assert 'name="sql_databases"' in response.text
    assert 'value="_memory"' in response.text
    assert "Query access" in response.text
    assert 'data-query-search-url="/-/queries.json"' in response.text
    assert 'data-recent-query-url="/-/apps/recent-queries.json"' in response.text
    assert 'name="stored_queries_present"' in response.text
    assert 'aria-controls="stored-query-results"' in response.text
    assert "loadRecent()" in response.text
    assert "moveActiveResult(1)" in response.text
    assert "Pick stored queries this app can run." in response.text
    assert "Changes take effect after you save this page" not in response.text
    assert "Network access" in response.text
    assert (
        "any site listed here could receive private data from this app" in response.text
    )
    assert "Enter exact https:// origins" in response.text
    assert 'placeholder="https://cdn.jsdelivr.net"' in response.text
    assert "images, scripts, and styles" in response.text
    assert 'name="csp_origins"' in response.text
    assert response.text.index(
        'class="datasette-app-edit-sidebar"'
    ) < response.text.index("Visibility")
    assert response.text.index("Visibility") < response.text.index("Data access")
    assert response.text.index("Data access") < response.text.index(
        ">Create app</button>"
    )


@pytest.mark.asyncio
async def test_create_form_saves_access_data_and_network_controls():
    datasette = Datasette(
        memory=True,
        config={
            "permissions": {
                "create-app": {"id": "alice"},
                "view-app": {"id": "*"},
                "apps-set-csp": {"id": "alice"},
            }
        },
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
            "stored_queries_present": "1",
            "csp_origins": "https://api.github.com\n",
        },
    )

    assert response.status_code == 302
    app_id = response.headers["location"].rsplit("/", 1)[-1]
    assert await registry.get_access_mode(app_id) == "not-private"
    assert await registry.get_sql_databases(app_id) == ["_memory"]
    assert await registry.get_stored_queries(app_id) == []
    assert await registry.get_csp_origins(app_id) == ["https://api.github.com"]

    bob = await datasette.client.get(response.headers["location"], actor={"id": "bob"})
    assert bob.status_code == 200


@pytest.mark.asyncio
async def test_edit_form_shows_access_data_network_and_capability_controls():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"apps-set-csp": {"id": "alice"}}},
    )
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
    assert "Visibility" in response.text
    assert "Private (only me)" in response.text
    assert (
        "If Private is unchecked, this app will be visible to other users of this site."
        in response.text
    )
    assert 'type="checkbox" name="is_private" value="1" checked' in response.text
    assert 'name="access_mode"' not in response.text
    assert "Specific users" not in response.text
    assert "Specific actor IDs" not in response.text
    assert 'name="actor_ids"' not in response.text
    assert "Data access" in response.text
    assert "Read-only SQL query databases" in response.text
    assert (
        "The app will only be able to access data from the selected databases."
        in response.text
    )
    assert 'name="sql_databases"' in response.text
    assert 'value="_memory"' in response.text
    assert "Query access" in response.text
    assert 'data-query-search-url="/-/queries.json"' in response.text
    assert 'data-recent-query-url="/-/apps/recent-queries.json"' in response.text
    assert 'name="stored_queries_present"' in response.text
    assert "Pick stored queries this app can run." in response.text
    assert "Changes take effect after you save this page" not in response.text
    assert "Network access" in response.text
    assert (
        "any site listed here could receive private data from this app" in response.text
    )
    assert "Enter exact https:// origins" in response.text
    assert 'placeholder="https://cdn.jsdelivr.net"' in response.text
    assert "images, scripts, and styles" in response.text
    assert "Capabilities" not in response.text
    assert "Capability grants JSON" not in response.text
    assert 'name="capability_grants"' not in response.text
    assert response.text.index(
        'class="datasette-app-edit-sidebar"'
    ) < response.text.index("Visibility")
    assert response.text.index("Visibility") < response.text.index("Data access")
    assert response.text.index("Data access") < response.text.index("Save app")


@pytest.mark.asyncio
async def test_create_form_shows_database_link_and_table_preview(tmp_path):
    datasette = Datasette(
        [str(create_table_preview_database(tmp_path))],
        config={"permissions": {"create-app": {"id": "alice"}}},
    )

    response = await datasette.client.get("/-/apps/create", actor={"id": "alice"})

    assert response.status_code == 200
    assert (
        '<a href="/table_preview"><strong>table_preview</strong></a>' in response.text
    )
    assert "alpha, beta, charlie, delta, echo, ..." in response.text
    assert "table_preview, _audit" not in response.text
    assert "alpha, beta, charlie, delta, echo, foxtrot" not in response.text


@pytest.mark.asyncio
async def test_recent_stored_queries_endpoint_lists_newest_accessible_queries():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    for name in ("old_report", "middle_report", "new_report", "newest_report"):
        await datasette.add_query(
            "_memory",
            name,
            "select 1",
            source="user",
            owner_id="alice",
            is_private=True,
        )
    await datasette.add_query(
        "_memory",
        "hidden_report",
        "select 1",
        source="user",
        owner_id="bob",
        is_private=True,
    )
    created_at = {
        "old_report": "2026-01-01 00:00:00",
        "middle_report": "2026-01-02 00:00:00",
        "new_report": "2026-01-03 00:00:00",
        "newest_report": "2026-01-04 00:00:00",
        "hidden_report": "2026-01-05 00:00:00",
    }
    for name, timestamp in created_at.items():
        await datasette.get_internal_database().execute_write(
            """
            UPDATE queries
            SET created_at = ?
            WHERE database_name = '_memory' AND name = ?
            """,
            [timestamp, name],
        )

    response = await datasette.client.get(
        "/-/apps/recent-queries.json", actor={"id": "alice"}
    )

    assert response.status_code == 200
    assert [
        query["database"] + "/" + query["name"] for query in response.json()["queries"]
    ] == [
        "_memory/newest_report",
        "_memory/new_report",
        "_memory/middle_report",
    ]


@pytest.mark.asyncio
async def test_edit_form_saves_sql_database_and_csp():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"apps-set-csp": {"id": "alice"}}},
    )
    await datasette.invoke_startup()
    await datasette.add_query(
        "_memory",
        "saved_report",
        "select 1",
        source="user",
        owner_id="alice",
    )
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
            "stored_queries_present": "1",
            "stored_queries": "_memory/saved_report",
            "csp_origins": "https://api.github.com\n",
        },
    )

    assert response.status_code == 302
    assert await registry.get_sql_databases(app["id"]) == ["_memory"]
    assert await registry.get_stored_queries(app["id"]) == ["_memory/saved_report"]
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
            "stored_queries_present": "1",
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
            "stored_queries_present": "1",
            "csp_origins": "",
        },
    )

    response = await datasette.client.get(f"/-/apps/{app['id']}", actor={"id": "bob"})
    assert response.status_code == 200


CSP_ALLOWLIST_CONFIG = {
    "plugins": {"datasette-apps": {"allowed_csp_origins": ["https://cdn.jsdelivr.net"]}}
}


@pytest.mark.asyncio
async def test_create_form_hides_network_access_without_permission_or_allowlist():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"create-app": {"id": "alice"}}},
    )

    response = await datasette.client.get("/-/apps/create", actor={"id": "alice"})

    assert response.status_code == 200
    assert "Network access" not in response.text
    assert 'name="csp_origins"' not in response.text


@pytest.mark.asyncio
async def test_create_form_shows_allowlist_checkboxes_without_permission():
    datasette = Datasette(
        memory=True,
        config={
            **CSP_ALLOWLIST_CONFIG,
            "permissions": {"create-app": {"id": "alice"}},
        },
    )

    response = await datasette.client.get("/-/apps/create", actor={"id": "alice"})

    assert response.status_code == 200
    assert "Network access" in response.text
    assert 'name="csp_origins_present"' in response.text
    assert (
        'type="checkbox" name="csp_origins" value="https://cdn.jsdelivr.net"'
        in response.text
    )
    assert 'id="app-csp-origins"' not in response.text
    assert "administrator-approved" in response.text


@pytest.mark.asyncio
async def test_create_form_shows_textarea_with_apps_set_csp_permission():
    datasette = Datasette(
        memory=True,
        config={
            "permissions": {
                "apps-set-csp": {"id": "alice"},
                "create-app": {"id": "alice"},
            }
        },
    )

    response = await datasette.client.get("/-/apps/create", actor={"id": "alice"})

    assert response.status_code == 200
    assert 'textarea id="app-csp-origins" name="csp_origins"' in response.text
    assert "Enter exact https:// origins" in response.text


@pytest.mark.asyncio
async def test_create_post_rejects_origin_not_on_allowlist():
    datasette = Datasette(
        memory=True,
        config={
            **CSP_ALLOWLIST_CONFIG,
            "permissions": {"create-app": {"id": "alice"}},
        },
    )

    response = await datasette.client.post(
        "/-/apps/create",
        actor={"id": "alice"},
        data={
            "name": "Sneaky app",
            "description": "",
            "html": "<h1>Hi</h1>",
            "csp_origins": "https://attacker.example.com",
        },
    )

    assert response.status_code == 403
    assert "https://attacker.example.com" in response.text


@pytest.mark.asyncio
async def test_create_post_accepts_allowlisted_origin():
    datasette = Datasette(
        memory=True,
        config={
            **CSP_ALLOWLIST_CONFIG,
            "permissions": {"create-app": {"id": "alice"}},
        },
    )
    registry = Registry(datasette)

    response = await datasette.client.post(
        "/-/apps/create",
        actor={"id": "alice"},
        data={
            "name": "CDN app",
            "description": "",
            "html": "<h1>Hi</h1>",
            "csp_origins_present": "1",
            "csp_origins": "https://cdn.jsdelivr.net",
        },
    )

    assert response.status_code == 302
    app_id = response.headers["location"].rsplit("/", 1)[-1]
    assert await registry.get_csp_origins(app_id) == ["https://cdn.jsdelivr.net"]


@pytest.mark.asyncio
async def test_create_post_rejects_arbitrary_origin_without_allowlist():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"create-app": {"id": "alice"}}},
    )

    response = await datasette.client.post(
        "/-/apps/create",
        actor={"id": "alice"},
        data={
            "name": "Sneaky app",
            "description": "",
            "html": "<h1>Hi</h1>",
            "csp_origins": "https://attacker.example.com",
        },
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_edit_form_shows_existing_out_of_list_origin_as_checkbox():
    datasette = Datasette(memory=True, config=CSP_ALLOWLIST_CONFIG)
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Existing app",
        description="",
        html="",
        csp_origins=["https://api.github.com"],
    )

    response = await datasette.client.get(
        f"/-/apps/{app['id']}/edit", actor={"id": "alice"}
    )

    assert response.status_code == 200
    assert 'id="app-csp-origins"' not in response.text
    assert (
        'type="checkbox" name="csp_origins" value="https://api.github.com" checked'
        in response.text
    )
    assert (
        'type="checkbox" name="csp_origins" value="https://cdn.jsdelivr.net"'
        in response.text
    )
    assert 'value="https://cdn.jsdelivr.net" checked' not in response.text


@pytest.mark.asyncio
async def test_edit_post_preserves_existing_out_of_list_origin():
    datasette = Datasette(memory=True, config=CSP_ALLOWLIST_CONFIG)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Existing app",
        description="",
        html="",
        csp_origins=["https://api.github.com"],
    )

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/edit",
        actor={"id": "alice"},
        data={
            "name": "Existing app",
            "description": "",
            "html": "",
            "csp_origins_present": "1",
            "csp_origins": ["https://api.github.com", "https://cdn.jsdelivr.net"],
        },
    )

    assert response.status_code == 302
    assert await registry.get_csp_origins(app["id"]) == [
        "https://api.github.com",
        "https://cdn.jsdelivr.net",
    ]


@pytest.mark.asyncio
async def test_edit_post_can_remove_existing_origin():
    datasette = Datasette(memory=True, config=CSP_ALLOWLIST_CONFIG)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Existing app",
        description="",
        html="",
        csp_origins=["https://api.github.com"],
    )

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/edit",
        actor={"id": "alice"},
        data={
            "name": "Existing app",
            "description": "",
            "html": "",
            "csp_origins_present": "1",
        },
    )

    assert response.status_code == 302
    assert await registry.get_csp_origins(app["id"]) == []


@pytest.mark.asyncio
async def test_edit_post_rejects_new_arbitrary_origin_without_permission():
    datasette = Datasette(memory=True, config=CSP_ALLOWLIST_CONFIG)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Existing app",
        description="",
        html="",
        csp_origins=["https://api.github.com"],
    )

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/edit",
        actor={"id": "alice"},
        data={
            "name": "Existing app",
            "description": "",
            "html": "",
            "csp_origins_present": "1",
            "csp_origins": [
                "https://api.github.com",
                "https://attacker.example.com",
            ],
        },
    )

    assert response.status_code == 403
    assert await registry.get_csp_origins(app["id"]) == ["https://api.github.com"]


@pytest.mark.asyncio
async def test_edit_post_allows_arbitrary_origin_with_permission():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"apps-set-csp": {"id": "alice"}}},
    )
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Existing app",
        description="",
        html="",
    )

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/edit",
        actor={"id": "alice"},
        data={
            "name": "Existing app",
            "description": "",
            "html": "",
            "csp_origins": "https://api.github.com\n",
        },
    )

    assert response.status_code == 302
    assert await registry.get_csp_origins(app["id"]) == ["https://api.github.com"]
