"""Tests for debug job storage and the /-/apps/debug/* endpoints."""

from datetime import datetime, timedelta, timezone

import pytest
from datasette.app import Datasette

from datasette_apps import Registry
from datasette_apps.debug import (
    DEFAULT_TIMEOUT_MS,
    DEFAULT_VIEWPORT,
    JOB_EXPIRY_SECONDS,
    build_debug_harness_html,
    complete_debug_job,
    create_debug_job,
    debug_task_payload,
    expire_debug_job,
    get_debug_job,
)
from datasette_apps.rendering import iframe_bridge_script


async def _make_app(datasette, *, html="<h1>Debug me</h1>", sql_databases=None):
    return await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Debuggable app",
        description="",
        html=html,
        sql_databases=sql_databases or [],
    )


async def _make_job(datasette, app, **kwargs):
    kwargs.setdefault("actor_id", "alice")
    kwargs.setdefault("javascript", "return document.title;")
    return await create_debug_job(datasette, app_id=app["id"], **kwargs)


async def _backdate_job(datasette, job_id, seconds):
    stale = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    await datasette.get_internal_database().execute_write(
        "UPDATE _app_debug_jobs SET created_at = ? WHERE id = ?",
        [stale, job_id],
    )


@pytest.mark.asyncio
async def test_create_debug_job_records_current_version_and_defaults():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    registry = Registry(datasette)
    app = await _make_app(datasette)
    await registry.update_stored_app(
        app["id"], "Debuggable app", "", "<h1>Version two</h1>", actor_id="alice"
    )

    job = await _make_job(datasette, app)
    assert job["id"]
    assert job["app_id"] == app["id"]
    assert job["actor_id"] == "alice"
    assert job["version"] == 2
    assert job["status"] == "pending"
    assert job["javascript"] == "return document.title;"
    assert job["config"]["viewport"] == DEFAULT_VIEWPORT
    assert job["config"]["timeout_ms"] == DEFAULT_TIMEOUT_MS
    assert job["config"]["channel_token"]
    assert job["config"]["frame_token"]

    fetched = await get_debug_job(datasette, job["id"])
    assert fetched["id"] == job["id"]
    assert fetched["version"] == 2


@pytest.mark.asyncio
async def test_create_debug_job_validates_app_viewport_and_timeout():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)

    with pytest.raises(ValueError):
        await create_debug_job(
            datasette, actor_id="alice", app_id="nope", javascript="return 1;"
        )
    with pytest.raises(ValueError):
        await _make_job(datasette, app, viewport={"width": 10, "height": 10})
    with pytest.raises(ValueError):
        await _make_job(datasette, app, viewport={"width": "wide"})

    job = await _make_job(
        datasette, app, viewport={"width": 375, "height": 812}, timeout_ms=999999
    )
    assert job["config"]["viewport"] == {"width": 375, "height": 812}
    # timeout_ms is clamped rather than rejected
    assert job["config"]["timeout_ms"] == 60000
    job = await _make_job(datasette, app, timeout_ms=1)
    assert job["config"]["timeout_ms"] == 1000


@pytest.mark.asyncio
async def test_debug_task_payload_carries_run_specifics():
    datasette = Datasette(memory=True, settings={"base_url": "/prefix/"})
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    job = await _make_job(datasette, app, viewport={"width": 375, "height": 812})

    payload = debug_task_payload(datasette, job)
    assert payload == {
        "frame_url": (
            f"/prefix/-/apps/debug/{job['id']}/frame"
            f"?token={job['config']['frame_token']}"
        ),
        "query_url": f"/prefix/-/apps/debug/{job['id']}/query",
        "channel_token": job["config"]["channel_token"],
        "javascript": "return document.title;",
        "viewport": {"width": 375, "height": 812},
        "timeout_ms": DEFAULT_TIMEOUT_MS,
    }


@pytest.mark.asyncio
async def test_frame_serves_job_revision_with_debug_bridge():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    registry = Registry(datasette)
    app = await _make_app(datasette, html="<h1>Version one</h1>")
    job = await _make_job(datasette, app)
    # An edit made after job creation is not what this job tests
    await registry.update_stored_app(
        app["id"], "Debuggable app", "", "<h1>Version two</h1>", actor_id="alice"
    )

    frame_path = f"/-/apps/debug/{job['id']}/frame"
    frame_url = debug_task_payload(datasette, job)["frame_url"]

    response = await datasette.client.get(frame_url)
    assert response.status_code == 200
    assert "<h1>Version one</h1>" in response.text
    assert "<h1>Version two</h1>" not in response.text
    assert 'http-equiv="Content-Security-Policy"' in response.text
    assert "datasette-app-debug-eval" in response.text
    assert "waitFor" in response.text
    assert job["config"]["channel_token"] in response.text
    assert response.headers["cache-control"] == "no-store"

    # The frame token is the capability: no token or a wrong token is
    # refused, and unknown jobs 404
    assert (await datasette.client.get(frame_path)).status_code == 403
    assert (
        await datasette.client.get(f"{frame_path}?token=wrong")
    ).status_code == 403
    assert (
        await datasette.client.get("/-/apps/debug/nope/frame?token=x")
    ).status_code == 404

    # Once the job has its result the frame stops serving
    await complete_debug_job(datasette, job["id"], {"ok": True})
    assert (await datasette.client.get(frame_url)).status_code == 403


@pytest.mark.asyncio
async def test_frame_and_query_refuse_expired_jobs():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette, sql_databases=["_memory"])
    job = await _make_job(datasette, app)
    await _backdate_job(datasette, job["id"], JOB_EXPIRY_SECONDS + 60)

    frame_url = debug_task_payload(datasette, job)["frame_url"]
    assert (await datasette.client.get(frame_url)).status_code == 403

    query = await datasette.client.post(
        f"/-/apps/debug/{job['id']}/query",
        actor={"id": "alice"},
        json={"database": "_memory", "sql": "select 1"},
    )
    assert query.status_code == 403
    assert (await get_debug_job(datasette, job["id"]))["status"] == "expired"


@pytest.mark.asyncio
async def test_query_endpoint_enforces_app_allowlists_and_actor():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette, sql_databases=["_memory"])
    job = await _make_job(datasette, app)
    query_path = f"/-/apps/debug/{job['id']}/query"
    body = {"database": "_memory", "sql": "select 1 as one"}

    response = await datasette.client.post(
        query_path, actor={"id": "alice"}, json=body
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "result": {"columns": ["one"], "rows": [{"one": 1}]},
    }

    denied = await datasette.client.post(
        query_path,
        actor={"id": "alice"},
        json={"database": "other", "sql": "select 1"},
    )
    assert denied.status_code == 200
    assert denied.json()["ok"] is False
    assert "not allowed" in denied.json()["error"]

    assert (
        await datasette.client.post(query_path, actor={"id": "bob"}, json=body)
    ).status_code == 403
    assert (await datasette.client.post(query_path, json=body)).status_code == 403

    # Queries stop once the job has its result
    await complete_debug_job(datasette, job["id"], {"ok": True})
    assert (
        await datasette.client.post(query_path, actor={"id": "alice"}, json=body)
    ).status_code == 403


@pytest.mark.asyncio
async def test_claim_and_result_endpoints_are_gone():
    # The one-shot claim and result delivery moved into datasette-agent's
    # browser-task runtime; datasette-apps no longer exposes them.
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    job = await _make_job(datasette, app)

    for suffix in ("claim", "result"):
        response = await datasette.client.post(
            f"/-/apps/debug/{job['id']}/{suffix}", actor={"id": "alice"}
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_complete_debug_job_stores_audit_envelope_once():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    job = await _make_job(datasette, app)

    envelope = {
        "ok": True,
        "result": {"title": "Debug me"},
        "events": {
            "errors": [{"kind": "javascript-error", "message": "boom"}],
            "logs": [{"kind": "console-log", "message": "hi"}] * 200,
        },
        "duration_ms": 1234,
        "timed_out": False,
    }
    assert await complete_debug_job(datasette, job["id"], envelope)

    stored = await get_debug_job(datasette, job["id"])
    assert stored["status"] == "completed"
    assert stored["completed_at"]
    assert stored["result"]["ok"] is True
    assert stored["result"]["result"] == {"title": "Debug me"}
    assert stored["result"]["duration_ms"] == 1234
    assert stored["result"]["timed_out"] is False
    assert stored["result"]["events"]["errors"] == [
        {"kind": "javascript-error", "message": "boom"}
    ]
    # Event lists are capped on write
    assert len(stored["result"]["events"]["logs"]) == 100

    # Completing twice is refused; so is completing an expired job
    assert not await complete_debug_job(datasette, job["id"], {"ok": False})

    other = await _make_job(datasette, app)
    await expire_debug_job(datasette, other["id"])
    assert not await complete_debug_job(datasette, other["id"], {"ok": True})
    assert (await get_debug_job(datasette, other["id"]))["status"] == "expired"


@pytest.mark.asyncio
async def test_complete_debug_job_normalizes_failure_envelopes():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    job = await _make_job(datasette, app)

    assert await complete_debug_job(
        datasette, job["id"], {"ok": False, "error": {"message": "script threw"}}
    )
    stored = await get_debug_job(datasette, job["id"])
    assert stored["result"]["ok"] is False
    assert stored["result"]["error"] == {"message": "script threw"}
    assert stored["result"]["events"] == {"errors": [], "logs": []}
    assert stored["result"]["timed_out"] is False


def test_debug_harness_html_is_a_generic_bootstrap():
    harness = build_debug_harness_html()
    assert "<script" in harness
    # Task identity comes from the runtime's data-task-id container; the
    # payload arrives through the one-shot claim - nothing job-specific
    # lives in the HTML
    assert "[data-task-id]" in harness
    assert "datasetteAgent" in harness
    assert "claimTask" in harness
    assert "completeTask" in harness
    # Layout-preserving hiding: hiding via display would zero out
    # every measurement the debug script takes
    assert "opacity: 0" in harness
    assert "pointer-events: none" in harness
    assert "display: none" not in harness


def test_iframe_bridge_debug_mode_gates_eval_channel():
    plain = iframe_bridge_script("token-1")
    assert "datasette-app-debug-eval" not in plain
    assert "waitFor" not in plain

    debug = iframe_bridge_script("token-1", debug=True)
    assert "datasette-app-debug-eval" in debug
    assert "datasette-app-debug-result" in debug
    assert "waitFor" in debug
    assert "window.debug" in debug
    # Eval runs via an inline script element permitted by the production
    # CSP - never eval()/new Function which would need 'unsafe-eval'
    assert "eval(" not in debug
    assert "new Function" not in debug
    assert "not JSON-serializable" in debug


@pytest.mark.asyncio
async def test_stored_app_view_has_no_debug_eval_channel():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    app = await _make_app(datasette)
    response = await datasette.client.get(
        f"/-/apps/{app['id']}", actor={"id": "alice"}
    )
    assert response.status_code == 200
    assert "datasette-app-debug-eval" not in response.text
