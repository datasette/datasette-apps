"""Telemetry for app data queries: the apps.query span, the apps.queries and
apps.query.duration metrics, and apps.app_id on app routes' request spans."""

import json
import sqlite3

import pytest

pytest.importorskip("opentelemetry.sdk")

from datasette.app import Datasette
from datasette.telemetry_testing import assert_metrics_conform, assert_spans_conform
from opentelemetry.trace import SpanKind, StatusCode

from datasette_apps import Registry, telemetry_registry
from datasette_apps.debug import create_debug_job

ACTOR = {"id": "alice"}


def create_database(tmp_path):
    db_path = tmp_path / "content.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        create table news (id integer primary key, title text);
        insert into news (title) values ('First'), ('Second');
        """)
    conn.close()
    return db_path


async def _make_app(datasette, *, sql_databases=("content",), stored_queries=()):
    return await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="News app",
        description="",
        html="<h1>News</h1>",
        sql_databases=list(sql_databases),
        stored_queries=list(stored_queries),
    )


async def _datasette(tmp_path, **kwargs):
    datasette = Datasette([str(create_database(tmp_path))], **kwargs)
    await datasette.invoke_startup()
    return datasette


async def _post_query(datasette, app, body):
    return await datasette.client.post(
        f"/-/apps/{app['id']}/query",
        actor=ACTOR,
        content=body if isinstance(body, str) else json.dumps(body),
    )


def _query_span(otel_spans):
    spans = [
        span
        for span in otel_spans.get_finished_spans()
        if span.name == telemetry_registry.QUERY
    ]
    assert len(spans) == 1, spans
    return spans[0]


def _children(otel_spans, parent):
    return [
        span
        for span in otel_spans.get_finished_spans()
        if span.parent is not None and span.parent.span_id == parent.context.span_id
    ]


def _assert_conforms(otel_spans, otel_metrics):
    assert_spans_conform(
        telemetry_registry.SPANS,
        otel_spans.get_finished_spans(),
        scope_name="datasette_apps",
    )
    assert_metrics_conform(
        telemetry_registry.METRICS, otel_metrics, scope_name="datasette_apps"
    )


def _assert_one_query_metric(otel_metrics, **attributes):
    otel_metrics.collect()
    counted = otel_metrics.point("apps.queries", attributes)
    assert counted.value == 1
    assert dict(counted.attributes) == attributes
    duration = otel_metrics.point("apps.query.duration")
    assert duration.count == 1
    for key in ("apps.app_id", "db.namespace", "apps.debug.job_id"):
        assert key not in duration.attributes
    return counted


@pytest.mark.asyncio
async def test_ok_sql_query(tmp_path, otel_spans, otel_metrics):
    datasette = await _datasette(tmp_path)
    app = await _make_app(datasette)

    response = await _post_query(
        datasette, app, {"database": "content", "sql": "select title from news"}
    )
    assert response.json()["ok"] is True

    span = _query_span(otel_spans)
    assert dict(span.attributes) == {
        "apps.app_id": app["id"],
        "apps.query.source": "app",
        "apps.query.outcome": "ok",
        "apps.query.kind": "sql",
        "db.namespace": "content",
        "apps.query.rows": 2,
    }
    assert span.status.status_code == StatusCode.UNSET
    _assert_one_query_metric(
        otel_metrics,
        **{
            "apps.query.source": "app",
            "apps.query.outcome": "ok",
            "apps.query.kind": "sql",
        },
    )
    _assert_conforms(otel_spans, otel_metrics)


@pytest.mark.asyncio
async def test_internal_client_span_nests_under_apps_query(tmp_path, otel_spans):
    datasette = await _datasette(tmp_path)
    app = await _make_app(datasette)

    await _post_query(
        datasette, app, {"database": "content", "sql": "select title from news"}
    )

    span = _query_span(otel_spans)
    nested = [
        child
        for child in _children(otel_spans, span)
        if child.kind == SpanKind.SERVER
        and child.attributes.get("datasette.internal_client") is True
    ]
    assert len(nested) == 1
    assert nested[0].context.trace_id == span.context.trace_id
    assert nested[0].attributes["url.path"].endswith("/content/-/query.json")


@pytest.mark.asyncio
async def test_not_allowed_database(tmp_path, otel_spans, otel_metrics):
    datasette = await _datasette(tmp_path, memory=True)
    app = await _make_app(datasette)

    response = await _post_query(
        datasette, app, {"database": "_memory", "sql": "select 1"}
    )
    assert "not allowed" in response.json()["error"]

    span = _query_span(otel_spans)
    assert span.attributes["apps.query.outcome"] == "not_allowed"
    assert span.attributes["apps.query.kind"] == "sql"
    # The requested database name is unvetted input until it passes the
    # allowlist, so it is never recorded.
    assert "db.namespace" not in span.attributes
    assert "apps.query.rows" not in span.attributes
    assert span.status.status_code == StatusCode.UNSET
    assert not [
        child for child in _children(otel_spans, span) if child.kind == SpanKind.SERVER
    ]
    _assert_one_query_metric(
        otel_metrics,
        **{
            "apps.query.source": "app",
            "apps.query.outcome": "not_allowed",
            "apps.query.kind": "sql",
        },
    )
    _assert_conforms(otel_spans, otel_metrics)


@pytest.mark.asyncio
async def test_denied_by_actor_permissions(tmp_path, otel_spans, otel_metrics):
    datasette = await _datasette(tmp_path, default_deny=True)
    app = await _make_app(datasette)

    response = await _post_query(
        datasette, app, {"database": "content", "sql": "select title from news"}
    )
    assert "Permission denied" in response.json()["error"]

    span = _query_span(otel_spans)
    assert span.attributes["apps.query.outcome"] == "denied"
    assert span.attributes["db.namespace"] == "content"
    assert span.status.status_code == StatusCode.UNSET
    _assert_one_query_metric(
        otel_metrics,
        **{
            "apps.query.source": "app",
            "apps.query.outcome": "denied",
            "apps.query.kind": "sql",
        },
    )


@pytest.mark.asyncio
async def test_failed_sql_records_no_error_text(tmp_path, otel_spans, otel_metrics):
    datasette = await _datasette(tmp_path)
    app = await _make_app(datasette)

    response = await _post_query(
        datasette, app, {"database": "content", "sql": "select * from nope"}
    )
    error = response.json()["error"]
    assert "nope" in error

    span = _query_span(otel_spans)
    assert span.attributes["apps.query.outcome"] == "failed"
    assert span.status.status_code == StatusCode.UNSET
    assert "error.type" not in span.attributes
    assert not span.events
    for value in span.attributes.values():
        assert "nope" not in str(value)
    _assert_one_query_metric(
        otel_metrics,
        **{
            "apps.query.source": "app",
            "apps.query.outcome": "failed",
            "apps.query.kind": "sql",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,kind",
    [
        ({"database": "content"}, "sql"),  # missing "sql": KeyError
        ({"database": "content", "sql": "select 1", "params": [1]}, "sql"),
        ("not json", None),
    ],
)
async def test_invalid_requests(tmp_path, otel_spans, otel_metrics, body, kind):
    datasette = await _datasette(tmp_path)
    app = await _make_app(datasette)

    response = await _post_query(datasette, app, body)
    assert response.status_code == 200
    assert response.json()["ok"] is False

    span = _query_span(otel_spans)
    assert span.attributes["apps.query.outcome"] == "invalid"
    assert span.attributes.get("apps.query.kind") == kind
    assert span.status.status_code == StatusCode.UNSET
    expected = {"apps.query.source": "app", "apps.query.outcome": "invalid"}
    if kind:
        expected["apps.query.kind"] = kind
    _assert_one_query_metric(otel_metrics, **expected)
    _assert_conforms(otel_spans, otel_metrics)


@pytest.mark.asyncio
async def test_non_object_body_is_recorded_as_error(tmp_path, otel_spans, otel_metrics):
    # Pre-existing bug, deliberately left alone here: a JSON body that is not
    # an object raises TypeError, which is not caught and becomes a 500.
    # Telemetry records it as an unexpected error, not as "invalid".
    datasette = await _datasette(tmp_path)
    app = await _make_app(datasette)

    response = await _post_query(datasette, app, "[]")
    assert response.status_code == 500

    span = _query_span(otel_spans)
    assert span.attributes["apps.query.outcome"] == "error"
    assert span.attributes["error.type"] == "TypeError"
    assert span.status.status_code == StatusCode.ERROR
    assert not span.events  # the exception itself is never recorded
    otel_metrics.collect()
    assert (
        otel_metrics.point(
            "apps.queries", {"apps.query.source": "app", "apps.query.outcome": "error"}
        ).value
        == 1
    )
    _assert_conforms(otel_spans, otel_metrics)


@pytest.mark.asyncio
async def test_stored_query_not_found(tmp_path, otel_spans, otel_metrics):
    datasette = await _datasette(tmp_path)
    # Allow-listed, but never created
    app = await _make_app(datasette, stored_queries=["content/missing"])

    response = await _post_query(
        datasette, app, {"database": "content", "query": "missing"}
    )
    assert response.json() == {"ok": False, "error": "Stored query not found"}

    span = _query_span(otel_spans)
    assert span.attributes["apps.query.outcome"] == "not_found"
    assert span.attributes["apps.query.kind"] == "stored"
    assert "apps.query.write" not in span.attributes
    _assert_one_query_metric(
        otel_metrics,
        **{
            "apps.query.source": "app",
            "apps.query.outcome": "not_found",
            "apps.query.kind": "stored",
        },
    )
    for value in span.attributes.values():
        assert "missing" not in str(value)


@pytest.mark.asyncio
async def test_stored_query_not_allowed(tmp_path, otel_spans):
    datasette = await _datasette(tmp_path)
    app = await _make_app(datasette)

    await _post_query(datasette, app, {"database": "content", "query": "anything"})

    span = _query_span(otel_spans)
    assert span.attributes["apps.query.outcome"] == "not_allowed"
    assert "db.namespace" not in span.attributes


@pytest.mark.asyncio
async def test_stored_read_query(tmp_path, otel_spans, otel_metrics):
    datasette = await _datasette(tmp_path)
    await datasette.add_query(
        "content",
        "news_by_id",
        "select title from news where id = :id",
        source="user",
        owner_id="alice",
    )
    app = await _make_app(datasette, stored_queries=["content/news_by_id"])

    response = await _post_query(
        datasette,
        app,
        {"database": "content", "query": "news_by_id", "params": {"id": 2}},
    )
    assert response.json()["result"]["rows"] == [{"title": "Second"}]

    span = _query_span(otel_spans)
    assert dict(span.attributes) == {
        "apps.app_id": app["id"],
        "apps.query.source": "app",
        "apps.query.outcome": "ok",
        "apps.query.kind": "stored",
        "apps.query.write": False,
        "db.namespace": "content",
        "apps.query.rows": 1,
    }
    _assert_one_query_metric(
        otel_metrics,
        **{
            "apps.query.source": "app",
            "apps.query.outcome": "ok",
            "apps.query.kind": "stored",
            "apps.query.write": False,
        },
    )
    _assert_conforms(otel_spans, otel_metrics)


@pytest.mark.asyncio
async def test_stored_write_query(tmp_path, otel_spans, otel_metrics):
    datasette = await _datasette(tmp_path)
    await datasette.add_query(
        "content",
        "add_news",
        "insert into news (title) values (:title)",
        is_write=True,
        # Trusted, so it runs without the actor needing execute-write-sql
        is_trusted=True,
        source="user",
        owner_id="alice",
    )
    app = await _make_app(datasette, stored_queries=["content/add_news"])

    response = await _post_query(
        datasette,
        app,
        {"database": "content", "query": "add_news", "params": {"title": "Third"}},
    )
    assert response.json()["ok"] is True, response.json()

    span = _query_span(otel_spans)
    assert span.attributes["apps.query.outcome"] == "ok"
    assert span.attributes["apps.query.kind"] == "stored"
    assert span.attributes["apps.query.write"] is True
    assert "apps.query.rows" not in span.attributes
    _assert_one_query_metric(
        otel_metrics,
        **{
            "apps.query.source": "app",
            "apps.query.outcome": "ok",
            "apps.query.kind": "stored",
            "apps.query.write": True,
        },
    )
    _assert_conforms(otel_spans, otel_metrics)


@pytest.mark.asyncio
async def test_debug_query_source_and_job_id(tmp_path, otel_spans, otel_metrics):
    datasette = await _datasette(tmp_path)
    app = await _make_app(datasette)
    job = await create_debug_job(
        datasette, app_id=app["id"], actor_id="alice", javascript="return 1;"
    )

    response = await datasette.client.post(
        f"/-/apps/debug/{job['id']}/query",
        actor=ACTOR,
        json={"database": "content", "sql": "select title from news"},
    )
    assert response.json()["ok"] is True

    span = _query_span(otel_spans)
    assert span.attributes["apps.query.source"] == "debug"
    assert span.attributes["apps.debug.job_id"] == job["id"]
    assert span.attributes["apps.app_id"] == app["id"]
    _assert_one_query_metric(
        otel_metrics,
        **{
            "apps.query.source": "debug",
            "apps.query.outcome": "ok",
            "apps.query.kind": "sql",
        },
    )
    _assert_conforms(otel_spans, otel_metrics)


# --- apps.app_id on core's request spans ----------------------------------


def _request_span(otel_spans):
    # Tests drive the app through datasette.client, so the outer request
    # span is itself flagged datasette.internal_client; it is the root
    # SERVER span. (In production it would not carry the flag.)
    spans = [
        span
        for span in otel_spans.get_finished_spans()
        if span.kind == SpanKind.SERVER and span.parent is None
    ]
    assert len(spans) == 1, spans
    return spans[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path_template,kwargs,status",
    [
        ("GET", "/-/apps/{id}", {}, 200),
        ("GET", "/-/apps/{id}.json", {}, 200),
        ("GET", "/-/apps/{id}/edit", {}, 200),
        ("GET", "/-/apps/{id}/delete", {}, 200),
        ("GET", "/-/apps/{id}/revisions/1", {}, 200),
        ("GET", "/-/apps/{id}/launch", {}, 302),
        (
            "POST",
            "/-/apps/{id}/query",
            {"json": {"database": "content", "sql": "select 1"}},
            200,
        ),
    ],
)
async def test_app_routes_tag_request_span_with_app_id(
    tmp_path, otel_spans, method, path_template, kwargs, status
):
    datasette = await _datasette(tmp_path)
    app = await _make_app(datasette)

    response = await datasette.client.request(
        method, path_template.format(id=app["id"]), actor=ACTOR, **kwargs
    )
    assert response.status_code == status

    assert _request_span(otel_spans).attributes["apps.app_id"] == app["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pin", "unpin"])
async def test_pin_routes_tag_request_span_with_app_id(tmp_path, otel_spans, action):
    datasette = await _datasette(tmp_path)
    app = await _make_app(datasette)
    response = await datasette.client.post(f"/-/apps/{app['id']}/{action}", actor=ACTOR)
    assert response.status_code == 302

    assert _request_span(otel_spans).attributes["apps.app_id"] == app["id"]


@pytest.mark.asyncio
async def test_unknown_app_id_is_not_recorded(tmp_path, otel_spans):
    datasette = await _datasette(tmp_path)

    response = await datasette.client.get("/-/apps/nope", actor=ACTOR)
    assert response.status_code == 404

    assert "apps.app_id" not in _request_span(otel_spans).attributes
