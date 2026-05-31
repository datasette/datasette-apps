from __future__ import annotations

from urllib.parse import urlencode

from datasette.utils import tilde_encode

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


async def run_app_stored_query(
    datasette, app, actor, database_name, query_name, params=None
):
    allowed_queries = await Registry(datasette).get_stored_queries(app["id"])
    if f"{database_name}/{query_name}" not in allowed_queries:
        raise AppQueryError("This app is not allowed to run that stored query")

    if params is not None and not isinstance(params, dict):
        raise AppQueryError("Query parameters must be an object of named values")

    stored_query = await datasette.get_query(database_name, query_name)
    if stored_query is None:
        raise AppQueryError("Stored query not found")

    try:
        database_path = datasette.urls.database(database_name)
    except KeyError as e:
        raise AppQueryError("Database not found") from e

    query_path = f"{database_path}/{tilde_encode(query_name)}"
    if stored_query.is_write:
        response = await datasette.client.post(
            query_path + "?_json=1",
            actor=actor,
            json={
                key: "" if value is None else value
                for key, value in (params or {}).items()
            },
        )
        return _json_response_or_error(response)

    query_args = {
        "_shape": "objects",
        "_extra": "columns",
    }
    for key, value in (params or {}).items():
        query_args[key] = "" if value is None else value
    response = await datasette.client.get(
        f"{query_path}.json?" + urlencode(query_args, doseq=True),
        actor=actor,
    )
    data = _json_response_or_error(response)
    return {
        "columns": data.get("columns") or [],
        "rows": data.get("rows") or [],
    }


def _json_response_or_error(response):
    try:
        data = response.json()
    except ValueError as e:
        if response.status_code in (401, 403):
            raise AppQueryError("Permission denied by Datasette") from e
        raise AppQueryError("Stored query failed") from e
    if response.status_code != 200 or data.get("ok") is False:
        raise AppQueryError(
            data.get("error") or data.get("message") or "Stored query failed"
        )
    return data
