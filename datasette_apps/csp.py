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


def build_csp(connect_origins):
    origins = [normalize_connect_origin(origin) for origin in connect_origins]
    directives = [*BASE_DIRECTIVES]
    if origins:
        element_sources = ["'unsafe-inline'", *origins]
        directives.append(f"script-src-elem {' '.join(element_sources)}")
        directives.append(f"style-src-elem {' '.join(element_sources)}")
    directives.append(f"img-src {' '.join(['data:', 'blob:', *origins])}")
    if origins:
        directives.append(f"connect-src {' '.join(origins)}")
    return "; ".join(directives) + ";"
