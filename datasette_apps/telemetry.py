"""OpenTelemetry helpers for datasette-apps.

Depends on ``opentelemetry-api`` only, like Datasette core: this module
never creates a ``TracerProvider`` or ``MeterProvider``, never configures an
exporter, and must never import ``opentelemetry.sdk`` (a test imports the
package in a fresh subprocess and checks). With no provider installed every
span is a ``NonRecordingSpan`` and every instrument is a no-op - turning
telemetry on is the operator's move (``opentelemetry-instrument datasette
...``), exactly as with core.

The tracer and meter live under their own ``datasette_apps``
instrumentation scope, never core's ``datasette`` scope. Context
propagation is via contextvars, so this plugin's spans nest inside core's
request span, and core's spans nest inside ours.

Every wire name comes from ``telemetry_registry``. Instruments are
module-level: OpenTelemetry's proxy meter forwards to a provider installed
later, and the test harness installs its tracer provider in a
session-scoped autouse fixture before any span is created.
"""

import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version

from datasette.telemetry import request_span
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.propagate import inject
from opentelemetry.trace import Status, StatusCode

from . import telemetry_registry as reg


def _version():
    try:
        return version("datasette-apps")
    except PackageNotFoundError:
        return None


# No schema_url: our attributes are plugin-specific apart from db.namespace
# and error.type; see the plugin telemetry docs ("Omit schema_url if you are
# unsure which version applies").
tracer = otel_trace.get_tracer("datasette_apps", _version())
meter = otel_metrics.get_meter("datasette_apps", _version())


# --- Instruments ----------------------------------------------------------
#
# Each instrument passes the SDK a short plain-text description; the
# registry entry carries the longer documentation. Unit and buckets always
# come off the registry entry so the create_*() call cannot drift from it.


def _histogram(entry, description):
    return meter.create_histogram(
        entry,
        unit=entry.unit,
        description=description,
        explicit_bucket_boundaries_advisory=entry.buckets,
    )


def _counter(entry, description):
    return meter.create_counter(entry, unit=entry.unit, description=description)


queries = _counter(reg.M_QUERIES, "App queries handled, by source and outcome")
query_duration = _histogram(
    reg.M_QUERY_DURATION, "Time to handle one app query, by source and outcome"
)
changes = _counter(reg.M_CHANGES, "App changes, by operation and origin")
debug_runs = _counter(reg.M_DEBUG_RUNS, "Terminal app_debug runs, by outcome")
debug_run_duration = _histogram(
    reg.M_DEBUG_RUN_DURATION, "Wall-clock time of an app_debug run, by outcome"
)
debug_script_duration = _histogram(
    reg.M_DEBUG_SCRIPT_DURATION,
    "Browser-reported debug script duration, by outcome",
)
debug_jobs_expired = _counter(
    reg.M_DEBUG_JOBS_EXPIRED, "Pending debug jobs lazily marked expired"
)


# --- Helpers --------------------------------------------------------------


def clamp(value, allowed, default):
    """Clamp an attribute value to its registry enum.

    Enum attributes declare a closed ``values=`` set in the registry and the
    conformance helpers enforce membership; this is the call-site half of
    that promise - anything unexpected becomes ``default`` instead of
    minting a new metric series.
    """
    return value if value in allowed else default


def error_type(exc):
    "The exception's class name for ``error.type`` - never its message."
    return type(exc).__qualname__


def _without_none(mapping):
    return {key: value for key, value in mapping.items() if value is not None}


def set_attributes(span, mapping):
    "Set attributes on ``span`` if it is recording, skipping ``None`` values."
    if span is None or not span.is_recording():
        return
    for key, value in mapping.items():
        if value is not None:
            span.set_attribute(key, value)


def annotate_request(request, mapping):
    "Add attributes to core's recording request span for this request, if any."
    set_attributes(request_span(request.scope), mapping)


def annotate_current_span(mapping):
    "Add attributes to the current span (e.g. datasette-agent's tool span)."
    set_attributes(otel_trace.get_current_span(), mapping)


def record_change(operation, via):
    """Count one app change. Call only after the write succeeded.

    Unrecognised values are dropped rather than guessed at.
    """
    changes.add(
        1,
        _without_none(
            {
                reg.CHANGE_OPERATION: clamp(
                    operation, reg.CHANGE_OPERATION.values, None
                ),
                reg.CHANGE_VIA: clamp(via, reg.CHANGE_VIA.values, None),
            }
        ),
    )


def record_debug_run(outcome, run_seconds=None, script_seconds=None):
    """Count one terminal ``app_debug`` run and record its durations.

    Durations that are ``None`` or negative are skipped. An unrecognised
    outcome is dropped rather than guessed at.
    """
    attributes = _without_none(
        {reg.DEBUG_OUTCOME: clamp(outcome, reg.DEBUG_OUTCOME.values, None)}
    )
    debug_runs.add(1, attributes)
    if run_seconds is not None and run_seconds >= 0:
        debug_run_duration.record(run_seconds, attributes)
    if script_seconds is not None and script_seconds >= 0:
        debug_script_duration.record(script_seconds, attributes)


def record_debug_job_expired():
    "Count one pending debug job flipped to expired."
    debug_jobs_expired.add(1)


def trace_headers():
    """W3C trace context headers for the current span, for ``datasette.client``.

    Core's ``TelemetryMiddleware`` starts every request's SERVER span from
    ``extract(headers)``, which falls back to an empty context rather than
    the current one, so without these headers an internal
    ``datasette.client`` request lands in a separate trace instead of
    nesting under the span that made it. Workaround for
    https://github.com/simonw/datasette/issues/2969 - remove once the
    minimum Datasette version propagates context itself. Uses the global
    propagator: with ``OTEL_PROPAGATORS=none`` it injects nothing, but core
    then extracts nothing either (the empty composite propagator returns no
    context), so the internal request still nests under the current span.
    With no provider installed it returns ``{}``.
    """
    headers = {}
    inject(headers)
    return headers


class QueryRecorder:
    """What the body of ``query_span`` learns about one app query.

    ``kind`` (``sql``/``stored``), ``write`` (bool, stored queries only),
    ``database`` (set only after the allowlist check passed), ``outcome``
    (see ``apps.query.outcome``) and ``rows`` (``ok`` read queries only).
    """

    __slots__ = ("kind", "write", "database", "outcome", "rows")

    def __init__(self):
        self.kind = None
        self.write = None
        self.database = None
        self.outcome = None
        self.rows = None


@contextmanager
def query_span(source, app_id, job_id=None):
    """Wrap one app query in an ``apps.query`` span and record its metrics.

    Yields a ``QueryRecorder`` for the body to fill in. On exit the outcome
    is clamped to the registry enum (``error`` when an exception escaped or
    no valid outcome was set), and only ``error`` sets span status
    ``ERROR``. Exceptions are never recorded on the span - their messages
    can quote SQL or data - only their class name as ``error.type``.
    ``apps.queries`` and ``apps.query.duration`` are recorded whether or not
    the span is recording (instruments are no-ops without a provider).
    """
    source = clamp(source, reg.QUERY_SOURCE.values, None)
    recorder = QueryRecorder()
    started = time.perf_counter()
    escaped = None
    with tracer.start_as_current_span(
        reg.QUERY, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield recorder
        except BaseException as exc:
            escaped = exc
            raise
        finally:
            duration = time.perf_counter() - started
            if escaped is not None:
                outcome = "error"
            else:
                outcome = clamp(recorder.outcome, reg.QUERY_OUTCOME.values, "error")
            kind = clamp(recorder.kind, reg.QUERY_KIND.values, None)
            write = recorder.write if isinstance(recorder.write, bool) else None
            if span.is_recording():
                rows = recorder.rows if isinstance(recorder.rows, int) else None
                set_attributes(
                    span,
                    {
                        reg.APP_ID: app_id,
                        reg.QUERY_SOURCE: source,
                        reg.QUERY_OUTCOME: outcome,
                        reg.QUERY_KIND: kind,
                        reg.QUERY_WRITE: write,
                        reg.DB_NAMESPACE: recorder.database,
                        reg.QUERY_ROWS: rows,
                        reg.DEBUG_JOB_ID: job_id,
                        reg.ERROR_TYPE: (
                            error_type(escaped) if escaped is not None else None
                        ),
                    },
                )
                if outcome == "error":
                    span.set_status(Status(StatusCode.ERROR))
            base = {reg.QUERY_SOURCE: source, reg.QUERY_OUTCOME: outcome}
            queries.add(
                1, _without_none({**base, reg.QUERY_KIND: kind, reg.QUERY_WRITE: write})
            )
            query_duration.record(
                duration, _without_none({**base, reg.QUERY_KIND: kind})
            )
