import pytest
from datasette.app import Datasette

from datasette_apps.csp import (
    APP_VIEW_PARENT_CSP,
    CspOriginNotAllowed,
    build_csp,
    configured_csp_allowlist,
    normalize_allowlist_origin,
    normalize_connect_origin,
    resolve_csp_origins,
)
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


def test_app_view_parent_csp_blocks_frame_navigation():
    assert APP_VIEW_PARENT_CSP == "frame-src 'none';"


def test_build_csp_includes_exact_connect_origins():
    assert build_csp(["https://api.github.com"]) == (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; "
        "script-src-elem 'unsafe-inline' https://api.github.com; "
        "style-src-elem 'unsafe-inline' https://api.github.com; "
        "img-src data: blob: https://api.github.com; "
        "connect-src https://api.github.com;"
    )


def test_build_csp_can_allow_insecure_origins_in_tests(monkeypatch):
    monkeypatch.setenv("DATASETTE_APPS_ALLOW_INSECURE_TEST_CSP_ORIGINS", "1")

    assert build_csp(["http://127.0.0.1:8000"]) == (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; "
        "script-src-elem 'unsafe-inline' http://127.0.0.1:8000; "
        "style-src-elem 'unsafe-inline' http://127.0.0.1:8000; "
        "img-src data: blob: http://127.0.0.1:8000; "
        "connect-src http://127.0.0.1:8000;"
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


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("cdn.jsdelivr.net", "https://cdn.jsdelivr.net"),
        ("https://cdn.jsdelivr.net", "https://cdn.jsdelivr.net"),
        ("https://cdn.jsdelivr.net/", "https://cdn.jsdelivr.net"),
    ],
)
def test_normalize_allowlist_origin_normalizes_bare_domains(origin, expected):
    assert normalize_allowlist_origin(origin) == expected


@pytest.mark.parametrize(
    "origin",
    [
        "https://*.github.com",
        "https://cdn.example.com/path",
        "localhost",
        "",
    ],
)
def test_normalize_allowlist_origin_rejects_invalid_entries(origin):
    with pytest.raises(ValueError):
        normalize_allowlist_origin(origin)


def _datasette_with(plugin_config=None, permissions=None):
    config = {}
    if plugin_config:
        config["plugins"] = {"datasette-apps": plugin_config}
    if permissions:
        config["permissions"] = permissions
    return Datasette(memory=True, config=config)


def test_configured_csp_allowlist_normalizes_dedupes_and_sorts():
    datasette = _datasette_with(
        {
            "allowed_csp_origins": [
                "cdn.jsdelivr.net",
                "https://api.github.com",
                "https://cdn.jsdelivr.net",
            ]
        }
    )
    assert configured_csp_allowlist(datasette) == [
        "https://api.github.com",
        "https://cdn.jsdelivr.net",
    ]


def test_configured_csp_allowlist_defaults_to_empty():
    assert configured_csp_allowlist(Datasette(memory=True)) == []


def test_configured_csp_allowlist_raises_on_invalid_entry():
    datasette = _datasette_with({"allowed_csp_origins": ["https://*.example.com"]})
    with pytest.raises(ValueError):
        configured_csp_allowlist(datasette)


@pytest.mark.asyncio
async def test_resolve_csp_origins_allows_any_origin_with_permission():
    datasette = _datasette_with(permissions={"apps-set-csp": {"id": "admin"}})
    await datasette.invoke_startup()

    assert await resolve_csp_origins(
        datasette, {"id": "admin"}, ["https://attacker.example.com"]
    ) == ["https://attacker.example.com"]


@pytest.mark.asyncio
async def test_resolve_csp_origins_restricts_to_allowlist_without_permission():
    datasette = _datasette_with({"allowed_csp_origins": ["cdn.jsdelivr.net"]})
    await datasette.invoke_startup()

    assert await resolve_csp_origins(
        datasette, {"id": "alice"}, ["https://cdn.jsdelivr.net"]
    ) == ["https://cdn.jsdelivr.net"]

    with pytest.raises(CspOriginNotAllowed) as excinfo:
        await resolve_csp_origins(
            datasette, {"id": "alice"}, ["https://attacker.example.com"]
        )
    assert "https://attacker.example.com" in str(excinfo.value)
    assert "apps-set-csp" in str(excinfo.value)


@pytest.mark.asyncio
async def test_resolve_csp_origins_denies_all_without_permission_or_allowlist():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()

    with pytest.raises(CspOriginNotAllowed):
        await resolve_csp_origins(
            datasette, {"id": "alice"}, ["https://cdn.jsdelivr.net"]
        )


@pytest.mark.asyncio
async def test_resolve_csp_origins_preserves_existing_origins():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()

    assert await resolve_csp_origins(
        datasette,
        {"id": "alice"},
        ["https://api.github.com"],
        existing_origins=["https://api.github.com"],
    ) == ["https://api.github.com"]

    with pytest.raises(CspOriginNotAllowed):
        await resolve_csp_origins(
            datasette,
            {"id": "alice"},
            ["https://api.github.com", "https://attacker.example.com"],
            existing_origins=["https://api.github.com"],
        )


def test_build_app_srcdoc_preserves_doctype_and_inserts_csp_first_in_head():
    srcdoc = build_app_srcdoc(
        "<!DOCTYPE html><html><head><title>Hello</title></head><body></body></html>",
        "default-src 'none';",
    )

    assert srcdoc.startswith(
        '<!DOCTYPE html><meta http-equiv="Content-Security-Policy"'
    )
    assert srcdoc.index("Content-Security-Policy") < srcdoc.index(
        "<title>Hello</title>"
    )


def test_build_app_srcdoc_creates_head_if_missing():
    srcdoc = build_app_srcdoc("<h1>Hello</h1>", "default-src 'none';")

    assert srcdoc.startswith('<meta http-equiv="Content-Security-Policy"')
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

    assert "new MessageChannel()" in script
    assert 'type: "datasette-app-channel-ready"' in script
    assert 'type: "datasette-app-error"' in script
    assert "securitypolicyviolation" in script
    assert "unhandledrejection" in script
    assert "console.error" in script
    assert "console.log" in script
    assert "window.fetch" in script
    assert 'type: "datasette-app-log"' in script


def test_iframe_bridge_reports_viewport_meta_once_after_dom_load():
    script = iframe_bridge_script()

    assert 'type: "datasette-app-viewport"' in script
    assert 'getElementsByTagName("meta")' in script
    assert "DOMContentLoaded" in script
    assert "MutationObserver" not in script


def test_iframe_bridge_intercepts_external_link_clicks():
    script = iframe_bridge_script()

    assert 'id="datasette-apps-bridge"' in script
    assert 'type: "datasette-app-open-link"' in script
    assert "event.preventDefault()" in script
    assert "event.isTrusted" in script
    assert 'rawHref.charAt(0) === "#"' in script


def test_iframe_bridge_noops_history_mutation_methods():
    script = iframe_bridge_script()

    assert "shimHistoryMethod" in script
    assert "Object.defineProperty(window.history, name" in script
    for method in ("replaceState", "pushState", "back", "forward", "go"):
        assert f'"{method}"' in script


def test_parent_bridge_renders_app_error_panel():
    script = parent_bridge_script("app1")

    assert "acceptBridgePort" in script
    assert "event.ports[0]" in script
    assert "datasette-app-error-panel" in script
    assert "datasette-app-error-list" in script
    assert 'message.type === "datasette-app-error"' in script
    assert "errors.slice(-50)" in script


def test_parent_bridge_renders_app_log_panel():
    script = parent_bridge_script("app1")

    assert "datasette-app-log-panel" in script
    assert "datasette-app-log-list" in script
    assert 'message.type === "datasette-app-log"' in script
    assert "logs.slice(-100)" in script
    assert "iframe.nextSibling" in script


def test_parent_bridge_mirrors_viewport_only_when_enabled():
    disabled = parent_bridge_script("app1", mirror_viewport=False)
    enabled = parent_bridge_script("app1", mirror_viewport=True)

    assert "var mirrorViewport = false;" in disabled
    assert "var mirrorViewport = true;" in enabled
    assert 'message.type === "datasette-app-viewport"' in enabled


def test_parent_bridge_renders_external_link_modal():
    script = parent_bridge_script("app1")

    assert "datasette-app-link-modal" in script
    assert "Open external link" in script
    assert 'message.type === "datasette-app-open-link"' in script
    assert 'window.open(url, "_blank", "noopener,noreferrer")' in script
