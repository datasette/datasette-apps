from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

BASE_DIRECTIVES = [
    "default-src 'none'",
    "script-src 'unsafe-inline'",
    "style-src 'unsafe-inline'",
]

APP_VIEW_PARENT_CSP = "frame-src 'none';"


def _is_localhost(hostname):
    if not hostname:
        return False
    hostname = hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def normalize_connect_origin(origin):
    parsed = urlsplit((origin or "").strip())
    allow_insecure_test_origins = os.environ.get(
        "DATASETTE_APPS_ALLOW_INSECURE_TEST_CSP_ORIGINS"
    )
    if parsed.scheme != "https" and not allow_insecure_test_origins:
        raise ValueError("Only https:// origins are allowed")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http:// and https:// origins are allowed")
    if not parsed.hostname:
        raise ValueError("Origin must include a host")
    if parsed.username or parsed.password:
        raise ValueError("Origin must not include username or password")
    if parsed.query or parsed.fragment:
        raise ValueError("Origin must not include query string or fragment")
    if parsed.path and parsed.path != "/":
        raise ValueError("Origin must not include a path")
    hostname = parsed.hostname.lower()
    if "*" in hostname:
        raise ValueError("Wildcard hosts are not allowed")
    if _is_localhost(hostname) and not allow_insecure_test_origins:
        raise ValueError("Localhost origins are not allowed")

    # Accessing .port validates the port and raises ValueError if malformed.
    port = parsed.port
    if ":" in hostname:
        netloc = f"[{hostname}]"
    else:
        netloc = hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parsed.scheme}://{netloc}"


class CspOriginNotAllowed(ValueError):
    pass


def normalize_allowlist_origin(origin):
    origin = (origin or "").strip()
    if "://" not in origin:
        origin = f"https://{origin}"
    return normalize_connect_origin(origin)


def configured_csp_allowlist(datasette):
    plugin_config = datasette.plugin_config("datasette-apps") or {}
    origins = plugin_config.get("allowed_csp_origins") or []
    return sorted({normalize_allowlist_origin(origin) for origin in origins})


async def resolve_csp_origins(datasette, actor, requested_origins, existing_origins=()):
    from .permissions import AppsResource

    normalized = []
    for origin in requested_origins:
        origin = normalize_connect_origin(origin)
        if origin not in normalized:
            normalized.append(origin)
    if not normalized:
        return normalized
    if await datasette.allowed(
        action="apps-set-csp", resource=AppsResource(), actor=actor
    ):
        return normalized
    permitted = set(configured_csp_allowlist(datasette)) | set(existing_origins)
    disallowed = [origin for origin in normalized if origin not in permitted]
    if disallowed:
        raise CspOriginNotAllowed(
            "Not allowed to set CSP origins: {}. These origins are not on the "
            "configured allow-list; the apps-set-csp permission is required to "
            "set arbitrary origins.".format(", ".join(disallowed))
        )
    return normalized


def build_csp(connect_origins, debug=False):
    """debug=True builds the debug frame's policy, which also allows
    blob: scripts: on engines that withhold error details for inline
    scripts in sandboxed frames (WebKit) the debug bridge re-runs each
    inline script as a same-text blob: script, whose errors keep their
    details. That grants nothing 'unsafe-inline' does not - page code can
    already run any script text it builds - and worker-src 'none' keeps
    workers blocked, as they are in production."""
    origins = [normalize_connect_origin(origin) for origin in connect_origins]
    directives = [*BASE_DIRECTIVES]
    if debug:
        directives = [
            f"{directive} blob:" if directive.startswith("script-src ") else directive
            for directive in directives
        ]
    if origins:
        element_sources = ["'unsafe-inline'", *origins]
        script_element_sources = [*element_sources, *(["blob:"] if debug else [])]
        directives.append(f"script-src-elem {' '.join(script_element_sources)}")
        directives.append(f"style-src-elem {' '.join(element_sources)}")
    directives.append(f"img-src {' '.join(['data:', 'blob:', *origins])}")
    if origins:
        directives.append(f"connect-src {' '.join(origins)}")
    if debug:
        directives.append("worker-src 'none'")
    return "; ".join(directives) + ";"
