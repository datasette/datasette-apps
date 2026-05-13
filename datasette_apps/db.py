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
    current_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (external IN (0, 1))
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
    CHECK (subject_type IN ('authenticated', 'actor')),
    CHECK (allow IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_app_access_lookup
    ON app_access(action, app_id, subject_type, subject_id);

CREATE TABLE IF NOT EXISTS app_data_permissions (
    id INTEGER PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES apps(id),
    permission_type TEXT NOT NULL,
    database_name TEXT NOT NULL,
    resource_type TEXT NOT NULL DEFAULT 'table',
    resource_name TEXT NOT NULL,
    columns TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (permission_type IN ('table-read')),
    CHECK (resource_type IN ('table', 'view')),
    UNIQUE (app_id, permission_type, database_name, resource_type, resource_name)
);

CREATE INDEX IF NOT EXISTS idx_app_data_permissions_app
    ON app_data_permissions(app_id, permission_type, database_name, resource_type);

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

CREATE TABLE IF NOT EXISTS app_capability_grants (
    app_id TEXT NOT NULL REFERENCES apps(id),
    capability TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (app_id, capability)
);

CREATE INDEX IF NOT EXISTS idx_app_capability_grants_capability
    ON app_capability_grants(capability, app_id);

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
        conn.executescript(SCHEMA)

    await internal_db.execute_write_fn(create_schema)
