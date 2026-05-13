from __future__ import annotations

import html
import json
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

  function noopHistoryMethod() {
  }

  function shimHistoryMethod(name) {
    try {
      Object.defineProperty(window.history, name, {
        value: noopHistoryMethod,
        configurable: true,
        writable: true
      });
    } catch (ignore) {
      try {
        window.history[name] = noopHistoryMethod;
      } catch (ignoreAssignment) {
      }
    }

    try {
      if (window.History && window.History.prototype) {
        Object.defineProperty(window.History.prototype, name, {
          value: noopHistoryMethod,
          configurable: true,
          writable: true
        });
      }
    } catch (ignorePrototype) {
    }
  }

  ["replaceState", "pushState", "back", "forward", "go"].forEach(shimHistoryMethod);

  function valueToString(value) {
    if (value === null || value === undefined) {
      return "";
    }
    if (typeof value === "string") {
      return value;
    }
    if (value && value.message) {
      return String(value.message);
    }
    try {
      return JSON.stringify(value);
    } catch (ignore) {
      return String(value);
    }
  }

  function normalizeError(error) {
    var details = {message: valueToString(error)};
    if (error && typeof error === "object") {
      if (error.name) {
        details.name = String(error.name);
      }
      if (error.stack) {
        details.stack = String(error.stack);
      }
    }
    return details;
  }

  function postAppError(kind, details) {
    details = details || {};
    details.kind = kind;
    details.timestamp = new Date().toISOString();
    try {
      parent.postMessage({
        type: "datasette-app-error",
        error: details
      }, "*");
    } catch (ignore) {
    }
  }

  window.addEventListener("error", function(event) {
    if (event.target && event.target !== window && event.target !== document) {
      var target = event.target;
      var tagName = target.tagName ? target.tagName.toLowerCase() : "resource";
      var url = target.currentSrc || target.src || target.href || "";
      postAppError(tagName === "img" ? "image-error" : "resource-error", {
        message: "Failed to load " + tagName + (url ? ": " + url : ""),
        tagName: tagName,
        url: url
      });
      return;
    }

    var details = normalizeError(event.error || event.message);
    if (event.message && !details.message) {
      details.message = String(event.message);
    }
    details.filename = event.filename || "";
    details.lineno = event.lineno || 0;
    details.colno = event.colno || 0;
    postAppError("javascript-error", details);
  }, true);

  window.addEventListener("unhandledrejection", function(event) {
    postAppError("unhandled-rejection", normalizeError(event.reason));
  });

  window.addEventListener("securitypolicyviolation", function(event) {
    postAppError("csp-violation", {
      message: event.violatedDirective
        ? "Blocked by Content Security Policy: " + event.violatedDirective
        : "Blocked by Content Security Policy",
      blockedURI: event.blockedURI || "",
      violatedDirective: event.violatedDirective || "",
      effectiveDirective: event.effectiveDirective || "",
      originalPolicy: event.originalPolicy || "",
      sourceFile: event.sourceFile || "",
      lineno: event.lineNumber || 0,
      colno: event.columnNumber || 0
    });
  });

  if (window.console && typeof window.console.error === "function") {
    var originalConsoleError = window.console.error;
    window.console.error = function() {
      var parts = Array.prototype.slice.call(arguments).map(valueToString);
      postAppError("console-error", {
        message: parts.join(" ")
      });
      return originalConsoleError.apply(window.console, arguments);
    };
  }

  if (typeof window.fetch === "function") {
    var originalFetch = window.fetch.bind(window);
    window.fetch = function(input, init) {
      var url = "";
      try {
        url = typeof input === "string" ? input : (input && input.url) || "";
      } catch (ignore) {
      }

      return originalFetch(input, init).then(function(response) {
        if (!response.ok) {
          postAppError("fetch-http-error", {
            message: "fetch() returned HTTP " + response.status + (
              response.url || url ? " for " + (response.url || url) : ""
            ),
            status: response.status,
            statusText: response.statusText || "",
            url: response.url || url
          });
        }
        return response;
      }).catch(function(error) {
        var details = normalizeError(error);
        details.url = url;
        postAppError("fetch-error", details);
        throw error;
      });
    };
  }

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
      var errorMessage = message.error || "Capability request failed";
      postAppError("datasette-request-error", {
        message: errorMessage,
        capability: callbacks.capability || ""
      });
      callbacks.reject(new Error(errorMessage));
    }
  });

  window.datasette = {
    request: function(capability, input) {
      var id = nextId++;
      return new Promise(function(resolve, reject) {
        pending.set(id, {resolve: resolve, reject: reject, capability: capability});
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
    script = """<script>
(function() {
  var iframe = document.getElementById(__IFRAME_ID__);
  var errors = [];
  var errorPanel = null;
  var errorCount = null;
  var errorList = null;

  function appendText(parent, tagName, className, text) {
    var element = document.createElement(tagName);
    if (className) {
      element.className = className;
    }
    element.textContent = text || "";
    parent.appendChild(element);
    return element;
  }

  function ensureErrorPanel() {
    if (errorPanel) {
      return;
    }
    errorPanel = document.createElement("details");
    errorPanel.className = "datasette-app-error-panel";
    errorPanel.hidden = true;

    var summary = document.createElement("summary");
    errorCount = appendText(summary, "span", "datasette-app-error-count", "0 errors");
    errorPanel.appendChild(summary);

    errorList = document.createElement("ol");
    errorList.className = "datasette-app-error-list";
    errorPanel.appendChild(errorList);

    if (iframe && iframe.parentNode) {
      iframe.parentNode.insertBefore(errorPanel, iframe);
    }
  }

  function errorDetailsText(error) {
    var parts = [];
    if (error.filename) {
      parts.push(error.filename + (error.lineno ? ":" + error.lineno : "") + (
        error.colno ? ":" + error.colno : ""
      ));
    }
    if (error.sourceFile) {
      parts.push("Source: " + error.sourceFile + (
        error.lineno ? ":" + error.lineno : ""
      ) + (error.colno ? ":" + error.colno : ""));
    }
    if (error.url) {
      parts.push("URL: " + error.url);
    }
    if (error.status) {
      parts.push("Status: " + error.status + (error.statusText ? " " + error.statusText : ""));
    }
    if (error.blockedURI) {
      parts.push("Blocked URI: " + error.blockedURI);
    }
    if (error.violatedDirective) {
      parts.push("Directive: " + error.violatedDirective);
    }
    if (error.effectiveDirective) {
      parts.push("Effective directive: " + error.effectiveDirective);
    }
    if (error.capability) {
      parts.push("Capability: " + error.capability);
    }
    if (error.stack) {
      parts.push(error.stack);
    }
    return parts.join("\\n");
  }

  function renderErrors() {
    ensureErrorPanel();
    errorPanel.hidden = errors.length === 0;
    errorCount.textContent = errors.length + (errors.length === 1 ? " error" : " errors");
    errorList.textContent = "";
    errors.slice().reverse().forEach(function(error) {
      var item = document.createElement("li");
      appendText(item, "strong", "datasette-app-error-kind", error.kind || "error");
      appendText(item, "div", "datasette-app-error-message", error.message || "Unknown error");
      if (error.timestamp) {
        appendText(item, "time", "datasette-app-error-time", error.timestamp);
      }
      var details = errorDetailsText(error);
      if (details) {
        appendText(item, "pre", "datasette-app-error-details", details);
      }
      errorList.appendChild(item);
    });
  }

  function addAppError(error) {
    errors.push(error || {});
    errors = errors.slice(-50);
    renderErrors();
  }

  window.addEventListener("message", async function(event) {
    if (!iframe || event.source !== iframe.contentWindow) {
      return;
    }
    var message = event.data || {};
    if (message.type === "datasette-app-error") {
      addAppError(message.error || {});
      return;
    }
    if (message.type !== "datasette-app-request") {
      return;
    }
    var reply = {
      type: "datasette-app-response",
      id: message.id,
      ok: false,
      error: "Capability request failed"
    };
    try {
      var response = await fetch(__ENDPOINT_BASE__ + encodeURIComponent(message.capability), {
        method: "POST",
        headers: {"content-type": "application/json"},
        credentials: "same-origin",
        body: JSON.stringify(message.input || {})
      });
      var json = await response.json();
      reply.ok = !!json.ok;
      reply.result = json.result;
      reply.error = json.error;
    } catch (error) {
      reply.error = String(error);
    }
    event.source.postMessage(reply, "*");
  });
})();
</script>"""
    return script.replace("__IFRAME_ID__", _json_script_string(iframe_id)).replace(
        "__ENDPOINT_BASE__", _json_script_string(endpoint_base)
    )


def _json_script_string(value):
    return json.dumps(value).replace("</", "<\\/")


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
