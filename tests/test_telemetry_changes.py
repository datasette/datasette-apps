"""Telemetry for app changes and agent tools: the apps.changes counter and
apps.app_id / apps.tool.result on the current (tool) span."""

from dataclasses import dataclass
import json

import pytest

pytest.importorskip("opentelemetry.sdk")

from datasette.app import Datasette
from datasette.telemetry_testing import assert_metrics_conform
from opentelemetry import trace

from datasette_apps import Registry, agent_tools, telemetry_registry
from datasette_apps.agent_tools import get_app_edit_tools


@dataclass
class FakeAgentTool:
    name: str
    description: str
    input_schema: dict
    fn: object
    required_permission: str | None = None


ALICE = {"id": "alice"}
BOB = {"id": "bob"}


def _tools_by_name():
    return {tool.name: tool for tool in get_app_edit_tools(FakeAgentTool)}


async def _datasette(**kwargs):
    datasette = Datasette(memory=True, **kwargs)
    await datasette.invoke_startup()
    return datasette


async def _make_app(datasette, html="<h1>Hello</h1>\n<p>World</p>\n"):
    return await Registry(datasette).create_stored_app(
        actor_id="alice", name="Target app", description="", html=html
    )


def _changes(otel_metrics):
    otel_metrics.collect()
    return {
        (
            point.attributes["apps.change.operation"],
            point.attributes["apps.change.via"],
        ): point.value
        for point in otel_metrics.points("apps.changes")
    }


def _assert_metrics_conform(otel_metrics):
    assert_metrics_conform(
        telemetry_registry.METRICS, otel_metrics, scope_name="datasette_apps"
    )
    for point in otel_metrics.points("apps.changes"):
        assert set(point.attributes) == {"apps.change.operation", "apps.change.via"}


async def _call_in_tool_span(otel_spans, tool, **kwargs):
    """Run a tool inside a test-owned span standing in for datasette-agent's
    execute_tool span; return (result, that span's attributes)."""
    name = f"execute_tool {tool.name}"
    with trace.get_tracer("test").start_as_current_span(name):
        result = await tool.fn(**kwargs)
    spans = [s for s in otel_spans.get_finished_spans() if s.name == name]
    assert len(spans) == 1, spans
    return result, dict(spans[0].attributes)


# --- Web --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_changes_are_counted(otel_metrics):
    datasette = await _datasette(
        config={"permissions": {"create-app": {"id": "alice"}}}
    )

    create = await datasette.client.post(
        "/-/apps/create", actor=ALICE, data={"name": "Web app", "html": "<h1>Hi</h1>"}
    )
    assert create.status_code == 302
    app_id = create.headers["location"].rsplit("/", 1)[-1]

    edit = await datasette.client.post(
        f"/-/apps/{app_id}/edit",
        actor=ALICE,
        data={"name": "Web app", "html": "<h1>Updated</h1>"},
    )
    assert edit.status_code == 302
    for action in ("pin", "unpin"):
        response = await datasette.client.post(
            f"/-/apps/{app_id}/{action}", actor=ALICE
        )
        assert response.status_code == 302
    deleted = await datasette.client.post(f"/-/apps/{app_id}/delete", actor=ALICE)
    assert deleted.status_code == 302

    assert _changes(otel_metrics) == {
        ("create", "web"): 1,
        ("edit", "web"): 1,
        ("pin", "web"): 1,
        ("unpin", "web"): 1,
        ("delete", "web"): 1,
    }
    _assert_metrics_conform(otel_metrics)


@pytest.mark.asyncio
async def test_web_edit_without_changes_still_counts(otel_metrics):
    # The metric is "edit submitted and saved": a no-op POST writes no
    # revision but still counts.
    datasette = await _datasette()
    app = await _make_app(datasette, html="<h1>Same</h1>")

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/edit",
        actor=ALICE,
        data={"name": "Target app", "html": "<h1>Same</h1>"},
    )
    assert response.status_code == 302
    assert len(await Registry(datasette).list_versions(app["id"])) == 1
    assert _changes(otel_metrics) == {("edit", "web"): 1}


@pytest.mark.asyncio
async def test_forbidden_web_changes_are_not_counted(otel_metrics):
    datasette = await _datasette()
    app = await _make_app(datasette)

    for path in ("delete", "edit"):
        response = await datasette.client.post(
            f"/-/apps/{app['id']}/{path}", actor=BOB, data={"html": "<p>x</p>"}
        )
        assert response.status_code == 403
    create = await datasette.client.post(
        "/-/apps/create", actor=BOB, data={"name": "Nope", "html": "<p>x</p>"}
    )
    assert create.status_code == 403
    missing = await datasette.client.post("/-/apps/nope/delete", actor=ALICE)
    assert missing.status_code == 404

    assert _changes(otel_metrics) == {}


@pytest.mark.asyncio
async def test_plugin_registered_apps_are_not_counted(otel_metrics):
    datasette = await _datasette()
    await Registry(datasette).add_app(
        id="plugin:one", name="Plugin One", description="", path="/plugin-one"
    )
    assert _changes(otel_metrics) == {}


# --- Agent ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_create_counts_and_tags_span(otel_spans, otel_metrics):
    datasette = await _datasette(
        config={"permissions": {"create-app": {"id": "alice"}}}
    )
    tools = _tools_by_name()

    result, attributes = await _call_in_tool_span(
        otel_spans,
        tools["app_create"],
        datasette=datasette,
        actor=ALICE,
        name="Agent app",
        html="<h1>Secret HTML</h1>",
    )
    app_id = json.loads(result)["app_id"]
    assert attributes["apps.app_id"] == app_id
    assert attributes["apps.tool.result"] == "ok"
    assert _changes(otel_metrics) == {("create", "agent"): 1}
    _assert_metrics_conform(otel_metrics)


@pytest.mark.asyncio
async def test_agent_create_denied(otel_spans, otel_metrics):
    datasette = await _datasette()
    result, attributes = await _call_in_tool_span(
        otel_spans,
        _tools_by_name()["app_create"],
        datasette=datasette,
        actor=ALICE,
        name="Denied app",
        html="<p>x</p>",
    )
    assert json.loads(result)["error"].startswith("Permission denied")
    assert attributes["apps.tool.result"] == "denied"
    assert "apps.app_id" not in attributes
    assert _changes(otel_metrics) == {}


@pytest.mark.asyncio
async def test_agent_create_error(otel_spans, otel_metrics):
    datasette = await _datasette(
        config={"permissions": {"create-app": {"id": "alice"}}}
    )
    result, attributes = await _call_in_tool_span(
        otel_spans,
        _tools_by_name()["app_create"],
        datasette=datasette,
        actor=ALICE,
        name="Bad CSP",
        html="<p>x</p>",
        csp_origins=["not an origin"],
    )
    assert "error" in json.loads(result)
    assert attributes["apps.tool.result"] == "error"
    assert "apps.app_id" not in attributes
    assert _changes(otel_metrics) == {}


@pytest.mark.asyncio
async def test_agent_edit_batch_counts_once(otel_spans, otel_metrics):
    datasette = await _datasette()
    app = await _make_app(datasette)

    result, attributes = await _call_in_tool_span(
        otel_spans,
        _tools_by_name()["app_edit"],
        datasette=datasette,
        actor=ALICE,
        app_id=app["id"],
        edits=[
            {"operation": "str_replace", "old_str": "Hello", "new_str": "Goodbye"},
            {"operation": "insert", "insert_line": 2, "insert_text": "<hr>\n"},
        ],
    )
    assert json.loads(result)["edits_applied"] == 2
    assert attributes["apps.app_id"] == app["id"]
    assert attributes["apps.tool.result"] == "ok"
    assert _changes(otel_metrics) == {("edit", "agent"): 1}
    _assert_metrics_conform(otel_metrics)


@pytest.mark.asyncio
async def test_agent_edit_partial_failure_is_not_counted(otel_spans, otel_metrics):
    datasette = await _datasette()
    app = await _make_app(datasette)

    result, attributes = await _call_in_tool_span(
        otel_spans,
        _tools_by_name()["app_edit"],
        datasette=datasette,
        actor=ALICE,
        app_id=app["id"],
        edits=[
            {"operation": "str_replace", "old_str": "Hello", "new_str": "Goodbye"},
            {"operation": "str_replace", "old_str": "Missing", "new_str": "Nope"},
        ],
    )
    assert "Edit #2 failed" in json.loads(result)["error"]
    assert attributes["apps.app_id"] == app["id"]
    assert attributes["apps.tool.result"] == "error"
    assert _changes(otel_metrics) == {}


@pytest.mark.asyncio
async def test_agent_edit_denied_is_not_counted(otel_spans, otel_metrics):
    datasette = await _datasette()
    app = await _make_app(datasette)

    result, attributes = await _call_in_tool_span(
        otel_spans,
        _tools_by_name()["app_edit"],
        datasette=datasette,
        actor=BOB,
        app_id=app["id"],
        edits=[{"operation": "str_replace", "old_str": "Hello", "new_str": "Hi"}],
    )
    assert json.loads(result)["error"] == "Permission denied: edit-app"
    assert attributes["apps.app_id"] == app["id"]
    assert attributes["apps.tool.result"] == "denied"
    assert _changes(otel_metrics) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,kwargs,counted",
    [
        ("app_view", {}, False),
        ("app_view", {"view_range": "1,1"}, False),
        ("app_str_replace", {"old_str": "Hello", "new_str": "Hi"}, True),
        ("app_insert", {"insert_line": 0, "insert_text": "<hr>\n"}, True),
        ("app_render", {}, False),
    ],
)
async def test_other_edit_tools_tag_span(
    otel_spans, otel_metrics, tool_name, kwargs, counted
):
    datasette = await _datasette()
    app = await _make_app(datasette)

    result, attributes = await _call_in_tool_span(
        otel_spans,
        _tools_by_name()[tool_name],
        datasette=datasette,
        actor=ALICE,
        app_id=app["id"],
        **kwargs,
    )
    assert "error" not in json.loads(result)
    assert attributes["apps.app_id"] == app["id"]
    assert attributes["apps.tool.result"] == "ok"
    assert _changes(otel_metrics) == ({("edit", "agent"): 1} if counted else {})


@pytest.mark.asyncio
async def test_edit_tool_error_for_missing_text(otel_spans, otel_metrics):
    datasette = await _datasette()
    app = await _make_app(datasette)

    result, attributes = await _call_in_tool_span(
        otel_spans,
        _tools_by_name()["app_str_replace"],
        datasette=datasette,
        actor=ALICE,
        app_id=app["id"],
        old_str="Missing",
        new_str="x",
    )
    assert "error" in json.loads(result)
    assert attributes["apps.tool.result"] == "error"
    assert _changes(otel_metrics) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,kwargs",
    [
        ("app_view", {}),
        (
            "app_edit",
            {"edits": [{"operation": "insert", "insert_line": 0, "insert_text": "x"}]},
        ),
    ],
)
@pytest.mark.parametrize("allowed", [True, False])
async def test_unknown_app_id_is_never_recorded(
    otel_spans, otel_metrics, monkeypatch, tool_name, kwargs, allowed
):
    # The model-supplied app_id is only recorded once it names a real app.
    # Datasette's edit-app check always refuses an ID with no stored app row,
    # so EditToolset's "Content not found" path is only reachable if the app
    # vanishes after the check; force the check open to exercise it.
    datasette = await _datasette()
    await _make_app(datasette)
    if allowed:

        async def allow(datasette, actor, app_id):
            return True

        monkeypatch.setattr(agent_tools, "_can_edit_app", allow)
    actor = ALICE
    expected = "error" if allowed else "denied"

    result, attributes = await _call_in_tool_span(
        otel_spans,
        _tools_by_name()[tool_name],
        datasette=datasette,
        actor=actor,
        app_id="SENTINEL not an app",
        **kwargs,
    )
    assert "error" in json.loads(result)
    assert attributes["apps.tool.result"] == expected
    assert "apps.app_id" not in attributes
    assert not any("SENTINEL" in str(value) for value in attributes.values())
    assert _changes(otel_metrics) == {}


@pytest.mark.asyncio
async def test_denied_call_on_real_app_is_tagged(otel_spans):
    # Consistent with the app routes, which tag apps.app_id after the 404
    # check but before the permission check.
    datasette = await _datasette()
    app = await _make_app(datasette)

    _, attributes = await _call_in_tool_span(
        otel_spans,
        _tools_by_name()["app_view"],
        datasette=datasette,
        actor=BOB,
        app_id=app["id"],
    )
    assert attributes == {"apps.app_id": app["id"], "apps.tool.result": "denied"}


@pytest.mark.asyncio
async def test_app_list_tags_result_without_app_id(otel_spans):
    datasette = await _datasette()
    await _make_app(datasette)
    tools = _tools_by_name()

    result, attributes = await _call_in_tool_span(
        otel_spans, tools["app_list"], datasette=datasette, actor=ALICE
    )
    assert json.loads(result)["returned_count"] == 1
    assert attributes["apps.tool.result"] == "ok"
    assert "apps.app_id" not in attributes

    otel_spans.clear()
    result, attributes = await _call_in_tool_span(
        otel_spans, tools["app_list"], datasette=datasette, actor=ALICE, limit=99
    )
    assert "error" in json.loads(result)
    assert attributes["apps.tool.result"] == "error"


@pytest.mark.asyncio
async def test_tool_span_carries_no_tool_arguments(otel_spans):
    datasette = await _datasette(
        config={"permissions": {"create-app": {"id": "alice"}}}
    )
    _, attributes = await _call_in_tool_span(
        otel_spans,
        _tools_by_name()["app_create"],
        datasette=datasette,
        actor=ALICE,
        name="SENTINEL_NAME",
        description="SENTINEL_DESCRIPTION",
        html="<h1>SENTINEL_HTML</h1>",
    )
    assert set(attributes) == {"apps.app_id", "apps.tool.result"}
    assert not any("SENTINEL" in str(value) for value in attributes.values())


def test_tool_signatures_unchanged():
    # datasette-agent passes datasette/actor/context by keyword and only
    # passes context when "context" is a named parameter of fn.
    import inspect

    tools = _tools_by_name()
    for name in (
        "app_list",
        "app_create",
        "app_view",
        "app_str_replace",
        "app_insert",
        "app_edit",
        "app_render",
    ):
        parameters = inspect.signature(tools[name].fn).parameters
        assert list(parameters)[:2] == ["datasette", "actor"], name
        assert "context" not in parameters, name
    assert "context" in inspect.signature(tools["app_debug"].fn).parameters


@pytest.mark.asyncio
async def test_real_datasette_agent_tool_span(otel_spans):
    """Only once the datasette-agent dev dependency ships telemetry."""
    pytest.importorskip("datasette_agent.telemetry")
    from datasette_agent.tools import AgentTool, execute_agent_tool

    datasette = await _datasette()
    app = await _make_app(datasette)
    tool = {t.name: t for t in get_app_edit_tools(AgentTool)}["app_view"]

    await execute_agent_tool(tool, datasette, ALICE, arguments={"app_id": app["id"]})
    spans = [
        s for s in otel_spans.get_finished_spans() if s.name == "execute_tool app_view"
    ]
    assert len(spans) == 1
    assert spans[0].attributes["apps.app_id"] == app["id"]
    assert spans[0].attributes["apps.tool.result"] == "ok"
