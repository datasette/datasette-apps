from __future__ import annotations

from urllib.parse import urlencode

from .registry import Registry


class AppQueryError(Exception):
    pass


async def run_app_query(datasette, app, actor, database_name, sql, params=None):
    allowed_databases = await Registry(datasette).get_sql_databases(app["id"])
    if database_name not in allowed_databases:
        raise AppQueryError("This app is not allowed to query that database")

    if params is not None and not isinstance(params, dict):
        raise AppQueryError("Query parameters must be an object of named values")

    query_args = {
        "sql": sql,
        "_shape": "objects",
        "_extra": "columns",
    }
    for key, value in (params or {}).items():
        query_args[key] = "" if value is None else value

    try:
        database_path = datasette.urls.database(database_name)
    except KeyError as e:
        raise AppQueryError("Database not found") from e
    path = f"{database_path}/-/query.json?" + urlencode(query_args, doseq=True)
    response = await datasette.client.get(path, actor=actor)
    try:
        data = response.json()
    except ValueError as e:
        if response.status_code in (401, 403):
            raise AppQueryError("Permission denied by Datasette") from e
        raise AppQueryError("Query failed") from e
    if response.status_code != 200 or not data.get("ok"):
        raise AppQueryError(data.get("error") or "Query failed")
    return {
        "columns": data.get("columns") or [],
        "rows": data.get("rows") or [],
    }
