"""Malformed request bodies to the app query endpoints must get the usual
{"ok": false, "error": "Invalid request: ..."} response, never a 500.
https://github.com/datasette/datasette-apps/issues/40"""

import json

import pytest
from datasette.app import Datasette

from datasette_apps import Registry
from datasette_apps.debug import create_debug_job

MALFORMED_BODIES = [
    pytest.param(b"[]", id="list"),
    pytest.param(b'"x"', id="string"),
    pytest.param(b"5", id="int"),
    pytest.param(b"null", id="null"),
    pytest.param(b"\xff\xfe", id="invalid-utf8"),
    pytest.param(b"{not json", id="invalid-json"),
    pytest.param(json.dumps({"sql": "select 1"}).encode(), id="missing-database"),
    pytest.param(
        json.dumps({"database": ["_memory"], "sql": "select 1"}).encode(),
        id="database-not-string",
    ),
    pytest.param(
        json.dumps({"database": "_memory", "sql": 5}).encode(),
        id="sql-not-string",
    ),
    pytest.param(
        json.dumps({"database": "_memory", "query": {"x": 1}}).encode(),
        id="query-not-string",
    ),
]


async def _setup():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Query app",
        description="",
        html="",
        sql_databases=["_memory"],
    )
    return datasette, app


async def _app_query_path(datasette, app):
    return f"/-/apps/{app['id']}/query"


async def _debug_query_path(datasette, app):
    job = await create_debug_job(
        datasette, app_id=app["id"], actor_id="alice", javascript="return 1;"
    )
    return f"/-/apps/debug/{job['id']}/query"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", MALFORMED_BODIES)
@pytest.mark.parametrize(
    "path_for", [_app_query_path, _debug_query_path], ids=["app", "debug"]
)
async def test_malformed_body_returns_invalid_request(body, path_for):
    datasette, app = await _setup()
    path = await path_for(datasette, app)

    response = await datasette.client.post(path, actor={"id": "alice"}, content=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"].startswith("Invalid request: ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path_for", [_app_query_path, _debug_query_path], ids=["app", "debug"]
)
async def test_empty_body_still_reports_missing_fields(path_for):
    datasette, app = await _setup()
    path = await path_for(datasette, app)

    response = await datasette.client.post(path, actor={"id": "alice"}, content=b"")

    assert response.json() == {"ok": False, "error": "Invalid request: 'database'"}
