from __future__ import annotations

import html
import re


def _csp_meta(csp):
    return (
        '<meta http-equiv="Content-Security-Policy" '
        f'content="{html.escape(csp, quote=True)}">'
    )


def iframe_bridge_script():
    return """<script>
(function() {
  var nextId = 1;
  var pending = new Map();

  window.addEventListener("message", function(event) {
    var message = event.data || {};
    if (message.type !== "datasette-app-response" || !pending.has(message.id)) {
      return;
    }
    var callbacks = pending.get(message.id);
    pending.delete(message.id);
    if (message.ok) {
      callbacks.resolve(message.result);
    } else {
      callbacks.reject(new Error(message.error || "Capability request failed"));
    }
  });

  window.datasette = {
    request: function(capability, input) {
      var id = nextId++;
      return new Promise(function(resolve, reject) {
        pending.set(id, {resolve: resolve, reject: reject});
        parent.postMessage({
          type: "datasette-app-request",
          id: id,
          capability: capability,
          input: input || {}
        }, "*");
      });
    },
    query: function(database, sql, params) {
      return this.request("datasette.query", {
        database: database,
        sql: sql,
        params: params || {}
      });
    }
  };
})();
</script>"""


def parent_bridge_script(app_id, iframe_id="datasette-app-frame"):
    endpoint_base = f"/-/apps/{app_id}/capabilities/"
    return f"""<script>
(function() {{
  var iframe = document.getElementById("{html.escape(iframe_id, quote=True)}");
  window.addEventListener("message", async function(event) {{
    if (!iframe || event.source !== iframe.contentWindow) {{
      return;
    }}
    var message = event.data || {{}};
    if (message.type !== "datasette-app-request") {{
      return;
    }}
    var reply = {{
      type: "datasette-app-response",
      id: message.id,
      ok: false,
      error: "Capability request failed"
    }};
    try {{
      var response = await fetch("{html.escape(endpoint_base, quote=True)}" + encodeURIComponent(message.capability), {{
        method: "POST",
        headers: {{"content-type": "application/json"}},
        credentials: "same-origin",
        body: JSON.stringify(message.input || {{}})
      }});
      var json = await response.json();
      reply.ok = !!json.ok;
      reply.result = json.result;
      reply.error = json.error;
    }} catch (error) {{
      reply.error = String(error);
    }}
    event.source.postMessage(reply, "*");
  }});
}})();
</script>"""


def build_app_srcdoc(source, csp, bridge_script=""):
    source = source or ""
    security_head = _csp_meta(csp) + (bridge_script or "")
    head_match = re.search(r"<head\b[^>]*>", source, flags=re.IGNORECASE)
    if head_match:
        return source[: head_match.end()] + security_head + source[head_match.end() :]

    html_match = re.search(r"<html\b[^>]*>", source, flags=re.IGNORECASE)
    if html_match:
        return (
            source[: html_match.end()]
            + f"<head>{security_head}</head>"
            + source[html_match.end() :]
        )

    doctype_match = re.match(r"\s*<!doctype html\s*>", source, flags=re.IGNORECASE)
    if doctype_match:
        return (
            source[: doctype_match.end()]
            + f"<html><head>{security_head}</head><body>"
            + source[doctype_match.end() :]
            + "</body></html>"
        )

    return f"<html><head>{security_head}</head><body>{source}</body></html>"
