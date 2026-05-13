from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .db import ensure_tables


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

    async def list_apps(self, q=None, limit=20):
        await self.ensure_tables()
        fts = _fts_query(q)
        if fts:
            sql = """
                SELECT apps.*
                FROM apps
                JOIN apps_fts ON apps.rowid = apps_fts.rowid
                WHERE apps_fts MATCH :q
                ORDER BY apps.updated_at DESC, apps.id
                LIMIT :limit
            """
            params = {"q": fts, "limit": limit}
        else:
            sql = """
                SELECT *
                FROM apps
                ORDER BY updated_at DESC, id
                LIMIT :limit
            """
            params = {"limit": limit}
        result = await self.db.execute(sql, params)
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
