# Importing the fixture names registers them: session-scoped autouse
# tracer/meter providers with in-memory export (skipped when the SDK is not
# installed), and a per-test reset. Tests take otel_spans / otel_metrics.
from datasette.telemetry_testing import (  # noqa: F401
    MetricsCollector,
    otel_metrics,
    otel_meter_provider,
    otel_provider,
    otel_reset,
    otel_spans,
)


def pytest_collection_modifyitems(items):
    # assert_package_never_imports_sdk shells out to a fresh interpreter; its
    # docstring documents a macOS/CPython 3.13 crash when subprocess-spawning
    # tests run late in a thread-heavy process, so run it first.
    front_names = {"test_package_never_imports_the_sdk"}
    front = [item for item in items if item.name in front_names]
    rest = [item for item in items if item.name not in front_names]
    items[:] = front + rest
