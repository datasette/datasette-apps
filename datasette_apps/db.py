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

CREATE TABLE IF NOT EXISTS app_revisions (
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
        if "apps" in existing_tables:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_sql_databases (
                    app_id TEXT NOT NULL REFERENCES apps(id),
                    database_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (app_id, database_name)
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_csp_origins (
                    id INTEGER PRIMARY KEY,
                    app_id TEXT NOT NULL REFERENCES apps(id),
                    directive TEXT NOT NULL DEFAULT 'connect-src',
                    origin TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (directive IN ('connect-src')),
                    UNIQUE (app_id, directive, origin)
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_revisions (
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
                )
                """)
        if "apps" in existing_tables and "app_versions" in existing_tables:
            conn.execute("""
                INSERT INTO app_revisions (
                    app_id, version, actor_id, name, description, html,
                    is_private, sql_databases, csp_origins, changed_fields,
                    created_at
                )
                SELECT
                    apps.id,
                    1,
                    apps.actor_id,
                    apps.name,
                    apps.description,
                    COALESCE(
                        (
                            SELECT app_versions.html
                            FROM app_versions
                            WHERE app_versions.app_id = apps.id
                              AND app_versions.version = apps.current_version
                            LIMIT 1
                        ),
                        (
                            SELECT app_versions.html
                            FROM app_versions
                            WHERE app_versions.app_id = apps.id
                            ORDER BY app_versions.version DESC
                            LIMIT 1
                        ),
                        ''
                    ),
                    apps.is_private,
                    COALESCE(
                        (
                            SELECT json_group_array(database_name)
                            FROM (
                                SELECT database_name
                                FROM app_sql_databases
                                WHERE app_sql_databases.app_id = apps.id
                                ORDER BY database_name
                            )
                        ),
                        '[]'
                    ),
                    COALESCE(
                        (
                            SELECT json_group_array(origin)
                            FROM (
                                SELECT origin
                                FROM app_csp_origins
                                WHERE app_csp_origins.app_id = apps.id
                                  AND directive = 'connect-src'
                                ORDER BY origin
                            )
                        ),
                        '[]'
                    ),
                    '["name", "description", "html", "is_private", "sql_databases", "csp_origins"]',
                    apps.updated_at
                FROM apps
                WHERE apps.external = 0
                  AND NOT EXISTS (
                      SELECT 1
                      FROM app_revisions
                      WHERE app_revisions.app_id = apps.id
                  )
                """)
            conn.execute("""
                UPDATE app_revisions
                SET html = (
                    SELECT app_versions.html
                    FROM app_versions
                    WHERE app_versions.app_id = app_revisions.app_id
                    ORDER BY app_versions.version DESC
                    LIMIT 1
                )
                WHERE version = 1
                  AND (html IS NULL OR html = '')
                  AND EXISTS (
                      SELECT 1
                      FROM app_versions
                      WHERE app_versions.app_id = app_revisions.app_id
                        AND app_versions.html != ''
                  )
                """)
            conn.execute("UPDATE apps SET current_version = 1 WHERE external = 0")
        if "apps" in existing_tables:
            conn.execute("""
            INSERT INTO app_revisions (
                app_id, version, actor_id, name, description, html,
                is_private, sql_databases, csp_origins, changed_fields,
                created_at
            )
            SELECT
                apps.id,
                1,
                apps.actor_id,
                apps.name,
                apps.description,
                '',
                apps.is_private,
                COALESCE(
                    (
                        SELECT json_group_array(database_name)
                        FROM (
                            SELECT database_name
                            FROM app_sql_databases
                            WHERE app_sql_databases.app_id = apps.id
                            ORDER BY database_name
                        )
                    ),
                    '[]'
                ),
                COALESCE(
                    (
                        SELECT json_group_array(origin)
                        FROM (
                            SELECT origin
                            FROM app_csp_origins
                            WHERE app_csp_origins.app_id = apps.id
                              AND directive = 'connect-src'
                            ORDER BY origin
                        )
                    ),
                    '[]'
                ),
                '["name", "description", "html", "is_private", "sql_databases", "csp_origins"]',
                apps.updated_at
            FROM apps
            WHERE apps.external = 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM app_revisions
                  WHERE app_revisions.app_id = apps.id
              )
            """)
            conn.execute("""
            UPDATE apps
            SET current_version = 1
            WHERE external = 0
              AND current_version IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM app_revisions
                  WHERE app_revisions.app_id = apps.id
                    AND app_revisions.version = apps.current_version
              )
            """)
        conn.execute("DROP TABLE IF EXISTS app_versions")
        conn.executescript(SCHEMA)

    await internal_db.execute_write_fn(create_schema)
