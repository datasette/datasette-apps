import pytest

from datasette.app import Datasette
from datasette_apps import Registry


@pytest.mark.asyncio
async def test_registry_add_get_list_and_remove_external_app():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)

    await registry.add_app(
        id="myplugin:one",
        name="Plugin app",
        description="A useful app",
        path="/-/plugin-app",
        source="myplugin",
        metadata={"color": "blue"},
    )

    app = await registry.get_app("myplugin:one")
    assert app["id"] == "myplugin:one"
    assert app["external"] == 1
    assert app["name"] == "Plugin app"
    assert app["description"] == "A useful app"
    assert app["path"] == "/-/plugin-app"
    assert app["source"] == "myplugin"
    assert app["metadata"] == {"color": "blue"}
    assert app["is_private"] == 0

    apps = await registry.list_apps()
    assert [app["id"] for app in apps] == ["myplugin:one"]

    await registry.remove_app("myplugin:one")
    assert await registry.get_app("myplugin:one") is None


@pytest.mark.asyncio
async def test_registry_search_uses_fts5():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)

    await registry.add_apps(
        [
            {
                "id": "plugin:weather",
                "name": "Weather dashboard",
                "description": "Forecasts and climate",
                "path": "/-/weather",
            },
            {
                "id": "plugin:news",
                "name": "News reader",
                "description": "Headlines and authors",
                "path": "/-/news",
            },
        ],
        source="plugin",
    )

    apps = await registry.list_apps(q="forecast")
    assert [app["id"] for app in apps] == ["plugin:weather"]


@pytest.mark.asyncio
async def test_registry_records_access_and_pins_apps():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    await registry.add_app(
        id="plugin:one",
        name="One",
        description="",
        path="/-/one",
        source="plugin",
    )

    await registry.record_access("alice", "plugin:one")
    await registry.record_access("alice", "plugin:one")
    await registry.set_pinned("alice", "plugin:one", True)

    state = await registry.get_user_state("alice", "plugin:one")
    assert state["access_count"] == 2
    assert state["last_accessed_at"] is not None
    assert state["pinned_at"] is not None

    await registry.set_pinned("alice", "plugin:one", False)
    state = await registry.get_user_state("alice", "plugin:one")
    assert state["pinned_at"] is None


@pytest.mark.asyncio
async def test_registry_stores_sql_database_allow_list():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="SQL app",
        description="",
        html="",
    )

    await registry.set_sql_databases(app["id"], ["_memory", "_memory", "content"])

    assert await registry.get_sql_databases(app["id"]) == ["_memory", "content"]

    await registry.set_sql_databases(app["id"], [])

    assert await registry.get_sql_databases(app["id"]) == []


@pytest.mark.asyncio
async def test_registry_create_stored_app_and_save_versions():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)

    app = await registry.create_stored_app(
        actor_id="alice",
        name="My app",
        description="An HTML app",
        html="<h1>Hello</h1>",
    )

    assert app["external"] == 0
    assert len(app["id"]) == 26
    assert app["id"] == app["id"].lower()
    assert app["path"] == f"/-/apps/{app['id']}"
    assert app["current_version"] == 1
    assert app["actor_id"] == "alice"
    assert app["is_private"] == 1

    version = await registry.get_current_version(app["id"])
    assert version["version"] == 1
    assert version["html"] == "<h1>Hello</h1>"

    await registry.update_stored_app(
        app["id"], "My app", "An HTML app", "<h1>Updated</h1>"
    )
    app = await registry.get_app(app["id"])
    version = await registry.get_current_version(app["id"])
    assert app["current_version"] == 2
    assert version["version"] == 2
    assert version["html"] == "<h1>Updated</h1>"


@pytest.mark.asyncio
async def test_ensure_tables_migrates_old_access_rows_to_is_private():
    datasette = Datasette(memory=True)

    def setup_old_schema(conn):
        conn.executescript("""
            CREATE TABLE apps (
                id TEXT PRIMARY KEY,
                external INTEGER NOT NULL DEFAULT 0,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                actor_id TEXT,
                current_version INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (external IN (0, 1))
            );
            CREATE TABLE app_access (
                id INTEGER PRIMARY KEY,
                app_id TEXT REFERENCES apps(id),
                action TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                subject_id TEXT,
                allow INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (subject_type IN ('authenticated')),
                CHECK (allow IN (0, 1))
            );
            INSERT INTO apps (
                id, external, name, description, path, source, metadata,
                actor_id, current_version, created_at, updated_at
            )
            VALUES
                ('private-app', 0, 'Private', '', '/-/apps/private-app', 'datasette-apps', '{}', 'alice', 1, '2026-01-01', '2026-01-01'),
                ('shared-app', 0, 'Shared', '', '/-/apps/shared-app', 'datasette-apps', '{}', 'alice', 1, '2026-01-01', '2026-01-01'),
                ('external-app', 1, 'External', '', '/-/external', 'plugin', '{}', NULL, NULL, '2026-01-01', '2026-01-01');
            INSERT INTO app_access (
                app_id, action, subject_type, subject_id, allow, created_at, updated_at
            )
            VALUES (
                'shared-app', 'view-app', 'authenticated', NULL, 1, '2026-01-01', '2026-01-01'
            );
            """)

    await datasette.get_internal_database().execute_write_fn(setup_old_schema)
    registry = Registry(datasette)
    await registry.ensure_tables()

    assert await registry.get_access_mode("private-app") == "private"
    assert await registry.get_access_mode("shared-app") == "not-private"
    assert await registry.get_access_mode("external-app") == "not-private"
