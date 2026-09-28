"""
Every span, metric and attribute datasette-apps emits.

Instrumentation scope: ``datasette_apps``. Prefix: ``apps.*`` (``datasette.*``
is reserved for Datasette core). Shared names reused from core:
``db.namespace`` and ``error.type``.

Built on Datasette's plugin telemetry kit (``datasette.telemetry_registry``):
``Attribute`` / ``SpanName`` / ``MetricName`` subclass ``str``, so a registry
entry *is* the name handed to OpenTelemetry. Call sites import these
constants; no wire name is written anywhere else.

Privacy: nothing here records app HTML, names or descriptions, SQL text or
parameter values, stored query names, debug scripts or their results, CSP
origins, actor or conversation IDs, tokens, or exception messages.
``apps.app_id``, ``apps.debug.job_id`` and ``db.namespace`` ride on spans
only, never on a metric.
"""

from datasette.telemetry_registry import (
    COUNTER,
    DB_NAMESPACE,
    DURATION_BUCKETS,
    ERROR_TYPE,
    HISTOGRAM,
    Attribute,
    MetricName,
    SpanName,
)

__all__ = [
    "ATTRIBUTES",
    "DEBUG_RUN_BUCKETS",
    "DEBUG_SCRIPT_BUCKETS",
    "DURATION_BUCKETS",
    "METRICS",
    "REQUEST_SPAN_ATTRIBUTES",
    "SPANS",
    "TOOL_SPAN_ATTRIBUTES",
]

# --- Histogram bucket boundaries ------------------------------------------

# A debug run is wall-clock from job creation to the resumed tool call
# recording its envelope: it includes the browser claiming the task, so it
# can far exceed the script's own timeout (max 60s, debug.py MAX_TIMEOUT_MS),
# up to the 15 minute job window (debug.py JOB_EXPIRY_SECONDS).
DEBUG_RUN_BUCKETS = (0.5, 1, 2.5, 5, 10, 15, 30, 60, 120, 300, 900)
# The browser-reported duration_ms of the script itself, capped by
# MAX_TIMEOUT_MS = 60000.
DEBUG_SCRIPT_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 30, 60)


# --- Attributes -----------------------------------------------------------

APP_ID = Attribute(
    "apps.app_id",
    "The app's ID: a ULID for stored apps, or a plugin-supplied ID for "
    "external apps. Unbounded, so it rides on spans only - **never on a "
    "metric**. Besides ``apps.query``, it is added to spans this plugin "
    "does not own: core's HTTP request span for the app's routes (view, "
    "JSON, edit, delete, revision, pin, unpin, launch, query, and the "
    "``app_debug`` frame and query routes), and the current span inside "
    "this plugin's agent tools, which with datasette-agent's telemetry is "
    "its ``execute_tool`` span in the ``datasette_agent`` scope. Only "
    "recorded once the ID is known to name a real app, so a made-up ID in "
    "a URL or a tool argument is never recorded.",
)
DEBUG_JOB_ID = Attribute(
    "apps.debug.job_id",
    "The ``_app_debug_jobs.id`` (a ULID) of an ``app_debug`` run. Spans "
    "only - **never on a metric**. Present on ``apps.query`` only for "
    "queries made by a debug run. Also added to core's HTTP request span "
    "for the debug run's ``/frame`` and ``/query`` routes (once the job is "
    "known to be running and to belong to the actor) and to the "
    "``app_debug`` tool span.",
    optional=True,
)
QUERY_SOURCE = Attribute(
    "apps.query.source",
    "Which endpoint the app's query arrived on: ``app`` "
    "(``/-/apps/{id}/query``, a running app) or ``debug`` "
    "(``/-/apps/debug/{job}/query``, an ``app_debug`` run).",
    values={"app", "debug"},
)
QUERY_KIND = Attribute(
    "apps.query.kind",
    "``sql`` for ``datasette.query()``, ``stored`` for "
    "``datasette.storedQuery()``. Absent when the request body is not valid "
    "JSON.",
    optional=True,
    values={"sql", "stored"},
)
QUERY_WRITE = Attribute(
    "apps.query.write",
    "True for a stored query declared ``is_write``. Only known once the "
    "stored query has been resolved, so absent otherwise.",
    optional=True,
    values={True, False},
)
QUERY_OUTCOME = Attribute(
    "apps.query.outcome",
    "How the app query ended. ``ok``: the query ran and returned a result. "
    "``invalid``: the request body or parameters were malformed. "
    "``not_allowed``: the database or stored query is not on the app's "
    "allowlist. ``not_found``: the database or stored query doesn't exist. "
    "``denied``: Datasette's permission checks refused the actor. "
    "``failed``: Datasette rejected or failed the query (SQL error, time "
    "limit, write denied by a read-only query). ``error``: an unexpected "
    "exception escaped - the only outcome that sets span status ``ERROR``. "
    "Clamped at the call site: anything unrecognised becomes ``error``.",
    values={"ok", "invalid", "not_allowed", "not_found", "denied", "failed", "error"},
)
QUERY_ROWS = Attribute(
    "apps.query.rows",
    "Number of rows returned. Only on ``ok`` read queries.",
    optional=True,
)
CHANGE_OPERATION = Attribute(
    "apps.change.operation",
    "What changed: an app was created, edited, deleted, pinned or unpinned.",
    values={"create", "edit", "delete", "pin", "unpin"},
)
CHANGE_VIA = Attribute(
    "apps.change.via",
    "Where the change came from: ``web`` (the plugin's HTML forms) or "
    "``agent`` (datasette-agent tools).",
    values={"web", "agent"},
)
TOOL_RESULT = Attribute(
    "apps.tool.result",
    "How one of this plugin's agent tools ended: ``ok``, ``error`` or "
    "``denied`` (the actor lacked permission). Set on the current tool span "
    "(datasette-agent's ``execute_tool`` span), not on a span of our own, "
    "so it sits in the ``datasette_agent`` scope; with a datasette-agent "
    "that has no telemetry it lands on whatever span is current, or "
    "nowhere. Set only when the tool returns: an exception escaping the "
    "tool leaves it unset (a datasette-agent version with telemetry "
    "records ``error.type`` on its own span).",
    values={"ok", "error", "denied"},
)
DEBUG_OUTCOME = Attribute(
    "apps.debug.outcome",
    "How an ``app_debug`` run ended. ``ok``: the script ran and the envelope "
    "reported success. ``script_error``: the script threw, or the harness "
    "reported an error. ``timed_out``: the script hit its timeout. "
    "``task_expired`` / ``task_cancelled``: the browser task expired or was "
    "cancelled before a result came back. ``denied``: the actor may not edit "
    "the app. ``invalid``: the debug request was rejected (e.g. a bad "
    "viewport). ``unsupported``: the agent host does not support browser "
    "tasks. Also set on the ``app_debug`` tool span alongside "
    "``apps.tool.result``. There is no ``error`` outcome: an exception "
    "escaping the tool records nothing.",
    values={
        "ok",
        "script_error",
        "timed_out",
        "task_expired",
        "task_cancelled",
        "denied",
        "invalid",
        "unsupported",
    },
)


# --- Spans ----------------------------------------------------------------

QUERY = SpanName(
    "apps.query",
    "One query from a running app (or an ``app_debug`` run) through the "
    "plugin's query endpoint: body parsing, the allowlist check and the "
    "internal ``datasette.client`` call to Datasette's JSON API, which nests "
    "inside as a SERVER span tagged ``datasette.internal_client``. Only an "
    "unexpected exception (outcome ``error``) sets status ``ERROR``; "
    "exception messages are never recorded. ``db.namespace`` reuses core's "
    "attribute, which is declared as always present, but here it is set "
    "only once the database has passed the app's allowlist - so it is "
    "absent for ``invalid`` and ``not_allowed`` queries, and the requested "
    "database name is never recorded unvetted. The internal request nests "
    "under this span because the plugin injects W3C trace context headers "
    "into it with the global propagator, a workaround for "
    "https://github.com/simonw/datasette/issues/2969: with the default "
    "propagator core would otherwise start it as a new trace. With "
    "``OTEL_PROPAGATORS=none`` nothing is injected, but core extracts "
    "nothing either, so it still nests.",
    attributes=(
        APP_ID,
        QUERY_SOURCE,
        QUERY_OUTCOME,
        QUERY_KIND,
        QUERY_WRITE,
        DB_NAMESPACE,
        QUERY_ROWS,
        DEBUG_JOB_ID,
        ERROR_TYPE,
    ),
)


# --- Metrics --------------------------------------------------------------

M_QUERIES = MetricName(
    "apps.queries",
    COUNTER,
    "{query}",
    "App queries handled, by source, outcome, kind and whether a stored "
    "query was a write.",
    attributes=(QUERY_SOURCE, QUERY_OUTCOME, QUERY_KIND, QUERY_WRITE),
)
M_QUERY_DURATION = MetricName(
    "apps.query.duration",
    HISTOGRAM,
    "s",
    "Time to handle one app query end to end, as the app sees it. Uses "
    "core's duration buckets so it lines up with "
    "``db.client.operation.duration``.",
    attributes=(QUERY_SOURCE, QUERY_OUTCOME, QUERY_KIND),
    buckets=DURATION_BUCKETS,
)
M_CHANGES = MetricName(
    "apps.changes",
    COUNTER,
    "{change}",
    "App changes (create, edit, delete, pin, unpin), by operation and "
    "whether they came from the web UI or the agent. Counted only after the "
    "write succeeded. An edit is one submitted and saved edit form or agent "
    "edit call (an ``app_edit`` batch counts once), even when nothing "
    "changed and no revision was written. Apps registered by other plugins "
    "through the ``Registry`` API (``add_app``/``add_apps``) are not "
    "counted.",
    attributes=(CHANGE_OPERATION, CHANGE_VIA),
)
M_DEBUG_RUNS = MetricName(
    "apps.debug.runs",
    COUNTER,
    "{run}",
    "Terminal ``app_debug`` tool executions, by outcome. The first "
    "execution that suspends waiting for the browser is not counted, and "
    "neither is an execution that ends in an escaped exception (there is "
    "no ``error`` outcome).",
    attributes=(DEBUG_OUTCOME,),
)
M_DEBUG_RUN_DURATION = MetricName(
    "apps.debug.run.duration",
    HISTOGRAM,
    "s",
    "Wall-clock time from debug job creation to the resumed tool call "
    "recording its result, including the browser claiming the task. Only "
    "for outcomes that came back from the browser.",
    attributes=(DEBUG_OUTCOME,),
    buckets=DEBUG_RUN_BUCKETS,
)
M_DEBUG_SCRIPT_DURATION = MetricName(
    "apps.debug.script.duration",
    HISTOGRAM,
    "s",
    "The debug script's own run time as reported by the browser "
    "(``duration_ms``), capped by the 60s maximum script timeout.",
    attributes=(DEBUG_OUTCOME,),
    buckets=DEBUG_SCRIPT_BUCKETS,
)
M_DEBUG_JOBS_EXPIRED = MetricName(
    "apps.debug.jobs.expired",
    COUNTER,
    "{job}",
    "Pending debug jobs flipped to ``expired`` because a ``/frame`` or "
    "``/query`` request arrived after the 15 minute job window. Counted "
    "only when a row actually changed.",
)


# Attributes this plugin adds to spans it does not own. The scoped
# conformance helpers never see them, so the conformance test and the
# generated README reference list them from here.
REQUEST_SPAN_ATTRIBUTES = (APP_ID, DEBUG_JOB_ID)
TOOL_SPAN_ATTRIBUTES = (APP_ID, DEBUG_JOB_ID, TOOL_RESULT, DEBUG_OUTCOME)

SPANS = (QUERY,)

METRICS = (
    M_QUERIES,
    M_QUERY_DURATION,
    M_CHANGES,
    M_DEBUG_RUNS,
    M_DEBUG_RUN_DURATION,
    M_DEBUG_SCRIPT_DURATION,
    M_DEBUG_JOBS_EXPIRED,
)

ATTRIBUTES = (
    APP_ID,
    DEBUG_JOB_ID,
    QUERY_SOURCE,
    QUERY_KIND,
    QUERY_WRITE,
    QUERY_OUTCOME,
    QUERY_ROWS,
    CHANGE_OPERATION,
    CHANGE_VIA,
    TOOL_RESULT,
    DEBUG_OUTCOME,
    DB_NAMESPACE,
    ERROR_TYPE,
)
