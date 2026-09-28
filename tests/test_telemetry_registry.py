"""Static checks on datasette-apps' telemetry registry (no SDK needed)."""

import importlib.util
from pathlib import Path

from datasette_apps.telemetry_registry import (
    APP_ID,
    ATTRIBUTES,
    DB_NAMESPACE,
    DEBUG_JOB_ID,
    HISTOGRAM,
    METRICS,
    REQUEST_SPAN_ATTRIBUTES,
    SPANS,
    TOOL_SPAN_ATTRIBUTES,
)

ROOT = Path(__file__).resolve().parent.parent

# Core's semantic-convention names, deliberately reused rather than shadowed.
CORE_ATTRIBUTES = {"db.namespace", "error.type"}

# --- Wire names pinned as literals ----------------------------------------
#
# A rename is a dashboard-breaking decision to take here, deliberately. If
# one of these fails, either revert the rename or update the literal in the
# same commit.

EXPECTED_SPANS = {"apps.query"}

EXPECTED_METRICS = {
    "apps.queries",
    "apps.query.duration",
    "apps.changes",
    "apps.debug.runs",
    "apps.debug.run.duration",
    "apps.debug.script.duration",
    "apps.debug.jobs.expired",
}

EXPECTED_ATTRIBUTES = {
    "apps.app_id",
    "apps.debug.job_id",
    "apps.query.source",
    "apps.query.kind",
    "apps.query.write",
    "apps.query.outcome",
    "apps.query.rows",
    "apps.change.operation",
    "apps.change.via",
    "apps.tool.result",
    "apps.debug.outcome",
    "db.namespace",
    "error.type",
}


def test_wire_names_are_pinned():
    assert {str(s) for s in SPANS} == EXPECTED_SPANS
    assert {str(m) for m in METRICS} == EXPECTED_METRICS
    assert {str(a) for a in ATTRIBUTES} == EXPECTED_ATTRIBUTES


def test_registry_has_no_duplicate_names():
    for group in (SPANS, METRICS, ATTRIBUTES):
        names = [str(entry) for entry in group]
        assert len(names) == len(set(names)), f"duplicates in {names}"


def test_registry_entries_are_documented():
    for entry in (*SPANS, *METRICS, *ATTRIBUTES):
        assert entry.description and entry.description.strip(), entry


def test_every_name_is_prefixed():
    for entry in (*SPANS, *METRICS):
        assert str(entry).startswith("apps."), entry
    for attribute in ATTRIBUTES:
        name = str(attribute)
        assert name.startswith("apps.") or name in CORE_ATTRIBUTES, attribute


def test_span_and_metric_attributes_are_registered():
    registered = set(ATTRIBUTES)
    for entry in (*SPANS, *METRICS):
        for attribute in entry.attributes:
            assert attribute in registered, f"{entry!r} uses unlisted {attribute!r}"


def test_metric_dimensions_are_bounded():
    # Per-app, per-job and per-database dimensions ride on spans only.
    unbounded = {str(APP_ID), str(DEBUG_JOB_ID), str(DB_NAMESPACE)}
    for metric in METRICS:
        for attribute in metric.attributes:
            assert str(attribute) not in unbounded, f"{metric}: {attribute}"
            assert (
                attribute.values is not None
            ), f"{metric}: {attribute} declares no values= enum"


def test_only_histograms_have_increasing_buckets():
    for metric in METRICS:
        if metric.kind == HISTOGRAM:
            assert metric.buckets, f"{metric!r} declares no buckets"
            assert all(
                a < b for a, b in zip(metric.buckets, metric.buckets[1:])
            ), f"{metric!r} buckets are not strictly increasing"
        else:
            assert metric.buckets is None, f"{metric!r} is not a histogram"


def test_every_attribute_is_used():
    # Declared-but-dead attributes are documentation for a signal that does
    # not exist. Attributes set only on spans this plugin does not own
    # (core's request spans, the agent tool span) count via their tuples.
    used = set(REQUEST_SPAN_ATTRIBUTES) | set(TOOL_SPAN_ATTRIBUTES)
    for entry in (*SPANS, *METRICS):
        used.update(entry.attributes)
    unused = [str(attribute) for attribute in ATTRIBUTES if attribute not in used]
    assert not unused, f"registered but never used: {unused}"
    registered = set(ATTRIBUTES)
    for attribute in (*REQUEST_SPAN_ATTRIBUTES, *TOOL_SPAN_ATTRIBUTES):
        assert attribute in registered, attribute


def test_generated_docs_are_fresh():
    # In-process rather than a subprocess: the same renderer as
    # `uv run scripts/telemetry-doc.py --check`.
    spec = importlib.util.spec_from_file_location(
        "telemetry_doc", ROOT / "scripts" / "telemetry-doc.py"
    )
    telemetry_doc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(telemetry_doc)
    readme = (ROOT / "README.md").read_text()
    assert telemetry_doc.rendered_readme(readme) == readme, (
        "README.md telemetry reference is stale; run "
        "`uv run scripts/telemetry-doc.py`"
    )
