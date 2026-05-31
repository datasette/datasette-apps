from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
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

CREATE TABLE IF NOT EXISTS app_versions (
    app_id TEXT NOT NULL REFERENCES apps(id),
    version INTEGER NOT NULL,
    html TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (app_id, version)
);

CREATE INDEX IF NOT EXISTS idx_apps_updated ON apps(updated_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_apps_external_updated ON apps(external, updated_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_apps_source ON apps(source);

CREATE VIRTUAL TABLE IF NOT EXISTS apps_fts
USING fts5(name, description, source, content='apps', content_rowid='rowid');

CREATE TRIGGER IF NOT EXISTS apps_ai AFTER INSERT ON apps BEGIN
    INSERT INTO apps_fts(rowid, name, description, source)
    VALUES (new.rowid, new.name, new.description, new.source);
END;

CREATE TRIGGER IF NOT EXISTS apps_ad AFTER DELETE ON apps BEGIN
    INSERT INTO apps_fts(apps_fts, rowid, name, description, source)
    VALUES ('delete', old.rowid, old.name, old.description, old.source);
END;

CREATE TRIGGER IF NOT EXISTS apps_au AFTER UPDATE ON apps BEGIN
    INSERT INTO apps_fts(apps_fts, rowid, name, description, source)
    VALUES ('delete', old.rowid, old.name, old.description, old.source);
    INSERT INTO apps_fts(rowid, name, description, source)
    VALUES (new.rowid, new.name, new.description, new.source);
END;

CREATE TABLE IF NOT EXISTS app_access (
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

CREATE INDEX IF NOT EXISTS idx_app_access_lookup
    ON app_access(action, app_id, subject_type, subject_id);

CREATE TABLE IF NOT EXISTS app_sql_databases (
    app_id TEXT NOT NULL REFERENCES apps(id),
    database_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (app_id, database_name)
);

CREATE INDEX IF NOT EXISTS idx_app_sql_databases_app
    ON app_sql_databases(app_id, database_name);

CREATE TABLE IF NOT EXISTS app_csp_origins (
    id INTEGER PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES apps(id),
    directive TEXT NOT NULL DEFAULT 'connect-src',
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (directive IN ('connect-src')),
    UNIQUE (app_id, directive, origin)
);

CREATE INDEX IF NOT EXISTS idx_app_csp_origins_app
    ON app_csp_origins(app_id, directive);

CREATE TABLE IF NOT EXISTS app_user_state (
    actor_id TEXT NOT NULL,
    app_id TEXT NOT NULL REFERENCES apps(id),
    last_accessed_at TEXT,
    pinned_at TEXT,
    access_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (actor_id, app_id)
);

CREATE INDEX IF NOT EXISTS idx_app_user_state_actor_pinned
    ON app_user_state(actor_id, pinned_at DESC, last_accessed_at DESC, app_id);

CREATE INDEX IF NOT EXISTS idx_app_user_state_actor_recent
    ON app_user_state(actor_id, last_accessed_at DESC, app_id);
"""


async def ensure_tables(datasette):
    internal_db = datasette.get_internal_database()

    def create_schema(conn):
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "apps" in existing_tables:
            app_columns = {row[1] for row in conn.execute("PRAGMA table_info(apps)")}
            if "is_private" not in app_columns:
                conn.execute(
                    "ALTER TABLE apps ADD COLUMN is_private INTEGER NOT NULL DEFAULT 1"
                )
                conn.execute("UPDATE apps SET is_private = 0 WHERE external = 1")
                if "app_access" in existing_tables:
                    conn.execute("""
                        UPDATE apps
                        SET is_private = 0
                        WHERE id IN (
                            SELECT app_id
                            FROM app_access
                            WHERE action = 'view-app'
                              AND subject_type = 'authenticated'
                              AND allow = 1
                        )
                        """)
        conn.executescript(SCHEMA)

    await internal_db.execute_write_fn(create_schema)
