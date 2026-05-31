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
async def test_registry_uses_app_revisions_delta_log_for_settings_changes():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="My app",
        description="Initial",
        html="<h1>Hello</h1>",
    )

    await registry.set_access_mode(app["id"], "not-private")

    app = await registry.get_app(app["id"])
    current = await registry.get_current_version(app["id"])
    second = await registry.get_version(app["id"], 2)
    revisions = await registry.list_versions(app["id"])

    assert app["current_version"] == 2
    assert current["version"] == 2
    assert current["html"] == "<h1>Hello</h1>"
    assert current["is_private"] == 0
    assert second["html"] == "<h1>Hello</h1>"
    assert second["revision_html"] is None
    assert second["changed_fields"] == ["is_private"]
    assert [revision["version"] for revision in revisions] == [2, 1]

    rows = await datasette.get_internal_database().execute(
        """
        SELECT version, html, is_private, changed_fields
        FROM app_revisions
        WHERE app_id = ?
        ORDER BY version
        """,
        [app["id"]],
    )
    assert [dict(row) for row in rows.rows] == [
        {
            "version": 1,
            "html": "<h1>Hello</h1>",
            "is_private": 1,
            "changed_fields": '["name", "description", "html", "is_private", "sql_databases", "csp_origins"]',
        },
        {
            "version": 2,
            "html": None,
            "is_private": 0,
            "changed_fields": '["is_private"]',
        },
    ]


@pytest.mark.asyncio
async def test_registry_records_data_and_network_access_revision_deltas():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="My app",
        description="Initial",
        html="<h1>Hello</h1>",
    )

    await registry.set_sql_databases(app["id"], ["_memory"])
    await registry.set_csp_origins(app["id"], ["https://api.github.com"])

    current = await registry.get_current_version(app["id"])
    sql_revision = await registry.get_version(app["id"], 2)
    csp_revision = await registry.get_version(app["id"], 3)

    assert current["version"] == 3
    assert current["html"] == "<h1>Hello</h1>"
    assert current["sql_databases"] == ["_memory"]
    assert current["csp_origins"] == ["https://api.github.com"]
    assert sql_revision["revision_html"] is None
    assert sql_revision["changed_fields"] == ["sql_databases"]
    assert sql_revision["sql_databases"] == ["_memory"]
    assert csp_revision["revision_html"] is None
    assert csp_revision["changed_fields"] == ["csp_origins"]
    assert csp_revision["csp_origins"] == ["https://api.github.com"]


@pytest.mark.asyncio
async def test_ensure_tables_drops_old_app_versions_table():
    datasette = Datasette(memory=True)

    def create_old_table(conn):
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
                is_private INTEGER NOT NULL DEFAULT 1,
                current_version INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (external IN (0, 1)),
                CHECK (is_private IN (0, 1))
            );
            CREATE TABLE app_versions (
                app_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                html TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (app_id, version)
            );
            INSERT INTO apps (
                id, external, name, description, path, source, metadata,
                actor_id, is_private, current_version, created_at, updated_at
            )
            VALUES (
                'old', 0, 'Old', '', '/-/apps/old', 'datasette-apps', '{}',
                'alice', 1, 1, '2026-01-01', '2026-01-01'
            );
            INSERT INTO app_versions (app_id, version, html, created_at)
            VALUES ('old', 1, '<h1>Old</h1>', '2026-01-01');
            """)

    await datasette.get_internal_database().execute_write_fn(create_old_table)
    await Registry(datasette).ensure_tables()

    table_rows = await datasette.get_internal_database().execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN ('app_versions', 'app_revisions')
        ORDER BY name
        """)
    assert [row["name"] for row in table_rows.rows] == ["app_revisions"]


@pytest.mark.asyncio
async def test_ensure_tables_collapses_old_app_versions_to_one_revision():
    datasette = Datasette(memory=True)

    def create_old_app(conn):
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
                is_private INTEGER NOT NULL DEFAULT 1,
                current_version INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (external IN (0, 1)),
                CHECK (is_private IN (0, 1))
            );
            CREATE TABLE app_versions (
                app_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                html TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (app_id, version)
            );
            CREATE TABLE app_sql_databases (
                app_id TEXT NOT NULL,
                database_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (app_id, database_name)
            );
            CREATE TABLE app_csp_origins (
                id INTEGER PRIMARY KEY,
                app_id TEXT NOT NULL,
                directive TEXT NOT NULL DEFAULT 'connect-src',
                origin TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (directive IN ('connect-src')),
                UNIQUE (app_id, directive, origin)
            );
            INSERT INTO apps (
                id, external, name, description, path, source, metadata,
                actor_id, is_private, current_version, created_at, updated_at
            )
            VALUES (
                'old-app', 0, 'Old name', 'Old description',
                '/-/apps/old-app', 'datasette-apps', '{}',
                'alice', 0, 2, '2026-01-01', '2026-01-03'
            );
            INSERT INTO app_versions (app_id, version, html, created_at)
            VALUES
                ('old-app', 1, '<h1>First</h1>', '2026-01-01'),
                ('old-app', 2, '<h1>Second</h1>', '2026-01-02');
            INSERT INTO app_sql_databases (
                app_id, database_name, created_at, updated_at
            )
            VALUES ('old-app', 'data', '2026-01-01', '2026-01-01');
            INSERT INTO app_csp_origins (
                app_id, directive, origin, created_at, updated_at
            )
            VALUES (
                'old-app', 'connect-src', 'https://api.example.com',
                '2026-01-01', '2026-01-01'
            );
            """)

    await datasette.get_internal_database().execute_write_fn(create_old_app)
    registry = Registry(datasette)
    await registry.ensure_tables()

    app = await registry.get_app("old-app")
    current = await registry.get_current_version("old-app")
    revisions = await registry.list_versions("old-app")

    assert app["current_version"] == 1
    assert current["version"] == 1
    assert current["name"] == "Old name"
    assert current["description"] == "Old description"
    assert current["html"] == "<h1>Second</h1>"
    assert current["is_private"] == 0
    assert current["sql_databases"] == ["data"]
    assert current["csp_origins"] == ["https://api.example.com"]
    assert [revision["version"] for revision in revisions] == [1]


@pytest.mark.asyncio
async def test_ensure_tables_recovers_html_if_partial_revision_migration_exists():
    datasette = Datasette(memory=True)

    def create_partial_migration(conn):
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
                is_private INTEGER NOT NULL DEFAULT 1,
                current_version INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (external IN (0, 1)),
                CHECK (is_private IN (0, 1))
            );
            CREATE TABLE app_versions (
                app_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                html TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (app_id, version)
            );
            CREATE TABLE app_revisions (
                app_id TEXT NOT NULL REFERENCES apps(id),
                version INTEGER NOT NULL,
                actor_id TEXT,
                name TEXT,
                description TEXT,
                html TEXT,
                is_private INTEGER,
                sql_databases TEXT,
                csp_origins TEXT,
                changed_fields TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                PRIMARY KEY (app_id, version),
                CHECK (is_private IN (0, 1) OR is_private IS NULL)
            );
            INSERT INTO apps (
                id, external, name, description, path, source, metadata,
                actor_id, is_private, current_version, created_at, updated_at
            )
            VALUES (
                'partial-app', 0, 'Partial', '', '/-/apps/partial-app',
                'datasette-apps', '{}', 'alice', 0, 1,
                '2026-01-01', '2026-01-03'
            );
            INSERT INTO app_versions (app_id, version, html, created_at)
            VALUES
                ('partial-app', 1, '<h1>Old HTML</h1>', '2026-01-01'),
                ('partial-app', 2, '<h1>Latest HTML</h1>', '2026-01-02');
            INSERT INTO app_revisions (
                app_id, version, actor_id, name, description, html,
                is_private, sql_databases, csp_origins, changed_fields,
                created_at
            )
            VALUES (
                'partial-app', 1, 'alice', 'Partial', '', '',
                0, '[]', '[]',
                '["name", "description", "html", "is_private", "sql_databases", "csp_origins"]',
                '2026-01-03'
            );
            """)

    await datasette.get_internal_database().execute_write_fn(create_partial_migration)
    registry = Registry(datasette)
    await registry.ensure_tables()

    current = await registry.get_current_version("partial-app")
    table_rows = await datasette.get_internal_database().execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'app_versions'
        """)

    assert current["html"] == "<h1>Latest HTML</h1>"
    assert [row["name"] for row in table_rows.rows] == []


@pytest.mark.asyncio
async def test_ensure_tables_creates_placeholder_revision_for_apps_without_history():
    datasette = Datasette(memory=True)

    def create_app_without_history(conn):
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
                is_private INTEGER NOT NULL DEFAULT 1,
                current_version INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (external IN (0, 1)),
                CHECK (is_private IN (0, 1))
            );
            INSERT INTO apps (
                id, external, name, description, path, source, metadata,
                actor_id, is_private, current_version, created_at, updated_at
            )
            VALUES (
                'historyless', 0, 'Historyless', '',
                '/-/apps/historyless', 'datasette-apps', '{}',
                'alice', 0, 3, '2026-01-01', '2026-01-03'
            );
            """)

    await datasette.get_internal_database().execute_write_fn(create_app_without_history)
    registry = Registry(datasette)
    await registry.ensure_tables()

    app = await registry.get_app("historyless")
    current = await registry.get_current_version("historyless")

    assert app["current_version"] == 1
    assert current["version"] == 1
    assert current["name"] == "Historyless"
    assert current["html"] == ""
    assert current["is_private"] == 0
    assert current["changed_fields"] == [
        "name",
        "description",
        "html",
        "is_private",
        "sql_databases",
        "csp_origins",
    ]


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
