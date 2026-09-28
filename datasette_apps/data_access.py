from __future__ import annotations

from urllib.parse import urlencode

from datasette.utils import tilde_encode

from . import telemetry
from .registry import Registry


class AppQueryError(Exception):
    """A query the app asked for that was refused or failed.

    ``outcome`` classifies it for telemetry (``apps.query.outcome``): one of
    ``invalid``, ``not_allowed``, ``not_found``, ``denied`` or ``failed``.
    The message is shown to the app but never recorded in telemetry - for
    ``failed`` it is Datasette's SQL error text, which can quote SQL.
    """

    def __init__(self, message, *, outcome="failed"):
        super().__init__(message)
        self.outcome = outcome


async def run_app_query(
    datasette, app, actor, database_name, sql, params=None, recorder=None
):
    """Run ``sql`` against ``database_name`` as ``actor``.

    ``recorder`` is an optional ``telemetry.QueryRecorder``; ``database`` is
    set on it only once the database has passed the app's allowlist.
    """
    allowed_databases = await Registry(datasette).get_sql_databases(app["id"])
    if database_name not in allowed_databases:
        raise AppQueryError(
            "This app is not allowed to query that database", outcome="not_allowed"
        )
    if recorder is not None:
        recorder.database = database_name

    if params is not None and not isinstance(params, dict):
        raise AppQueryError(
            "Query parameters must be an object of named values", outcome="invalid"
        )

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
        raise AppQueryError("Database not found", outcome="not_found") from e
    path = f"{database_path}/-/query.json?" + urlencode(query_args, doseq=True)
    response = await datasette.client.get(
        path, actor=actor, headers=telemetry.trace_headers()
    )
    try:
        data = response.json()
    except ValueError as e:
        if response.status_code in (401, 403):
            raise AppQueryError(
                "Permission denied by Datasette", outcome="denied"
            ) from e
        raise AppQueryError("Query failed", outcome="failed") from e
    if response.status_code in (401, 403):
        raise AppQueryError("Permission denied by Datasette", outcome="denied")
    if response.status_code != 200 or not data.get("ok"):
        raise AppQueryError(data.get("error") or "Query failed", outcome="failed")
    return {
        "columns": data.get("columns") or [],
        "rows": data.get("rows") or [],
    }


async def run_app_stored_query(
    datasette, app, actor, database_name, query_name, params=None, recorder=None
):
    """Run the stored query ``database_name/query_name`` as ``actor``.

    ``recorder`` is an optional ``telemetry.QueryRecorder``: ``database`` is
    set on it once the stored query has passed the app's allowlist, and
    ``write`` once the stored query has been resolved.
    """
    allowed_queries = await Registry(datasette).get_stored_queries(app["id"])
    if f"{database_name}/{query_name}" not in allowed_queries:
        raise AppQueryError(
            "This app is not allowed to run that stored query", outcome="not_allowed"
        )
    if recorder is not None:
        recorder.database = database_name

    if params is not None and not isinstance(params, dict):
        raise AppQueryError(
            "Query parameters must be an object of named values", outcome="invalid"
        )

    stored_query = await datasette.get_query(database_name, query_name)
    if stored_query is None:
        raise AppQueryError("Stored query not found", outcome="not_found")
    if recorder is not None:
        recorder.write = bool(stored_query.is_write)

    try:
        database_path = datasette.urls.database(database_name)
    except KeyError as e:
        raise AppQueryError("Database not found", outcome="not_found") from e

    query_path = f"{database_path}/{tilde_encode(query_name)}"
    if stored_query.is_write:
        response = await datasette.client.post(
            query_path + "?_json=1",
            actor=actor,
            headers=telemetry.trace_headers(),
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
        headers=telemetry.trace_headers(),
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
            raise AppQueryError(
                "Permission denied by Datasette", outcome="denied"
            ) from e
        raise AppQueryError("Stored query failed", outcome="failed") from e
    if response.status_code in (401, 403):
        raise AppQueryError("Permission denied by Datasette", outcome="denied")
    if response.status_code != 200 or data.get("ok") is False:
        raise AppQueryError(
            data.get("error") or data.get("message") or "Stored query failed",
            outcome="failed",
        )
    return data
