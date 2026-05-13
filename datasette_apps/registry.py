from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .db import ensure_tables
from .ids import monotonic_ulid
from .csp import normalize_connect_origin
from .capabilities import validate_capability_name


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
    return app


def _row_to_state(row):
    if row is None:
        return None
    return dict(row)


def _row_to_version(row):
    if row is None:
        return None
    return dict(row)


def _fts_query(q):
    tokens = re.findall(r"[\w]+", q or "")
    if not tokens:
        return None
    return " ".join(token + "*" for token in tokens)


def _validate_external_id(id):
    if not id or "/" in id or "?" in id or "#" in id or any(c.isspace() for c in id):
        raise ValueError("External app IDs must be non-empty safe path segments")


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
                actor_id, current_version, created_at, updated_at
            )
            VALUES (
                :id, 1, :name, :description, :path, :source, :metadata,
                NULL, NULL, :now, :now
            )
            ON CONFLICT(id) DO UPDATE SET
                external = 1,
                name = excluded.name,
                description = excluded.description,
                path = excluded.path,
                source = excluded.source,
                metadata = excluded.metadata,
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

    async def create_stored_app(self, actor_id, name, description, html):
        await self.ensure_tables()
        app_id = monotonic_ulid()
        now = _now()

        def create(conn):
            conn.execute(
                """
                INSERT INTO apps (
                    id, external, name, description, path, source, metadata,
                    actor_id, current_version, created_at, updated_at
                )
                VALUES (?, 0, ?, ?, ?, 'datasette-apps', '{}', ?, 1, ?, ?)
                """,
                (
                    app_id,
                    name,
                    description or "",
                    f"/-/apps/{app_id}",
                    actor_id,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO app_versions (app_id, version, html, created_at)
                VALUES (?, 1, ?, ?)
                """,
                (app_id, html, now),
            )

        await self.db.execute_write_fn(create)
        return await self.get_app(app_id)

    async def save_new_version(self, app_id, html):
        await self.ensure_tables()
        now = _now()

        def save(conn):
            row = conn.execute(
                "SELECT current_version FROM apps WHERE id = ?", (app_id,)
            ).fetchone()
            if row is None:
                raise KeyError(app_id)
            next_version = int(row["current_version"] or 0) + 1
            conn.execute(
                """
                INSERT INTO app_versions (app_id, version, html, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (app_id, next_version, html, now),
            )
            conn.execute(
                """
                UPDATE apps
                SET current_version = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_version, now, app_id),
            )

        await self.db.execute_write_fn(save)

    async def update_stored_app(self, app_id, name, description, html):
        await self.ensure_tables()
        now = _now()

        def save(conn):
            row = conn.execute(
                "SELECT current_version, external FROM apps WHERE id = ?", (app_id,)
            ).fetchone()
            if row is None:
                raise KeyError(app_id)
            if row["external"]:
                raise ValueError("External apps cannot be edited by datasette-apps")
            next_version = int(row["current_version"] or 0) + 1
            conn.execute(
                """
                INSERT INTO app_versions (app_id, version, html, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (app_id, next_version, html, now),
            )
            conn.execute(
                """
                UPDATE apps
                SET name = ?, description = ?, current_version = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, description or "", next_version, now, app_id),
            )

        await self.db.execute_write_fn(save)

    async def get_current_version(self, app_id):
        await self.ensure_tables()
        result = await self.db.execute(
            """
            SELECT app_versions.*
            FROM app_versions
            JOIN apps ON apps.id = app_versions.app_id
            WHERE apps.id = :app_id
              AND app_versions.version = apps.current_version
            """,
            {"app_id": app_id},
        )
        return _row_to_version(result.first())

    async def remove_app(self, id):
        await self.ensure_tables()
        await self.db.execute_write("DELETE FROM apps WHERE id = :id", {"id": id})

    async def remove_apps_for_source(self, source):
        await self.ensure_tables()
        await self.db.execute_write(
            "DELETE FROM apps WHERE external = 1 AND source = :source",
            {"source": source},
        )

    async def get_app(self, id):
        await self.ensure_tables()
        result = await self.db.execute("SELECT * FROM apps WHERE id = :id", {"id": id})
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
                SELECT apps.* {select_user_state}
                FROM apps
                JOIN apps_fts ON apps.rowid = apps_fts.rowid
                {join_user_state}
                WHERE apps_fts MATCH :q
                ORDER BY {order_by}
                LIMIT :limit OFFSET :offset
            """.format(
                select_user_state=select_user_state,
                join_user_state=join_user_state,
                order_by=order_by,
            )
            params["q"] = fts
        else:
            sql = """
                SELECT apps.* {select_user_state}
                FROM apps
                {join_user_state}
                ORDER BY {order_by}
                LIMIT :limit OFFSET :offset
            """.format(
                select_user_state=select_user_state,
                join_user_state=join_user_state,
                order_by=order_by,
            )
        result = await self.db.execute(sql, params)
        return [_row_to_app(row) for row in result.rows]

    async def list_pinned_apps(self, actor_id, limit=3):
        await self.ensure_tables()
        result = await self.db.execute(
            """
            SELECT apps.*
            FROM apps
            JOIN app_user_state ON app_user_state.app_id = apps.id
            WHERE app_user_state.actor_id = :actor_id
              AND app_user_state.pinned_at IS NOT NULL
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
            """
            SELECT *
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

    async def set_csp_origins(self, app_id, origins, directive="connect-src"):
        await self.ensure_tables()
        normalized = [normalize_connect_origin(origin) for origin in origins]
        now = _now()

        def save(conn):
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

    async def set_sql_databases(self, app_id, database_names):
        await self.ensure_tables()
        now = _now()
        database_names = sorted(dict.fromkeys(database_names))

        def save(conn):
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

        await self.db.execute_write_fn(save)

    async def get_capability_grant(self, app_id, capability):
        await self.ensure_tables()
        result = await self.db.execute(
            """
            SELECT *
            FROM app_capability_grants
            WHERE app_id = :app_id AND capability = :capability
            """,
            {"app_id": app_id, "capability": capability},
        )
        row = result.first()
        if row is None:
            return None
        grant = dict(row)
        grant["config"] = _decode_json(grant["config"], {})
        return grant

    async def get_capability_grants(self, app_id):
        await self.ensure_tables()
        result = await self.db.execute(
            """
            SELECT *
            FROM app_capability_grants
            WHERE app_id = :app_id
            ORDER BY capability
            """,
            {"app_id": app_id},
        )
        grants = {}
        for row in result.rows:
            grants[row["capability"]] = _decode_json(row["config"], {})
        return grants

    async def set_capability_grant(self, app_id, capability, config=None):
        validate_capability_name(capability)
        await self.ensure_tables()
        now = _now()
        await self.db.execute_write(
            """
            INSERT INTO app_capability_grants (
                app_id, capability, config, created_at, updated_at
            )
            VALUES (:app_id, :capability, :config, :now, :now)
            ON CONFLICT(app_id, capability) DO UPDATE SET
                config = excluded.config,
                updated_at = excluded.updated_at
            """,
            {
                "app_id": app_id,
                "capability": capability,
                "config": json.dumps(config or {}, sort_keys=True),
                "now": now,
            },
        )

    async def set_capability_grants(self, app_id, grants):
        for capability in grants:
            validate_capability_name(capability)
        await self.ensure_tables()
        now = _now()

        def save(conn):
            conn.execute(
                "DELETE FROM app_capability_grants WHERE app_id = ?", (app_id,)
            )
            conn.executemany(
                """
                INSERT INTO app_capability_grants (
                    app_id, capability, config, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (app_id, capability, json.dumps(config or {}, sort_keys=True), now, now)
                    for capability, config in grants.items()
                ],
            )

        await self.db.execute_write_fn(save)

    async def get_access_mode(self, app_id):
        await self.ensure_tables()
        result = await self.db.execute(
            """
            SELECT 1
            FROM app_access
            WHERE app_id = :app_id
              AND action = 'view-app'
              AND subject_type = 'authenticated'
              AND allow = 1
            """,
            {"app_id": app_id},
        )
        if result.first():
            return "signed-in"
        actor_result = await self.db.execute(
            """
            SELECT 1
            FROM app_access
            WHERE app_id = :app_id
              AND action = 'view-app'
              AND subject_type = 'actor'
              AND allow = 1
            """,
            {"app_id": app_id},
        )
        return "specific" if actor_result.first() else "private"

    async def get_access_actor_ids(self, app_id):
        await self.ensure_tables()
        result = await self.db.execute(
            """
            SELECT subject_id
            FROM app_access
            WHERE app_id = :app_id
              AND action = 'view-app'
              AND subject_type = 'actor'
              AND allow = 1
            ORDER BY subject_id
            """,
            {"app_id": app_id},
        )
        return [row["subject_id"] for row in result.rows]

    async def set_access_mode(self, app_id, mode, actor_ids=None):
        if mode not in {"private", "signed-in", "specific"}:
            raise ValueError("Unknown app access mode")
        await self.ensure_tables()
        now = _now()
        actor_ids = [actor_id for actor_id in actor_ids or [] if actor_id]

        def save(conn):
            conn.execute(
                """
                DELETE FROM app_access
                WHERE app_id = ? AND action = 'view-app'
                """,
                (app_id,),
            )
            if mode == "signed-in":
                conn.execute(
                    """
                    INSERT INTO app_access (
                        app_id, action, subject_type, subject_id,
                        allow, created_at, updated_at
                    )
                    VALUES (?, 'view-app', 'authenticated', NULL, 1, ?, ?)
                    """,
                    (app_id, now, now),
                )
            if mode == "specific":
                conn.executemany(
                    """
                    INSERT INTO app_access (
                        app_id, action, subject_type, subject_id,
                        allow, created_at, updated_at
                    )
                    VALUES (?, 'view-app', 'actor', ?, 1, ?, ?)
                    """,
                    [(app_id, actor_id, now, now) for actor_id in actor_ids],
                )

        await self.db.execute_write_fn(save)
