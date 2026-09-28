import pytest

pytest.importorskip("opentelemetry.sdk")

from datasette.telemetry_testing import assert_package_never_imports_sdk


def test_package_never_imports_the_sdk():
    assert_package_never_imports_sdk(
        "datasette_apps",
        "datasette_apps.telemetry",
        "datasette_apps.telemetry_registry",
    )
