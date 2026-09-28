"""Telemetry for app_debug runs: apps.debug.runs by outcome, run and script
durations, apps.debug.jobs.expired, and job/app IDs on the debug routes'
request spans."""

import json

import pytest

pytest.importorskip("opentelemetry.sdk")

from datasette.app import Datasette
from datasette.telemetry_testing import assert_metrics_conform
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from datasette_apps import agent_tools, telemetry_registry
from datasette_apps.debug import (
    JOB_EXPIRY_SECONDS,
    create_debug_job,
    debug_task_payload,
    expire_debug_job,
    get_debug_job,
    get_debug_job_for_call,
)
from test_debug_agent_tool import (
    COMPLETED_OUTCOME,
    FakeBrowserTaskPending,
    FakeContext,
    _make_app,
    _tools_by_name,
)
from test_debug_jobs import _backdate_job

ALICE = {"id": "alice"}
TOOL_SPAN = "execute_tool app_debug"


async def _datasette():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    return datasette


async def _call_debug(otel_spans, context, **kwargs):
    """Call app_debug inside a test-owned span standing in for
    datasette-agent's execute_tool span; return (result, span attributes).
    """
    tool = _tools_by_name()["app_debug"]
    kwargs.setdefault("javascript", "return 1;")
    with trace.get_tracer("test").start_as_current_span(TOOL_SPAN):
        result = await tool.fn(
            datasette=kwargs.pop("datasette"),
            actor=kwargs.pop("actor", ALICE),
            context=context,
            **kwargs,
        )
    spans = [s for s in otel_spans.get_finished_spans() if s.name == TOOL_SPAN]
    attributes = dict(spans[-1].attributes)
    otel_spans.clear()
    return result, attributes


# The collector's reader uses delta temporality: collect() once per check,
# then read every metric from that snapshot.


def _runs(otel_metrics):
    otel_metrics.collect()
    return {
        point.attributes["apps.debug.outcome"]: point.value
        for point in otel_metrics.points("apps.debug.runs")
    }


def _histogram_points(otel_metrics, name):
    "Points from the snapshot taken by the preceding _runs() call."
    return list(otel_metrics.points(name))


def _assert_metrics_conform(otel_metrics):
    assert_metrics_conform(
        telemetry_registry.METRICS, otel_metrics, scope_name="datasette_apps"
    )


# --- Outcomes ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,expected,tool_result",
    [
        (dict(COMPLETED_OUTCOME), "ok", "ok"),
        (
            {
                "ok": False,
                "error": {"message": "boom"},
                "duration_ms": 12,
                "outcome": "completed",
            },
            "script_error",
            "ok",
        ),
        (
            # The harness's timeout envelope sets ok: false AND timed_out
            {
                "ok": False,
                "error": {"message": "Debug run timed out after 15000ms"},
                "timed_out": True,
                "outcome": "completed",
            },
            "timed_out",
            "ok",
        ),
        (
            {
                "ok": False,
                "error": {"message": "Browser task timed out before completing"},
                "outcome": "expired",
            },
            "task_expired",
            "error",
        ),
        (
            {
                "ok": False,
                "error": {"message": "Cancelled by the user"},
                "outcome": "cancelled",
            },
            "task_cancelled",
            "error",
        ),
    ],
)
async def test_browser_outcomes(
    otel_spans, otel_metrics, outcome, expected, tool_result
):
    datasette = await _datasette()
    app = await _make_app(datasette)

    result, attributes = await _call_debug(
        otel_spans, FakeContext(outcome=outcome), datasette=datasette, app_id=app["id"]
    )
    job = await get_debug_job_for_call(datasette, "conv-1", "call-1")

    assert _runs(otel_metrics) == {expected: 1}
    run_points = _histogram_points(otel_metrics, "apps.debug.run.duration")
    assert len(run_points) == 1
    assert run_points[0].attributes == {"apps.debug.outcome": expected}
    assert run_points[0].sum >= 0
    assert attributes == {
        "apps.app_id": app["id"],
        "apps.debug.job_id": job["id"],
        "apps.debug.outcome": expected,
        "apps.tool.result": tool_result,
    }
    # The tool's result is unchanged by instrumentation
    assert json.loads(result)["app_id"] == app["id"]
    _assert_metrics_conform(otel_metrics)


@pytest.mark.asyncio
async def test_denied(otel_spans, otel_metrics):
    datasette = await _datasette()
    app = await _make_app(datasette)

    result, attributes = await _call_debug(
        otel_spans,
        FakeContext(),
        datasette=datasette,
        actor={"id": "bob"},
        app_id=app["id"],
    )
    assert json.loads(result)["error"] == "Permission denied: edit-app"
    assert _runs(otel_metrics) == {"denied": 1}
    assert _histogram_points(otel_metrics, "apps.debug.run.duration") == []
    # A denied call on a real app is tagged with it, as in ticket 04
    assert attributes == {
        "apps.app_id": app["id"],
        "apps.debug.outcome": "denied",
        "apps.tool.result": "denied",
    }


@pytest.mark.asyncio
async def test_invalid_viewport(otel_spans, otel_metrics):
    datasette = await _datasette()
    app = await _make_app(datasette)

    result, attributes = await _call_debug(
        otel_spans,
        FakeContext(),
        datasette=datasette,
        app_id=app["id"],
        viewport={"width": 10, "height": 10},
    )
    assert "viewport" in json.loads(result)["error"]
    assert _runs(otel_metrics) == {"invalid": 1}
    assert _histogram_points(otel_metrics, "apps.debug.run.duration") == []
    assert attributes == {
        "apps.app_id": app["id"],
        "apps.debug.outcome": "invalid",
        "apps.tool.result": "error",
    }


@pytest.mark.asyncio
async def test_invalid_unknown_app_id_is_never_recorded(
    otel_spans, otel_metrics, monkeypatch
):
    # Datasette's edit-app check refuses an ID with no stored app row, so
    # force it open to reach create_debug_job's "App not found".
    async def allow(datasette, actor, app_id):
        return True

    monkeypatch.setattr(agent_tools, "_can_edit_app", allow)
    datasette = await _datasette()

    result, attributes = await _call_debug(
        otel_spans,
        FakeContext(),
        datasette=datasette,
        app_id="SENTINEL not an app",
    )
    assert json.loads(result)["error"] == "App not found"
    assert _runs(otel_metrics) == {"invalid": 1}
    assert attributes == {
        "apps.debug.outcome": "invalid",
        "apps.tool.result": "error",
    }


@pytest.mark.asyncio
async def test_unsupported(otel_spans, otel_metrics):
    datasette = await _datasette()
    app = await _make_app(datasette)

    result, attributes = await _call_debug(
        otel_spans,
        FakeContext(supports_browser_tasks=False),
        datasette=datasette,
        app_id=app["id"],
    )
    assert "interactive" in json.loads(result)["error"]
    job = await get_debug_job_for_call(datasette, "conv-1", "call-1")
    assert _runs(otel_metrics) == {"unsupported": 1}
    assert _histogram_points(otel_metrics, "apps.debug.run.duration") == []
    assert attributes == {
        "apps.app_id": app["id"],
        "apps.debug.job_id": job["id"],
        "apps.debug.outcome": "unsupported",
        "apps.tool.result": "error",
    }


# --- Suspend, then resume ---------------------------------------------------


@pytest.mark.asyncio
async def test_suspend_then_resume_records_exactly_once(otel_spans, otel_metrics):
    datasette = await _datasette()
    app = await _make_app(datasette)
    tool = _tools_by_name()["app_debug"]

    # First execution: suspends waiting for the browser, records nothing
    with trace.get_tracer("test").start_as_current_span(TOOL_SPAN):
        with pytest.raises(FakeBrowserTaskPending):
            await tool.fn(
                datasette=datasette,
                actor=ALICE,
                context=FakeContext(),
                app_id=app["id"],
                javascript="return 1;",
            )
    (suspended,) = [s for s in otel_spans.get_finished_spans() if s.name == TOOL_SPAN]
    assert not {
        "apps.debug.outcome",
        "apps.tool.result",
        "apps.debug.job_id",
    } & set(suspended.attributes)
    otel_spans.clear()
    job = await get_debug_job_for_call(datasette, "conv-1", "call-1")
    await _backdate_job(datasette, job["id"], 3)

    # Second execution: the resumed call records the run. Metrics are
    # collected only now, so the snapshot covers both executions.
    _, attributes = await _call_debug(
        otel_spans,
        FakeContext(outcome=dict(COMPLETED_OUTCOME)),
        datasette=datasette,
        app_id=app["id"],
    )
    assert (await get_debug_job(datasette, job["id"]))["status"] == "completed"
    assert _runs(otel_metrics) == {"ok": 1}
    (runs_point,) = otel_metrics.points("apps.debug.runs")
    assert runs_point.value == 1
    (run_point,) = _histogram_points(otel_metrics, "apps.debug.run.duration")
    assert run_point.count == 1
    (script_point,) = _histogram_points(otel_metrics, "apps.debug.script.duration")
    assert script_point.count == 1
    # Wall-clock from job creation, not from the resumed call
    assert run_point.sum >= 3
    assert attributes["apps.debug.job_id"] == job["id"]
    assert attributes["apps.debug.outcome"] == "ok"
    _assert_metrics_conform(otel_metrics)


# --- Script duration --------------------------------------------------------


@pytest.mark.asyncio
async def test_script_duration_recorded_from_envelope(otel_spans, otel_metrics):
    datasette = await _datasette()
    app = await _make_app(datasette)
    outcome = dict(COMPLETED_OUTCOME, duration_ms=1234)

    await _call_debug(
        otel_spans, FakeContext(outcome=outcome), datasette=datasette, app_id=app["id"]
    )
    assert _runs(otel_metrics) == {"ok": 1}
    (point,) = _histogram_points(otel_metrics, "apps.debug.script.duration")
    assert point.attributes == {"apps.debug.outcome": "ok"}
    assert point.count == 1
    assert point.sum == pytest.approx(1.234)
    # Lands in the (1, 2.5] bucket
    bounds = list(point.explicit_bounds)
    bucket = bounds.index(2.5)
    assert bounds[bucket - 1] == 1
    assert point.bucket_counts[bucket] == 1
    assert sum(point.bucket_counts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("duration_ms", [None, -5])
async def test_no_script_duration_without_a_usable_duration_ms(
    otel_spans, otel_metrics, duration_ms
):
    datasette = await _datasette()
    app = await _make_app(datasette)
    outcome = dict(COMPLETED_OUTCOME)
    if duration_ms is None:
        outcome.pop("duration_ms")
    else:
        outcome["duration_ms"] = duration_ms

    await _call_debug(
        otel_spans, FakeContext(outcome=outcome), datasette=datasette, app_id=app["id"]
    )
    assert _runs(otel_metrics) == {"ok": 1}
    assert _histogram_points(otel_metrics, "apps.debug.script.duration") == []


# --- Lazy expiry ------------------------------------------------------------


def _expired(otel_metrics):
    otel_metrics.collect()
    return sum(point.value for point in otel_metrics.points("apps.debug.jobs.expired"))


@pytest.mark.asyncio
async def test_lazy_expiry_counts_only_flipped_rows(otel_metrics):
    datasette = await _datasette()
    app = await _make_app(datasette)
    job = await create_debug_job(
        datasette, app_id=app["id"], actor_id="alice", javascript="return 1;"
    )
    await _backdate_job(datasette, job["id"], JOB_EXPIRY_SECONDS + 60)
    frame_url = debug_task_payload(datasette, job)["frame_url"]

    assert (await datasette.client.get(frame_url, actor=ALICE)).status_code == 403
    assert _expired(otel_metrics) == 1
    _assert_metrics_conform(otel_metrics)
    # Already expired: still 403, adds nothing (delta since the last collect)
    assert (await datasette.client.get(frame_url, actor=ALICE)).status_code == 403
    query = await datasette.client.post(
        f"/-/apps/debug/{job['id']}/query",
        actor=ALICE,
        json={"database": "_memory", "sql": "select 1"},
    )
    assert query.status_code == 403
    assert _expired(otel_metrics) == 0


# --- Debug route request spans ----------------------------------------------


def _request_span(otel_spans):
    # The test client's own request is the root SERVER span (see
    # test_telemetry_queries._request_span).
    spans = [
        span
        for span in otel_spans.get_finished_spans()
        if span.kind == SpanKind.SERVER and span.parent is None
    ]
    assert len(spans) == 1, spans
    return spans[0]


async def _job(datasette):
    app = await _make_app(datasette, sql_databases=["_memory"])
    job = await create_debug_job(
        datasette, app_id=app["id"], actor_id="alice", javascript="return 1;"
    )
    return app, job


@pytest.mark.asyncio
async def test_frame_request_span_carries_job_and_app(otel_spans):
    datasette = await _datasette()
    app, job = await _job(datasette)

    response = await datasette.client.get(
        debug_task_payload(datasette, job)["frame_url"], actor=ALICE
    )
    assert response.status_code == 200
    attributes = _request_span(otel_spans).attributes
    assert attributes["apps.debug.job_id"] == job["id"]
    assert attributes["apps.app_id"] == app["id"]
    # The frame token rides in the query string, which core never records
    token = job["config"]["frame_token"]
    for span in otel_spans.get_finished_spans():
        assert token not in json.dumps(dict(span.attributes), default=str)


@pytest.mark.asyncio
async def test_query_request_span_carries_job_and_app(otel_spans):
    datasette = await _datasette()
    app, job = await _job(datasette)

    response = await datasette.client.post(
        f"/-/apps/debug/{job['id']}/query",
        actor=ALICE,
        json={"database": "_memory", "sql": "select 1"},
    )
    assert response.json()["ok"] is True
    attributes = _request_span(otel_spans).attributes
    assert attributes["apps.debug.job_id"] == job["id"]
    assert attributes["apps.app_id"] == app["id"]
    (query_span,) = [
        s for s in otel_spans.get_finished_spans() if s.name == "apps.query"
    ]
    assert query_span.attributes["apps.query.source"] == "debug"
    assert query_span.attributes["apps.debug.job_id"] == job["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,method,kwargs",
    [
        ("frame", "GET", {}),
        ("query", "POST", {"json": {"database": "_memory", "sql": "select 1"}}),
    ],
)
async def test_forbidden_debug_requests_are_not_tagged(
    otel_spans, path, method, kwargs
):
    datasette = await _datasette()
    _, job = await _job(datasette)
    url = f"/-/apps/debug/{job['id']}/{path}"
    if path == "frame":
        url = debug_task_payload(datasette, job)["frame_url"]

    response = await datasette.client.request(
        method, url, actor={"id": "mallory"}, **kwargs
    )
    assert response.status_code == 403
    attributes = _request_span(otel_spans).attributes
    assert "apps.debug.job_id" not in attributes
    assert "apps.app_id" not in attributes


@pytest.mark.asyncio
async def test_unknown_job_id_is_not_tagged(otel_spans):
    datasette = await _datasette()

    response = await datasette.client.get(
        "/-/apps/debug/made-up-job/frame?token=x", actor=ALICE
    )
    assert response.status_code == 404
    attributes = _request_span(otel_spans).attributes
    assert "apps.debug.job_id" not in attributes
    assert "apps.app_id" not in attributes


@pytest.mark.asyncio
async def test_expire_debug_job_reports_whether_it_flipped_the_row():
    datasette = await _datasette()
    app = await _make_app(datasette)
    job = await create_debug_job(
        datasette, app_id=app["id"], actor_id="alice", javascript="return 1;"
    )
    assert await expire_debug_job(datasette, job["id"]) is True
    # A second (e.g. concurrent) request finds nothing left to flip
    assert await expire_debug_job(datasette, job["id"]) is False


# --- Real datasette-agent runtime -------------------------------------------


@pytest.mark.asyncio
async def test_real_datasette_agent_suspend_and_resume_records_once(
    tmp_path, otel_metrics
):
    """Both executions of one logical run through the real browser-task
    runtime record exactly one apps.debug.runs point."""
    pytest.importorskip("datasette_agent")
    from datasette_agent.browser_tasks import (
        BrowserTaskPending,
        claim_task,
        complete_task,
    )
    from datasette_agent.schema import ensure_tables
    from datasette_agent.tools import ToolContext

    datasette = Datasette(memory=True, internal=str(tmp_path / "internal.db"))
    await datasette.invoke_startup()
    await ensure_tables(datasette.get_internal_database())
    app = await _make_app(datasette)
    tool = _tools_by_name()["app_debug"]

    async def run():
        return await tool.fn(
            datasette=datasette,
            actor=ALICE,
            context=ToolContext(
                datasette=datasette,
                actor=ALICE,
                conversation_id="01CONVERSATION00000000TEST",
                tool_name="app_debug",
                arguments={"app_id": app["id"], "javascript": "return 1;"},
                tool_call_id="call_1",
                supports_browser_tasks=True,
            ),
            app_id=app["id"],
            javascript="return 1;",
        )

    with pytest.raises(BrowserTaskPending) as excinfo:
        await run()
    task = excinfo.value.task
    db = datasette.get_internal_database()
    await claim_task(db, task["id"], "alice")
    await complete_task(
        db,
        task["id"],
        {"ok": False, "error": {"message": "x"}, "duration_ms": 10},
        "alice",
    )
    assert json.loads(await run())["ok"] is False

    assert _runs(otel_metrics) == {"script_error": 1}
    (point,) = otel_metrics.points("apps.debug.runs")
    assert point.value == 1
    (script_point,) = _histogram_points(otel_metrics, "apps.debug.script.duration")
    assert script_point.sum == pytest.approx(0.01)
