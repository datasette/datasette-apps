import pytest

from datasette_apps.csp import build_csp, normalize_connect_origin
from datasette_apps.rendering import (
    build_app_srcdoc,
    iframe_bridge_script,
    parent_bridge_script,
)


def test_build_csp_defaults_to_no_connect_src():
    assert build_csp([]) == (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; img-src data: blob:;"
    )


def test_build_csp_includes_exact_connect_origins():
    assert build_csp(["https://api.github.com"]) == (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; "
        "script-src-elem 'unsafe-inline' https://api.github.com; "
        "style-src-elem 'unsafe-inline' https://api.github.com; "
        "img-src data: blob: https://api.github.com; "
        "connect-src https://api.github.com;"
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://api.github.com",
        "https://localhost:8000",
        "https://127.0.0.1:8000",
        "https://[::1]:8000",
        "https://api.github.com/path",
        "https://*.github.com",
    ],
)
def test_normalize_connect_origin_rejects_unsafe_origins(origin):
    with pytest.raises(ValueError):
        normalize_connect_origin(origin)


def test_build_app_srcdoc_preserves_doctype_and_inserts_csp_first_in_head():
    srcdoc = build_app_srcdoc(
        "<!DOCTYPE html><html><head><title>Hello</title></head><body></body></html>",
        "default-src 'none';",
    )

    assert srcdoc.startswith("<!DOCTYPE html><html><head><meta http-equiv=\"Content-Security-Policy\"")
    assert srcdoc.index("Content-Security-Policy") < srcdoc.index("<title>Hello</title>")


def test_build_app_srcdoc_creates_head_if_missing():
    srcdoc = build_app_srcdoc("<h1>Hello</h1>", "default-src 'none';")

    assert srcdoc.startswith("<html><head><meta http-equiv=\"Content-Security-Policy\"")
    assert srcdoc.index("Content-Security-Policy") < srcdoc.index("<h1>Hello</h1>")


def test_build_app_srcdoc_injects_datasette_query_bridge_after_csp():
    srcdoc = build_app_srcdoc(
        "<!DOCTYPE html><html><head><title>Hello</title></head><body></body></html>",
        "default-src 'none';",
        iframe_bridge_script(),
    )

    assert "window.datasette" in srcdoc
    assert srcdoc.index("Content-Security-Policy") < srcdoc.index("window.datasette")
    assert srcdoc.index("window.datasette") < srcdoc.index("<title>Hello</title>")


def test_iframe_bridge_reports_app_errors_to_parent():
    script = iframe_bridge_script()

    assert 'type: "datasette-app-error"' in script
    assert "securitypolicyviolation" in script
    assert "unhandledrejection" in script
    assert "console.error" in script
    assert "window.fetch" in script


def test_parent_bridge_renders_app_error_panel():
    script = parent_bridge_script("app1")

    assert "datasette-app-error-panel" in script
    assert "datasette-app-error-list" in script
    assert 'message.type === "datasette-app-error"' in script
    assert "errors.slice(-50)" in script
