from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .db import ensure_tables
from .ids import monotonic_ulid
from .csp import normalize_connect_origin


def _now():
    return datetime.now(timezone.utc).isoformat()


def _decode_json(value, fallback):
    if not value:
        return fallback
    return json.loads(value)


def _row_to_app(row):
    if row is None:
        return None
    app = dict(row)
    app["metadata"] = _decode_json(app["metadata"], {})
    app["stored_queries"] = _decode_json(app.get("stored_queries"), [])
    return app


def _row_to_state(row):
    if row is None:
        return None
    return dict(row)


def _row_to_version(row):
    if row is None:
        return None
    version = dict(row)
    version["changed_fields"] = _decode_json(version.get("changed_fields"), [])
    version["sql_databases"] = _decode_json(version.get("sql_databases"), [])
    version["stored_queries"] = _decode_json(version.get("stored_queries"), [])
    version["csp_origins"] = _decode_json(version.get("csp_origins"), [])
    if version.get("revision_sql_databases") is not None:
        version["revision_sql_databases"] = _decode_json(
            version["revision_sql_databases"], []
        )
    if version.get("revision_stored_queries") is not None:
        version["revision_stored_queries"] = _decode_json(
            version["revision_stored_queries"], []
        )
    if version.get("revision_csp_origins") is not None:
        version["revision_csp_origins"] = _decode_json(
            version["revision_csp_origins"], []
        )
    return version


APP_REVISION_VALUE_FIELDS = (
    "name",
    "description",
    "html",
    "is_private",
    "sql_databases",
    "stored_queries",
    "csp_origins",
)

APP_REVISION_CHANGED_FIELDS = [
    "name",
    "description",
    "html",
    "is_private",
    "sql_databases",
    "stored_queries",
    "csp_origins",
]

_UNSET = object()


def _revision_db_value(field, value):
    if field in {"sql_databases", "stored_queries", "csp_origins"}:
        return json.dumps(value, sort_keys=True)
    if field == "is_private":
        return int(bool(value))
    return value


def _insert_revision(conn, app_id, changes, now, actor_id=None):
    if not changes:
        return None
    row = conn.execute(
        "SELECT current_version FROM apps WHERE id = ?", (app_id,)
    ).fetchone()
    next_version = int(row["current_version"] or 0) + 1
    changed_fields = [
        field for field in APP_REVISION_CHANGED_FIELDS if field in changes
    ]
    values = {
        field: _revision_db_value(field, changes[field]) if field in changes else None
        for field in APP_REVISION_VALUE_FIELDS
    }
    conn.execute(
        """
        INSERT INTO app_revisions (
            app_id, version, actor_id, name, description, html, is_private,
            sql_databases, stored_queries, csp_origins, changed_fields, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            app_id,
            next_version,
            actor_id,
            values["name"],
            values["description"],
            values["html"],
            values["is_private"],
            values["sql_databases"],
            values["stored_queries"],
            values["csp_origins"],
            json.dumps(changed_fields),
            now,
        ),
    )
    conn.execute(
        """
        UPDATE apps
        SET current_version = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (next_version, now, app_id),
    )
    return next_version


def _fts_query(q):
    tokens = re.findall(r"[\w]+", q or "")
    if not tokens:
        return None
    return " ".join(token + "*" for token in tokens)


def _validate_external_id(id):
    if not id or "/" in id or "?" in id or "#" in id or any(c.isspace() for c in id):
        raise ValueError("External app IDs must be non-empty safe path segments")


APP_COLUMNS = """
    apps.id,
    apps.external,
    apps.name,
    apps.description,
    apps.path,
    apps.source,
    apps.metadata,
    apps.actor_id,
    apps.is_private,
    apps.stored_queries,
    apps.current_version,
    apps.deleted_at,
    apps.deleted_actor_id,
    apps.created_at,
    apps.updated_at
"""

APP_REVISION_RESOLVED_COLUMNS = """
    app_revisions.app_id,
    app_revisions.version,
    app_revisions.actor_id,
    app_revisions.name AS revision_name,
    app_revisions.description AS revision_description,
    app_revisions.html AS revision_html,
    app_revisions.is_private AS revision_is_private,
    app_revisions.sql_databases AS revision_sql_databases,
    app_revisions.stored_queries AS revision_stored_queries,
    app_revisions.csp_origins AS revision_csp_origins,
    app_revisions.changed_fields,
    app_revisions.created_at,
    (
        SELECT previous.name
        FROM app_revisions AS previous
        WHERE previous.app_id = app_revisions.app_id
          AND previous.version <= app_revisions.version
          AND previous.name IS NOT NULL
        ORDER BY previous.version DESC
        LIMIT 1
    ) AS name,
    (
        SELECT previous.description
        FROM app_revisions AS previous
        WHERE previous.app_id = app_revisions.app_id
          AND previous.version <= app_revisions.version
          AND previous.description IS NOT NULL
        ORDER BY previous.version DESC
        LIMIT 1
    ) AS description,
    (
        SELECT previous.html
        FROM app_revisions AS previous
        WHERE previous.app_id = app_revisions.app_id
          AND previous.version <= app_revisions.version
          AND previous.html IS NOT NULL
        ORDER BY previous.version DESC
        LIMIT 1
    ) AS html,
    (
        SELECT previous.is_private
        FROM app_revisions AS previous
        WHERE previous.app_id = app_revisions.app_id
          AND previous.version <= app_revisions.version
          AND previous.is_private IS NOT NULL
        ORDER BY previous.version DESC
        LIMIT 1
    ) AS is_private,
    (
        SELECT previous.sql_databases
        FROM app_revisions AS previous
        WHERE previous.app_id = app_revisions.app_id
          AND previous.version <= app_revisions.version
          AND previous.sql_databases IS NOT NULL
        ORDER BY previous.version DESC
        LIMIT 1
    ) AS sql_databases,
    (
        SELECT previous.stored_queries
        FROM app_revisions AS previous
        WHERE previous.app_id = app_revisions.app_id
          AND previous.version <= app_revisions.version
          AND previous.stored_queries IS NOT NULL
        ORDER BY previous.version DESC
        LIMIT 1
    ) AS stored_queries,
    (
        SELECT previous.csp_origins
        FROM app_revisions AS previous
        WHERE previous.app_id = app_revisions.app_id
          AND previous.version <= app_revisions.version
          AND previous.csp_origins IS NOT NULL
        ORDER BY previous.version DESC
        LIMIT 1
    ) AS csp_origins
"""

APP_USER_STATE_COLUMNS = """
    actor_id,
    app_id,
    last_accessed_at,
    pinned_at,
    access_count
"""


class Registry:
    def __init__(self, datasette):
        self.datasette = datasette

    async def ensure_tables(self):
        await ensure_tables(self.datasette)

    @property
    def db(self):
        return self.datasette.get_internal_database()

    async def add_app(
        self,
        id,
        name,
        description,
        path,
        source=None,
        metadata=None,
    ):
        _validate_external_id(id)
        await self.ensure_tables()
        now = _now()
        await self.db.execute_write(
            """
            INSERT INTO apps (
                id, external, name, description, path, source, metadata,
                actor_id, is_private, stored_queries, current_version, created_at,
                updated_at
            )
            VALUES (
                :id, 1, :name, :description, :path, :source, :metadata,
                NULL, 0, '[]', NULL, :now, :now
            )
            ON CONFLICT(id) DO UPDATE SET
                external = 1,
                name = excluded.name,
                description = excluded.description,
                path = excluded.path,
                source = excluded.source,
                metadata = excluded.metadata,
                is_private = excluded.is_private,
                updated_at = excluded.updated_at
            """,
            {
                "id": id,
                "name": name,
                "description": description or "",
                "path": path,
                "source": source or "",
                "metadata": json.dumps(metadata or {}, sort_keys=True),
                "now": now,
            },
        )

    async def add_apps(self, apps, source=None):
        for app in apps:
            await self.add_app(
                id=app["id"],
                name=app["name"],
                description=app.get("description") or "",
                path=app["path"],
                source=app.get("source") or source,
                metadata=app.get("metadata") or {},
            )

    async def create_stored_app(
        self,
        actor_id,
        name,
        description,
        html,
        is_private=True,
        sql_databases=None,
        stored_queries=None,
        csp_origins=None,
    ):
        await self.ensure_tables()
        app_id = monotonic_ulid()
        now = _now()
        sql_databases = sorted(dict.fromkeys(sql_databases or []))
        stored_queries = sorted(dict.fromkeys(stored_queries or []))
        csp_origins = sorted(
            dict.fromkeys(
                normalize_connect_origin(origin) for origin in csp_origins or []
            )
        )
        is_private = int(bool(is_private))

        def create(conn):
            conn.execute(
                """
                INSERT INTO apps (
                    id, external, name, description, path, source, metadata,
                    actor_id, is_private, stored_queries, current_version, created_at,
                    updated_at
                )
                VALUES (?, 0, ?, ?, ?, 'datasette-apps', '{}', ?, ?, ?, 1, ?, ?)
                """,
                (
                    app_id,
                    name,
                    description or "",
                    f"/-/apps/{app_id}",
                    actor_id,
                    is_private,
                    json.dumps(stored_queries),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO app_revisions (
                    app_id, version, actor_id, name, description, html,
                    is_private, sql_databases, stored_queries, csp_origins,
                    changed_fields, created_at
                )
                VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id,
                    actor_id,
                    name,
                    description or "",
                    html,
                    is_private,
                    json.dumps(sql_databases),
                    json.dumps(stored_queries),
                    json.dumps(csp_origins),
                    json.dumps(APP_REVISION_CHANGED_FIELDS),
                    now,
                ),
            )
            conn.executemany(
                """
                INSERT INTO app_sql_databases (
                    app_id, database_name, created_at, updated_at
                )
                VALUES (?, ?, ?, ?)
                """,
                [(app_id, database_name, now, now) for database_name in sql_databases],
            )
            conn.executemany(
                """
                INSERT INTO app_csp_origins (
                    app_id, directive, origin, created_at, updated_at
                )
                VALUES (?, 'connect-src', ?, ?, ?)
                """,
                [(app_id, origin, now, now) for origin in csp_origins],
            )

        await self.db.execute_write_fn(create)
        return await self.get_app(app_id)

    async def update_stored_app(
        self,
        app_id,
        name,
        description,
        html,
        actor_id=None,
        *,
        is_private=_UNSET,
        sql_databases=_UNSET,
        stored_queries=_UNSET,
        csp_origins=_UNSET,
    ):
        await self.ensure_tables()
        now = _now()
        description = description or ""
        if is_private is not _UNSET:
            is_private = int(bool(is_private))
        if sql_databases is not _UNSET:
            sql_databases = sorted(dict.fromkeys(sql_databases))
        if stored_queries is not _UNSET:
            stored_queries = sorted(dict.fromkeys(stored_queries))
        if csp_origins is not _UNSET:
            csp_origins = sorted(
                dict.fromkeys(
                    normalize_connect_origin(origin) for origin in csp_origins
                )
            )

        def save(conn):
            row = conn.execute(
                """
                SELECT current_version, external, name, description, is_private,
                       stored_queries, deleted_at
                FROM apps
                WHERE id = ?
                """,
                (app_id,),
            ).fetchone()
            if row is None or row["deleted_at"] is not None:
                raise KeyError(app_id)
            if row["external"]:
                raise ValueError("External apps cannot be edited by datasette-apps")
            current_html_row = conn.execute(
                """
                SELECT html
                FROM app_revisions
                WHERE app_id = ?
                  AND html IS NOT NULL
                ORDER BY version DESC
                LIMIT 1
                """,
                (app_id,),
            ).fetchone()
            changes = {}
            if name != row["name"]:
                changes["name"] = name
            if description != row["description"]:
                changes["description"] = description
            if html != (current_html_row["html"] if current_html_row else None):
                changes["html"] = html
            if is_private is not _UNSET and is_private != row["is_private"]:
                changes["is_private"] = is_private
            if sql_databases is not _UNSET:
                current_sql_databases = [
                    current_row["database_name"]
                    for current_row in conn.execute(
                        """
                        SELECT database_name
                        FROM app_sql_databases
                        WHERE app_id = ?
                        ORDER BY database_name
                        """,
                        (app_id,),
                    ).fetchall()
                ]
                if current_sql_databases != sql_databases:
                    changes["sql_databases"] = sql_databases
            if stored_queries is not _UNSET:
                current_stored_queries = _decode_json(row["stored_queries"], [])
                if current_stored_queries != stored_queries:
                    changes["stored_queries"] = stored_queries
            if csp_origins is not _UNSET:
                current_csp_origins = [
                    current_row["origin"]
                    for current_row in conn.execute(
                        """
                        SELECT origin
                        FROM app_csp_origins
                        WHERE app_id = ? AND directive = 'connect-src'
                        ORDER BY origin
                        """,
                        (app_id,),
                    ).fetchall()
                ]
                if current_csp_origins != csp_origins:
                    changes["csp_origins"] = csp_origins
            if not changes:
                return
            if "sql_databases" in changes:
                conn.execute(
                    "DELETE FROM app_sql_databases WHERE app_id = ?", (app_id,)
                )
                conn.executemany(
                    """
                    INSERT INTO app_sql_databases (
                        app_id, database_name, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (app_id, database_name, now, now)
                        for database_name in sql_databases
                    ],
                )
            if "csp_origins" in changes:
                conn.execute(
                    """
                    DELETE FROM app_csp_origins
                    WHERE app_id = ? AND directive = 'connect-src'
                    """,
                    (app_id,),
                )
                conn.executemany(
                    """
                    INSERT INTO app_csp_origins (
                        app_id, directive, origin, created_at, updated_at
                    )
                    VALUES (?, 'connect-src', ?, ?, ?)
                    """,
                    [(app_id, origin, now, now) for origin in csp_origins],
                )
            _insert_revision(conn, app_id, changes, now, actor_id=actor_id)
            if {
                "name",
                "description",
                "is_private",
                "stored_queries",
            } & changes.keys():
                conn.execute(
                    """
                    UPDATE apps
                    SET name = ?,
                        description = ?,
                        is_private = ?,
                        stored_queries = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        description,
                        is_private if is_private is not _UNSET else row["is_private"],
                        (
                            json.dumps(stored_queries)
                            if stored_queries is not _UNSET
                            else row["stored_queries"]
                        ),
                        now,
                        app_id,
                    ),
                )

        await self.db.execute_write_fn(save)

    async def get_current_version(self, app_id, include_deleted=False):
        await self.ensure_tables()
        deleted_filter = "" if include_deleted else "AND apps.deleted_at IS NULL"
        result = await self.db.execute(
            f"""
            SELECT {APP_REVISION_RESOLVED_COLUMNS}
            FROM app_revisions
            JOIN apps ON apps.id = app_revisions.app_id
            WHERE apps.id = :app_id
              AND app_revisions.version = apps.current_version
              {deleted_filter}
            """,
            {"app_id": app_id},
        )
        return _row_to_version(result.first())

    async def get_version(self, app_id, version, include_deleted=False):
        await self.ensure_tables()
        deleted_filter = "" if include_deleted else "AND apps.deleted_at IS NULL"
        result = await self.db.execute(
            f"""
            SELECT {APP_REVISION_RESOLVED_COLUMNS}
            FROM app_revisions
            JOIN apps ON apps.id = app_revisions.app_id
            WHERE apps.id = :app_id
              AND apps.external = 0
              AND app_revisions.version = :version
              {deleted_filter}
            """,
            {"app_id": app_id, "version": version},
        )
        return _row_to_version(result.first())

    async def list_versions(self, app_id, include_deleted=False):
        await self.ensure_tables()
        deleted_filter = "" if include_deleted else "AND apps.deleted_at IS NULL"
        result = await self.db.execute(
            f"""
            SELECT {APP_REVISION_RESOLVED_COLUMNS}
            FROM app_revisions
            JOIN apps ON apps.id = app_revisions.app_id
            WHERE apps.id = :app_id
              AND apps.external = 0
              {deleted_filter}
            ORDER BY app_revisions.version DESC
            """,
            {"app_id": app_id},
        )
        return [_row_to_version(row) for row in result.rows]

    async def remove_app(self, id):
        await self.ensure_tables()
        await self.db.execute_write("DELETE FROM apps WHERE id = :id", {"id": id})

    async def delete_stored_app(self, app_id, actor_id=None):
        await self.ensure_tables()
        now = _now()

        def delete(conn):
            row = conn.execute(
                """
                SELECT external, deleted_at
                FROM apps
                WHERE id = ?
                """,
                (app_id,),
            ).fetchone()
            if row is None or row["deleted_at"] is not None:
                raise KeyError(app_id)
            if row["external"]:
                raise ValueError("External apps cannot be deleted by datasette-apps")
            conn.execute(
                """
                UPDATE apps
                SET deleted_at = ?,
                    deleted_actor_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, actor_id, now, app_id),
            )

        await self.db.execute_write_fn(delete)

    async def remove_apps_for_source(self, source):
        await self.ensure_tables()
        await self.db.execute_write(
            "DELETE FROM apps WHERE external = 1 AND source = :source",
            {"source": source},
        )

    async def get_app(self, id, include_deleted=False):
        await self.ensure_tables()
        deleted_filter = "" if include_deleted else "AND deleted_at IS NULL"
        result = await self.db.execute(
            f"""
            SELECT {APP_COLUMNS}
            FROM apps
            WHERE id = :id
              {deleted_filter}
            """,
            {"id": id},
        )
        return _row_to_app(result.first())

    async def list_apps(self, q=None, limit=20, offset=0, actor_id=None):
        await self.ensure_tables()
        fts = _fts_query(q)
        join_user_state = ""
        select_user_state = ""
        order_by = "apps.updated_at DESC, apps.id"
        params = {"limit": limit, "offset": offset}
        if actor_id:
            select_user_state = """
                , app_user_state.pinned_at AS pinned_at
                , app_user_state.last_accessed_at AS last_accessed_at
            """
            join_user_state = """
                LEFT JOIN app_user_state
                    ON app_user_state.app_id = apps.id
                   AND app_user_state.actor_id = :actor_id
            """
            order_by = """
                pinned_at IS NOT NULL DESC,
                CASE
                    WHEN pinned_at IS NOT NULL THEN COALESCE(last_accessed_at, pinned_at)
                    ELSE last_accessed_at
                END DESC,
                apps.updated_at DESC,
                apps.id
            """
            params["actor_id"] = actor_id
        if fts:
            sql = """
                SELECT {app_columns} {select_user_state}
                FROM apps
                JOIN apps_fts ON apps.rowid = apps_fts.rowid
                {join_user_state}
                WHERE apps_fts MATCH :q
                  AND apps.deleted_at IS NULL
                ORDER BY {order_by}
                LIMIT :limit OFFSET :offset
            """.format(
                app_columns=APP_COLUMNS,
                select_user_state=select_user_state,
                join_user_state=join_user_state,
                order_by=order_by,
            )
            params["q"] = fts
        else:
            sql = """
                SELECT {app_columns} {select_user_state}
                FROM apps
                {join_user_state}
                WHERE apps.deleted_at IS NULL
                ORDER BY {order_by}
                LIMIT :limit OFFSET :offset
            """.format(
                app_columns=APP_COLUMNS,
                select_user_state=select_user_state,
                join_user_state=join_user_state,
                order_by=order_by,
            )
        result = await self.db.execute(sql, params)
        return [_row_to_app(row) for row in result.rows]

    async def list_pinned_apps(self, actor_id, limit=3):
        await self.ensure_tables()
        result = await self.db.execute(
            f"""
            SELECT {APP_COLUMNS}
            FROM apps
            JOIN app_user_state ON app_user_state.app_id = apps.id
            WHERE app_user_state.actor_id = :actor_id
              AND app_user_state.pinned_at IS NOT NULL
              AND apps.deleted_at IS NULL
            ORDER BY
              COALESCE(app_user_state.last_accessed_at, app_user_state.pinned_at) DESC,
              apps.id DESC
            LIMIT :limit
            """,
            {"actor_id": actor_id, "limit": limit},
        )
        return [_row_to_app(row) for row in result.rows]

    async def record_access(self, actor_id, app_id):
        await self.ensure_tables()
        now = _now()
        await self.db.execute_write(
            """
            INSERT INTO app_user_state (
                actor_id, app_id, last_accessed_at, access_count
            )
            VALUES (:actor_id, :app_id, :now, 1)
            ON CONFLICT(actor_id, app_id) DO UPDATE SET
                last_accessed_at = excluded.last_accessed_at,
                access_count = app_user_state.access_count + 1
            """,
            {"actor_id": actor_id, "app_id": app_id, "now": now},
        )

    async def set_pinned(self, actor_id, app_id, pinned):
        await self.ensure_tables()
        now = _now()
        await self.db.execute_write(
            """
            INSERT INTO app_user_state (actor_id, app_id, pinned_at)
            VALUES (:actor_id, :app_id, :pinned_at)
            ON CONFLICT(actor_id, app_id) DO UPDATE SET
                pinned_at = excluded.pinned_at
            """,
            {
                "actor_id": actor_id,
                "app_id": app_id,
                "pinned_at": now if pinned else None,
            },
        )

    async def get_user_state(self, actor_id, app_id):
        await self.ensure_tables()
        result = await self.db.execute(
            f"""
            SELECT {APP_USER_STATE_COLUMNS}
            FROM app_user_state
            WHERE actor_id = :actor_id AND app_id = :app_id
            """,
            {"actor_id": actor_id, "app_id": app_id},
        )
        return _row_to_state(result.first())

    async def get_csp_origins(self, app_id, directive="connect-src"):
        await self.ensure_tables()
        result = await self.db.execute(
            """
            SELECT origin
            FROM app_csp_origins
            WHERE app_id = :app_id AND directive = :directive
            ORDER BY origin
            """,
            {"app_id": app_id, "directive": directive},
        )
        return [row["origin"] for row in result.rows]

    async def set_csp_origins(
        self, app_id, origins, directive="connect-src", actor_id=None
    ):
        await self.ensure_tables()
        normalized = [normalize_connect_origin(origin) for origin in origins]
        now = _now()

        def save(conn):
            app_row = conn.execute(
                "SELECT deleted_at FROM apps WHERE id = ?", (app_id,)
            ).fetchone()
            if app_row is None or app_row["deleted_at"] is not None:
                raise KeyError(app_id)
            current = [
                row["origin"]
                for row in conn.execute(
                    """
                    SELECT origin
                    FROM app_csp_origins
                    WHERE app_id = ? AND directive = ?
                    ORDER BY origin
                    """,
                    (app_id, directive),
                ).fetchall()
            ]
            if current == normalized:
                return
            conn.execute(
                """
                DELETE FROM app_csp_origins
                WHERE app_id = ? AND directive = ?
                """,
                (app_id, directive),
            )
            conn.executemany(
                """
                INSERT INTO app_csp_origins (
                    app_id, directive, origin, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [(app_id, directive, origin, now, now) for origin in normalized],
            )
            _insert_revision(
                conn,
                app_id,
                {"csp_origins": normalized},
                now,
                actor_id=actor_id,
            )

        await self.db.execute_write_fn(save)

    async def get_sql_databases(self, app_id):
        await self.ensure_tables()
        result = await self.db.execute(
            """
            SELECT database_name
            FROM app_sql_databases
            WHERE app_id = :app_id
            ORDER BY database_name
            """,
            {"app_id": app_id},
        )
        return [row["database_name"] for row in result.rows]

    async def get_stored_queries(self, app_id):
        await self.ensure_tables()
        result = await self.db.execute(
            """
            SELECT stored_queries
            FROM apps
            WHERE id = :app_id
            """,
            {"app_id": app_id},
        )
        row = result.first()
        return _decode_json(row["stored_queries"], []) if row else []

    async def set_stored_queries(self, app_id, stored_queries, actor_id=None):
        await self.ensure_tables()
        now = _now()
        stored_queries = sorted(dict.fromkeys(stored_queries))

        def save(conn):
            row = conn.execute(
                "SELECT stored_queries, deleted_at FROM apps WHERE id = ?", (app_id,)
            ).fetchone()
            if row is None or row["deleted_at"] is not None:
                raise KeyError(app_id)
            current = _decode_json(row["stored_queries"], [])
            if current == stored_queries:
                return
            conn.execute(
                """
                UPDATE apps
                SET stored_queries = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(stored_queries), now, app_id),
            )
            _insert_revision(
                conn,
                app_id,
                {"stored_queries": stored_queries},
                now,
                actor_id=actor_id,
            )

        await self.db.execute_write_fn(save)

    async def set_sql_databases(self, app_id, database_names, actor_id=None):
        await self.ensure_tables()
        now = _now()
        database_names = sorted(dict.fromkeys(database_names))

        def save(conn):
            app_row = conn.execute(
                "SELECT deleted_at FROM apps WHERE id = ?", (app_id,)
            ).fetchone()
            if app_row is None or app_row["deleted_at"] is not None:
                raise KeyError(app_id)
            current = [
                row["database_name"]
                for row in conn.execute(
                    """
                    SELECT database_name
                    FROM app_sql_databases
                    WHERE app_id = ?
                    ORDER BY database_name
                    """,
                    (app_id,),
                ).fetchall()
            ]
            if current == database_names:
                return
            conn.execute("DELETE FROM app_sql_databases WHERE app_id = ?", (app_id,))
            conn.executemany(
                """
                INSERT INTO app_sql_databases (
                    app_id, database_name, created_at, updated_at
                )
                VALUES (?, ?, ?, ?)
                """,
                [(app_id, database_name, now, now) for database_name in database_names],
            )
            _insert_revision(
                conn,
                app_id,
                {"sql_databases": database_names},
                now,
                actor_id=actor_id,
            )

        await self.db.execute_write_fn(save)

    async def get_access_mode(self, app_id):
        await self.ensure_tables()
        result = await self.db.execute(
            """
            SELECT is_private
            FROM apps
            WHERE id = :app_id
            """,
            {"app_id": app_id},
        )
        row = result.first()
        return "private" if row is None or row["is_private"] else "not-private"

    async def set_access_mode(self, app_id, mode, actor_id=None):
        if mode not in {"private", "not-private"}:
            raise ValueError("Unknown app access mode")
        await self.ensure_tables()
        now = _now()

        def save(conn):
            is_private = 1 if mode == "private" else 0
            row = conn.execute(
                "SELECT is_private, deleted_at FROM apps WHERE id = ?", (app_id,)
            ).fetchone()
            if row is None or row["deleted_at"] is not None:
                raise KeyError(app_id)
            if row["is_private"] == is_private:
                return
            conn.execute(
                """
                UPDATE apps
                SET is_private = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (is_private, now, app_id),
            )
            _insert_revision(
                conn,
                app_id,
                {"is_private": is_private},
                now,
                actor_id=actor_id,
            )

        await self.db.execute_write_fn(save)
