from __future__ import annotations

import sqlite3

from datasette.resources import TableResource

from .registry import Registry


class AppQueryError(Exception):
    pass


WRITE_ACTIONS = {
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
}


def _columns_allowed(allowed_columns, column):
    return allowed_columns is None or column in allowed_columns


async def _actor_allowed_grants(datasette, actor, app_id, database_name):
    grants = [
        grant
        for grant in await Registry(datasette).get_data_permissions(app_id)
        if grant["permission_type"] == "table-read"
        and grant["database_name"] == database_name
    ]
    allowed = {}
    actor_denied = False
    for grant in grants:
        resource = TableResource(database_name, grant["resource_name"])
        if await datasette.allowed(
            action="view-table", resource=resource, actor=actor
        ):
            allowed[(grant["resource_type"], grant["resource_name"])] = (
                set(grant["columns"]) if grant["columns"] else None
            )
        else:
            actor_denied = True
    if grants and not allowed and actor_denied:
        raise AppQueryError(
            "The actor is not allowed to read any of this app's granted resources"
        )
    return allowed


async def run_app_query(datasette, app, actor, database_name, sql, params=None):
    allowed = await _actor_allowed_grants(datasette, actor, app["id"], database_name)
    if not allowed:
        raise AppQueryError("This app is not allowed to read from that database")

    db = datasette.get_database(database_name)

    def execute(conn):
        def authorizer(action, arg1, arg2, dbname, source):
            if action in WRITE_ACTIONS or action == sqlite3.SQLITE_PRAGMA:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                table = arg1
                column = arg2
                table_columns = allowed.get(("table", table))
                if table_columns is not None or ("table", table) in allowed:
                    if _columns_allowed(table_columns, column):
                        return sqlite3.SQLITE_OK
                if source:
                    view_columns = allowed.get(("view", source))
                    if view_columns is not None or ("view", source) in allowed:
                        if _columns_allowed(view_columns, column):
                            return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(authorizer)
        try:
            cursor = conn.execute(sql, params or {})
            rows = cursor.fetchall()
            columns = [description[0] for description in cursor.description or []]
            return {
                "columns": columns,
                "rows": [dict(zip(columns, row)) for row in rows],
            }
        finally:
            conn.set_authorizer(None)

    try:
        return await db.execute_fn(execute)
    except sqlite3.DatabaseError as e:
        raise AppQueryError(f"Query not allowed: {e}") from e
