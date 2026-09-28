"""Registry <-> reality conformance and the sentinel privacy walk.

One broad workload (``exercise``) emits every registered span, metric and
attribute, then a single ``collect()`` feeds the kit's four conformance
assertions (always scoped to ``datasette_apps``: the fixtures also capture
core's spans and any other installed plugin's). Attributes this plugin sets
on *other* scopes' spans - core's request spans and the agent tool span -
are not seen by the scoped helpers, so they are checked explicitly.

The workload is driven with sentinel values in every private input. Two
privacy passes follow: an unscoped one (core's telemetry included) for
everything except SQL, and a scoped one for SQL text and stored query
names, which core records by design (``db.query`` spans carry SQL text;
the internal ``datasette.client`` request's ``url.path`` carries a stored
query's name). The sentinels therefore never appear inside SQL literals,
apart from the one scoped SQL sentinel.
"""

import json
import secrets
import sqlite3

import pytest

pytest.importorskip("opentelemetry.sdk")

from datasette.app import Datasette
from datasette.telemetry_testing import (
    assert_metrics_conform,
    assert_metrics_covered,
    assert_no_forbidden_values,
    assert_spans_conform,
    assert_spans_covered,
)
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from datasette_apps import Registry, views
from datasette_apps.debug import (
    JOB_EXPIRY_SECONDS,
    debug_task_payload,
    get_debug_job_for_call,
)
from datasette_apps.telemetry_registry import (
    APP_ID,
    ATTRIBUTES,
    DEBUG_JOB_ID,
    DEBUG_OUTCOME,
    M_DEBUG_RUN_DURATION,
    M_DEBUG_SCRIPT_DURATION,
    METRICS,
    QUERY,
    REQUEST_SPAN_ATTRIBUTES,
    SPANS,
    TOOL_RESULT,
    TOOL_SPAN_ATTRIBUTES,
)
from test_debug_agent_tool import (
    FakeBrowserTaskPending,
    FakeContext,
    _tools_by_name,
)
from test_debug_jobs import _backdate_job

SCOPE = "datasette_apps"

# --- Sentinels ----------------------------------------------------------------
#
# Planted in every private input. None of these may appear in any signal, in
# any scope. They are bound parameters, form fields, JSON bodies and tool
# arguments - never SQL text - so core's db.query spans (which record SQL
# text, but never parameter values) cannot produce false positives.

SENTINELS = {
    "actor": "sentinel-actor-3f9c",
    "other_actor": "sentinel-actor-other-6a12",
    "app_name": "Sentinel App Name 7d21",
    "app_description": "sentinel-description-b0e4",
    "app_html": "sentinel-html-51aa",
    "param_value": "sentinel-param-c77e",
    "stored_write_value": "sentinel-write-9a0d",
    "csp_origin": "https://sentinel-origin-2b.example",
    "debug_js": "sentinel-js-e61f",
    "debug_result": "sentinel-result-8c3b",
    "debug_error": "sentinel-error-40fa",
    "agent_edit_text": "sentinel-edit-1d9e",
    "app_list_query": "sentinel-search-77c0",
    "conversation_id": "sentinel-conversation-4b8e",
}

# Checked against this plugin's scope only. The SQL sentinel is a table name,
# so Datasette's "no such table" error - the AppQueryError message the app
# sees - quotes it; core's db.query span records the SQL (and that error) by
# design. A stored query's name is in the internal request's url.path.
SQL_SENTINEL = "sentinel_sql_5e3d"
STORED_QUERY_SENTINEL = "sentinel_stored_query_0f7b"
# Allowlisted but never created: not_found, no internal request
MISSING_QUERY_SENTINEL = "sentinel_missing_query_93d1"
# Created but not on the app's allowlist: not_allowed
UNLISTED_QUERY_SENTINEL = "sentinel_unlisted_query_c24a"
SCOPED_SENTINELS = {
    SQL_SENTINEL,
    STORED_QUERY_SENTINEL,
    MISSING_QUERY_SENTINEL,
    UNLISTED_QUERY_SENTINEL,
}

ACTOR = {"id": SENTINELS["actor"]}
OTHER_ACTOR = {"id": SENTINELS["other_actor"]}
HTML = f"<p>{SENTINELS['app_html']}</p>\n"
DEBUG_JS = f"/* {SENTINELS['debug_js']} */ return 1;"


def _envelope(**overrides):
    envelope = {
        "ok": True,
        "result": {"text": SENTINELS["debug_result"]},
        "events": {
            "errors": [{"kind": "console-error", "message": SENTINELS["debug_result"]}],
            "logs": [{"kind": "console-log", "message": SENTINELS["debug_result"]}],
        },
        "duration_ms": 321,
        "timed_out": False,
        "outcome": "completed",
    }
    envelope.update(overrides)
    return envelope


ERROR = {"message": SENTINELS["debug_error"], "stack": SENTINELS["debug_error"]}
DEBUG_OUTCOMES = {
    "ok": _envelope(),
    "script_error": _envelope(ok=False, error=ERROR, duration_ms=12),
    "timed_out": _envelope(ok=False, error=ERROR, timed_out=True, duration_ms=None),
    "task_expired": {"ok": False, "error": ERROR, "outcome": "expired"},
    "task_cancelled": {"ok": False, "error": ERROR, "outcome": "cancelled"},
}


class _Boom(Exception):
    "Escapes the query body once, to exercise outcome error / error.type."


# --- Workload -----------------------------------------------------------------


def _databases(tmp_path):
    content = tmp_path / "content.db"
    conn = sqlite3.connect(content)
    conn.executescript("""
        create table news (id integer primary key, title text);
        insert into news (title) values ('First'), ('Second');
        """)
    conn.close()
    secret = tmp_path / "secret.db"
    sqlite3.connect(secret).execute("create table t (id integer)").connection.close()
    return [str(content), str(secret)]


def _context(call_key, **kwargs):
    return FakeContext(
        conversation_id=SENTINELS["conversation_id"], call_key=call_key, **kwargs
    )


class Workload:
    def __init__(self, datasette):
        self.datasette = datasette
        self.tools = _tools_by_name()
        self.tool_span_ids = set()
        self.calls = 0

    async def tool(self, tool_name, actor=ACTOR, **kwargs):
        """Call an agent tool inside a stand-in for datasette-agent's
        execute_tool span. Exceptions (BrowserTaskPending) are not recorded
        on it: their message is the task label, which names the app."""
        with trace.get_tracer("test").start_as_current_span(
            f"execute_tool {tool_name}",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            self.tool_span_ids.add(span.get_span_context().span_id)
            return await self.tools[tool_name].fn(
                datasette=self.datasette, actor=actor, **kwargs
            )

    async def debug(self, app_id, outcome=None, call_key=None, **kwargs):
        if call_key is None:
            self.calls += 1
            call_key = f"call-{self.calls}"
        kwargs.setdefault("javascript", DEBUG_JS)
        kwargs.setdefault("context", _context(call_key, outcome=outcome))
        return await self.tool("app_debug", app_id=app_id, **kwargs)

    async def suspend(self, app_id):
        "First execution of app_debug: leaves a pending job, records nothing."
        self.calls += 1
        call_key = f"call-{self.calls}"
        with pytest.raises(FakeBrowserTaskPending):
            await self.debug(app_id, call_key=call_key)
        job = await get_debug_job_for_call(
            self.datasette, SENTINELS["conversation_id"], call_key
        )
        return call_key, job

    async def query(self, app, body, path=None):
        return await self.datasette.client.post(
            path or f"/-/apps/{app['id']}/query",
            actor=ACTOR,
            content=body if isinstance(body, str) else json.dumps(body),
        )


async def exercise(tmp_path, monkeypatch):
    # Every per-run secret (frame, channel and view bridge tokens) comes
    # from secrets.token_urlsafe: capture them all as forbidden values.
    tokens = []
    real_token_urlsafe = secrets.token_urlsafe

    def capturing_token_urlsafe(*args, **kwargs):
        token = real_token_urlsafe(*args, **kwargs)
        tokens.append(token)
        return token

    monkeypatch.setattr(secrets, "token_urlsafe", capturing_token_urlsafe)

    datasette = Datasette(
        _databases(tmp_path),
        memory=True,
        config={
            "permissions": {
                "create-app": {"id": ACTOR["id"]},
                "apps-set-csp": {"id": ACTOR["id"]},
            },
            # Visible to nobody, so an allowed query against it is denied
            "databases": {"secret": {"allow": {"id": "nobody"}}},
        },
    )
    await datasette.invoke_startup()
    work = Workload(datasette)
    registry = Registry(datasette)

    # --- App queries: every outcome, both kinds, both sources ---------------
    await datasette.add_query(
        "content",
        STORED_QUERY_SENTINEL,
        "select title from news where title != :title",
        source="user",
        owner_id=ACTOR["id"],
    )
    await datasette.add_query(
        "content",
        UNLISTED_QUERY_SENTINEL,
        "select 1",
        source="user",
        owner_id=ACTOR["id"],
    )
    await datasette.add_query(
        "content",
        "add_news",
        "insert into news (title) values (:title)",
        is_write=True,
        # Trusted, so it runs without the actor needing execute-write-sql
        is_trusted=True,
        source="user",
        owner_id=ACTOR["id"],
    )
    app = await registry.create_stored_app(
        actor_id=ACTOR["id"],
        name=SENTINELS["app_name"],
        description=SENTINELS["app_description"],
        html=HTML,
        sql_databases=["content", "secret", "missing"],
        stored_queries=[
            f"content/{STORED_QUERY_SENTINEL}",
            "content/add_news",
            f"content/{MISSING_QUERY_SENTINEL}",
        ],
        csp_origins=[SENTINELS["csp_origin"]],
    )
    param = SENTINELS["param_value"]
    results = {
        "ok": await work.query(
            app,
            {"database": "content", "sql": "select :v as v", "params": {"v": param}},
        ),
        "not_allowed": await work.query(
            app, {"database": "_memory", "sql": "select 1"}
        ),
        "denied": await work.query(app, {"database": "secret", "sql": "select 1"}),
        "failed": await work.query(
            app, {"database": "content", "sql": f"select * from {SQL_SENTINEL}"}
        ),
        "invalid_json": await work.query(app, "not json"),
        "invalid_params": await work.query(
            app, {"database": "content", "sql": "select 1", "params": [param]}
        ),
        "not_found_db": await work.query(
            app, {"database": "missing", "sql": "select 1"}
        ),
        "not_found_query": await work.query(
            app, {"database": "content", "query": MISSING_QUERY_SENTINEL}
        ),
        "not_allowed_query": await work.query(
            app, {"database": "content", "query": UNLISTED_QUERY_SENTINEL}
        ),
        "stored_read": await work.query(
            app,
            {
                "database": "content",
                "query": STORED_QUERY_SENTINEL,
                "params": {"title": param},
            },
        ),
        "stored_write": await work.query(
            app,
            {
                "database": "content",
                "query": "add_news",
                "params": {"title": SENTINELS["stored_write_value"]},
            },
        ),
    }

    async def boom(*args, **kwargs):
        raise _Boom()

    with monkeypatch.context() as patch:
        patch.setattr(views, "run_app_query", boom)
        results["error"] = await work.query(
            app, {"database": "content", "sql": "select 1"}
        )

    assert results["ok"].json()["result"]["rows"] == [{"v": param}]
    assert results["stored_read"].json()["result"]["rows"]
    assert results["stored_write"].json()["ok"] is True
    assert results["error"].status_code == 500
    # The AppQueryError message the app sees quotes the SQL sentinel
    assert SQL_SENTINEL in results["failed"].json()["error"]
    assert "not allowed" in results["not_allowed_query"].json()["error"]
    assert results["not_found_query"].json()["error"] == "Stored query not found"
    for name in ("not_allowed", "denied", "invalid_json", "not_found_query"):
        assert results[name].json()["ok"] is False, name

    # --- Web changes --------------------------------------------------------
    create = await datasette.client.post(
        "/-/apps/create",
        actor=ACTOR,
        data={
            "name": SENTINELS["app_name"],
            "description": SENTINELS["app_description"],
            "html": HTML,
            "csp_origins": SENTINELS["csp_origin"],
        },
    )
    assert create.status_code == 302
    web_app_id = create.headers["location"].rsplit("/", 1)[-1]
    assert (await registry.get_csp_origins(web_app_id)) == [SENTINELS["csp_origin"]]
    view = await datasette.client.get(f"/-/apps/{web_app_id}", actor=ACTOR)
    assert view.status_code == 200
    edit = await datasette.client.post(
        f"/-/apps/{web_app_id}/edit",
        actor=ACTOR,
        data={
            "name": SENTINELS["app_name"],
            "description": SENTINELS["app_description"],
            "html": f"<p>{SENTINELS['agent_edit_text']}</p>",
        },
    )
    assert edit.status_code == 302
    for action in ("pin", "unpin", "delete"):
        response = await datasette.client.post(
            f"/-/apps/{web_app_id}/{action}", actor=ACTOR
        )
        assert response.status_code == 302, action

    # --- Agent tools: every apps.tool.result --------------------------------
    created = json.loads(
        await work.tool(
            "app_create",
            name=SENTINELS["app_name"],
            description=SENTINELS["app_description"],
            html=HTML,
            csp_origins=[SENTINELS["csp_origin"]],
        )
    )
    agent_app_id = created["app_id"]
    edited = json.loads(
        await work.tool(
            "app_edit",
            app_id=agent_app_id,
            edits=[
                {
                    "operation": "str_replace",
                    "old_str": SENTINELS["app_html"],
                    "new_str": SENTINELS["agent_edit_text"],
                }
            ],
        )
    )
    assert edited["edits_applied"] == 1
    assert "error" in json.loads(
        await work.tool(
            "app_create", name=SENTINELS["app_name"], html=HTML, actor=OTHER_ACTOR
        )
    )
    assert "error" in json.loads(
        await work.tool(
            "app_str_replace",
            app_id=agent_app_id,
            old_str=SENTINELS["debug_error"],
            new_str=SENTINELS["agent_edit_text"],
        )
    )
    await work.tool("app_list", query=SENTINELS["app_list_query"])

    # --- app_debug: the browser flow, then every other outcome --------------
    call_key, job = await work.suspend(app["id"])
    frame_url = debug_task_payload(datasette, job)["frame_url"]
    assert job["config"]["frame_token"] in frame_url
    frame = await datasette.client.get(frame_url, actor=ACTOR)
    assert frame.status_code == 200
    debug_query = await work.query(
        app,
        {"database": "content", "sql": "select :v as v", "params": {"v": param}},
        path=f"/-/apps/debug/{job['id']}/query",
    )
    assert debug_query.json()["ok"] is True
    await work.debug(app["id"], outcome=DEBUG_OUTCOMES["ok"], call_key=call_key)
    for name in ("script_error", "timed_out", "task_expired", "task_cancelled"):
        await work.debug(app["id"], outcome=DEBUG_OUTCOMES[name])
    await work.debug(app["id"], actor=OTHER_ACTOR)  # denied
    await work.debug(app["id"], viewport={"width": 1, "height": 1})  # invalid
    await work.debug(  # unsupported
        app["id"],
        context=_context("call-unsupported", supports_browser_tasks=False),
    )

    # A pending job hit after its window: lazily expired and counted
    _, stale = await work.suspend(app["id"])
    await _backdate_job(datasette, stale["id"], JOB_EXPIRY_SECONDS + 60)
    stale_frame = debug_task_payload(datasette, stale)["frame_url"]
    assert (await datasette.client.get(stale_frame, actor=ACTOR)).status_code == 403

    assert job["config"]["frame_token"] in tokens
    assert job["config"]["channel_token"] in tokens
    # The view bridge token was minted and handed to the page
    assert any(token in view.text for token in tokens)
    return {
        "tokens": tokens,
        # Apps reached through HTTP routes; the agent-created one only ever
        # through tools. The stale job's frame request was refused before
        # its request span could be tagged.
        "route_app_ids": {app["id"], web_app_id},
        "route_job_ids": {job["id"]},
        "tool_app_ids": {app["id"], agent_app_id},
        "tool_job_ids": {job["id"]},
        "tool_span_ids": work.tool_span_ids,
    }


# --- The test -------------------------------------------------------------------


def _is_ours(span):
    return span.instrumentation_scope is not None and (
        span.instrumentation_scope.name == SCOPE
    )


@pytest.mark.asyncio
async def test_conformance_and_privacy(tmp_path, otel_spans, otel_metrics, monkeypatch):
    seen = await exercise(tmp_path, monkeypatch)
    finished = otel_spans.get_finished_spans()
    # Exactly one collect(): the reader is delta-temporality, so anything an
    # earlier collect() drained would be invisible to the coverage checks.
    otel_metrics.collect()

    # --- Conformance, in both directions ------------------------------------
    assert_spans_conform(SPANS, finished, scope_name=SCOPE)
    assert_spans_covered(SPANS, finished, scope_name=SCOPE)
    assert_metrics_conform(METRICS, otel_metrics, scope_name=SCOPE)
    assert_metrics_covered(METRICS, otel_metrics, scope_name=SCOPE)

    ours = [span for span in finished if _is_ours(span)]
    foreign = [span for span in finished if not _is_ours(span)]

    # --- Privacy ------------------------------------------------------------
    forbidden = set(SENTINELS.values()) | set(seen["tokens"])
    # 1. Unscoped: core's telemetry included. Covers the frame token in
    #    ?token= (core records url.path without the query string) and the
    #    parameter values sent as query string args to /-/query.json.
    assert_no_forbidden_values(
        forbidden, finished_spans=finished, collector=otel_metrics
    )
    # 2. Scoped: SQL text and stored query names. Core records both by
    #    design, so first prove the sentinels really reached telemetry. The
    #    SQL sentinel is also the text of an AppQueryError message (the
    #    "failed" query's "no such table" error), so this proves exception
    #    messages stay off apps.query too...
    core_text = json.dumps(
        [dict(span.attributes or {}) for span in foreign]
        + [span.status.description for span in foreign],
        default=str,
    )
    assert SQL_SENTINEL in core_text
    assert STORED_QUERY_SENTINEL in core_text
    # ...then that this plugin never adds them again.
    assert_no_forbidden_values(
        SCOPED_SENTINELS,
        finished_spans=finished,
        collector=otel_metrics,
        scope_name=SCOPE,
    )
    # The kit checks status descriptions for sentinels, not their absence:
    # this plugin never sets one, and never records an exception event.
    for span in ours:
        assert not span.status.description, span.name
        assert not span.events, span.name

    query_spans = [span for span in ours if span.name == QUERY]
    # assert_spans_covered skips optional attributes: every one must still
    # have been exercised.
    for attribute in QUERY.attributes:
        assert any(
            attribute in span.attributes for span in query_spans
        ), f"{attribute} never set on {QUERY}"
    for attribute in QUERY.attributes:
        if attribute.values is not None:
            assert {
                span.attributes[attribute]
                for span in query_spans
                if attribute in span.attributes
            } == set(attribute.values), attribute
    for metric in METRICS:
        for attribute in metric.attributes:
            assert any(
                attribute in point.attributes for point in otel_metrics.points(metric)
            ), f"{metric}: {attribute} never collected"
    # Every declared enum value was collected, so no dimension is dead. The
    # debug duration histograms are recorded only for runs that came back
    # from the browser (and the script duration only when it reported
    # duration_ms), by design.
    browser = {"ok", "script_error", "timed_out", "task_expired", "task_cancelled"}
    expected_values = {
        (M_DEBUG_RUN_DURATION, DEBUG_OUTCOME): browser,
        (M_DEBUG_SCRIPT_DURATION, DEBUG_OUTCOME): {"ok", "script_error"},
    }
    for metric in METRICS:
        for attribute in metric.attributes:
            if attribute.values is not None:
                assert {
                    point.attributes[attribute]
                    for point in otel_metrics.points(metric)
                    if attribute in point.attributes
                } == expected_values.get(
                    (metric, attribute), set(attribute.values)
                ), f"{metric}: {attribute}"

    # --- Attributes on other scopes' spans ----------------------------------
    registered = {str(attribute): attribute for attribute in ATTRIBUTES}
    for span in finished:
        for key, value in (span.attributes or {}).items():
            if key.startswith("apps."):
                assert key in registered, f"{span.name}: unregistered {key}"
                allowed = registered[key].values
                assert allowed is None or value in allowed, f"{span.name}: {key}"
    request_spans = [
        span
        for span in foreign
        if span.kind == SpanKind.SERVER
        and span.instrumentation_scope.name == "datasette"
    ]
    tool_spans = [
        span
        for span in foreign
        if span.get_span_context().span_id in seen["tool_span_ids"]
    ]
    for span in request_spans:
        ours_on_it = {key for key in span.attributes if key.startswith("apps.")}
        assert ours_on_it <= set(REQUEST_SPAN_ATTRIBUTES), span.name
    for attribute in REQUEST_SPAN_ATTRIBUTES:
        assert any(
            attribute in span.attributes for span in request_spans
        ), f"{attribute} never set on a request span"
    assert {s.attributes.get(APP_ID) for s in request_spans} - {None} == seen[
        "route_app_ids"
    ]
    assert {s.attributes.get(DEBUG_JOB_ID) for s in request_spans} - {None} == seen[
        "route_job_ids"
    ]

    for span in tool_spans:
        assert set(span.attributes) <= set(TOOL_SPAN_ATTRIBUTES), span.name
    for attribute in TOOL_SPAN_ATTRIBUTES:
        assert any(
            attribute in span.attributes for span in tool_spans
        ), f"{attribute} never set on a tool span"
    assert {
        s.attributes[TOOL_RESULT] for s in tool_spans if TOOL_RESULT in s.attributes
    } == set(TOOL_RESULT.values)
    assert {
        s.attributes[DEBUG_OUTCOME] for s in tool_spans if DEBUG_OUTCOME in s.attributes
    } == set(DEBUG_OUTCOME.values)
    assert {s.attributes.get(APP_ID) for s in tool_spans} - {None} == seen[
        "tool_app_ids"
    ]
    assert {s.attributes.get(DEBUG_JOB_ID) for s in tool_spans} - {None} >= seen[
        "tool_job_ids"
    ]
