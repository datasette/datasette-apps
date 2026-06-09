from __future__ import annotations

import html
import json
import re


def _csp_meta(csp):
    return (
        '<meta http-equiv="Content-Security-Policy" '
        f'content="{html.escape(csp, quote=True)}">'
    )


def iframe_bridge_script(channel_token=None):
    if channel_token is None:
        channel_token = "datasette-apps-test-channel"
    script = """<script id="datasette-apps-bridge">
(function() {
  var nextId = 1;
  var pending = new Map();
  var channelToken = __CHANNEL_TOKEN__;
  var bridgePort = null;

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

  function postToParent(message) {
    if (!bridgePort) {
      return;
    }
    try {
      bridgePort.postMessage(message);
    } catch (ignore) {
    }
  }

  function postAppError(kind, details) {
    details = details || {};
    details.kind = kind;
    details.timestamp = new Date().toISOString();
    postToParent({
      type: "datasette-app-error",
      error: details
    });
  }

  function postAppLog(kind, details) {
    details = details || {};
    details.kind = kind;
    details.timestamp = new Date().toISOString();
    postToParent({
      type: "datasette-app-log",
      log: details
    });
  }

  function describeDatasetteCall(method, input) {
    var parts = [];
    if (input.database !== undefined) {
      parts.push(valueToString(input.database));
    }
    if (input.sql !== undefined) {
      parts.push(valueToString(input.sql));
    }
    if (input.query !== undefined) {
      parts.push(valueToString(input.query));
    }
    if (input.params && Object.keys(input.params).length) {
      parts.push(valueToString(input.params));
    }
    return "datasette." + method + "(" + parts.join(", ") + ")";
  }

  function externalLinkUrl(anchor) {
    if (!anchor || !anchor.href || anchor.hasAttribute("download")) {
      return "";
    }
    try {
      var url = new URL(anchor.href);
      if (url.protocol !== "http:" && url.protocol !== "https:") {
        return "";
      }
      return url.href;
    } catch (ignore) {
      return "";
    }
  }

  function closestAnchor(element) {
    while (element && element !== document) {
      if (
        element.tagName &&
        element.tagName.toLowerCase() === "a" &&
        element.hasAttribute("href")
      ) {
        return element;
      }
      element = element.parentNode;
    }
    return null;
  }

  window.addEventListener("click", function(event) {
    if (
      !event.isTrusted ||
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    var anchor = closestAnchor(event.target);
    var url = externalLinkUrl(anchor);
    if (!url) {
      return;
    }
    event.preventDefault();
    postToParent({
      type: "datasette-app-open-link",
      url: url
    });
  }, true);

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

  if (window.console && typeof window.console.log === "function") {
    var originalConsoleLog = window.console.log;
    window.console.log = function() {
      var parts = Array.prototype.slice.call(arguments).map(valueToString);
      postAppLog("console-log", {
        message: parts.join(" "),
        arguments: parts
      });
      return originalConsoleLog.apply(window.console, arguments);
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

  function handleBridgeMessage(event) {
    var message = event.data || {};
    if (message.type !== "datasette-app-response" || !pending.has(message.id)) {
      return;
    }
    var callbacks = pending.get(message.id);
    pending.delete(message.id);
    if (message.ok) {
      callbacks.resolve(message.result);
    } else {
      var errorMessage = message.error || "Query request failed";
      postAppError(callbacks.errorKind || "datasette-query-error", {message: errorMessage});
      callbacks.reject(new Error(errorMessage));
    }
  }

  try {
    var bridgeChannel = new MessageChannel();
    bridgePort = bridgeChannel.port1;
    bridgePort.onmessage = handleBridgeMessage;
    if (typeof bridgePort.start === "function") {
      bridgePort.start();
    }
    parent.postMessage({
      type: "datasette-app-channel-ready",
      token: channelToken
    }, "*", [bridgeChannel.port2]);
  } catch (ignore) {
    bridgePort = null;
  }

  function requestDatasette(method, messageType, input, errorKind) {
    var id = nextId++;
    input.params = input.params || {};
    postAppLog("datasette-call", {
      message: describeDatasetteCall(method, input),
      method: method,
      database: valueToString(input.database),
      sql: input.sql === undefined ? "" : valueToString(input.sql),
      query: input.query === undefined ? "" : valueToString(input.query),
      params: valueToString(input.params)
    });
    return new Promise(function(resolve, reject) {
      pending.set(id, {
        resolve: resolve,
        reject: reject,
        errorKind: errorKind
      });
      postToParent({
        type: messageType,
        id: id,
        input: input
      });
    });
  }

  var datasetteApi = {
    query: function(database, sql, params) {
      return requestDatasette(
        "query",
        "datasette-app-query",
        {database: database, sql: sql, params: params},
        "datasette-query-error"
      );
    },
    storedQuery: function(database, query, params) {
      return requestDatasette(
        "storedQuery",
        "datasette-app-stored-query",
        {database: database, query: query, params: params},
        "datasette-stored-query-error"
      );
    }
  };
  window.datasette = datasetteApi;

  try {
    // Cosmetic only: drop this <script> node now that the IIFE has run so the
    // app's own DOM stays tidy. This is NOT an isolation boundary -- the click,
    // message, error and fetch listeners plus the window.datasette API live on
    // in closures and stay fully reachable by the app after the node is removed.
    var bridgeScript = document.getElementById("datasette-apps-bridge");
    if (bridgeScript && bridgeScript.parentNode) {
      bridgeScript.parentNode.removeChild(bridgeScript);
    }
  } catch (ignore) {
  }
})();
</script>"""
    return script.replace("__CHANNEL_TOKEN__", _json_script_string(channel_token))


def parent_bridge_script(app_id, iframe_id="datasette-app-frame", channel_token=None):
    if channel_token is None:
        channel_token = "datasette-apps-test-channel"
    query_endpoint = f"/-/apps/{app_id}/query"
    script = """<script>
(function() {
  var channelToken = __CHANNEL_TOKEN__;
  var bridgePort = null;
  var channelEstablished = false;
  var errors = [];
  var errorPanel = null;
  var errorCount = null;
  var errorList = null;
  var logs = [];
  var logPanel = null;
  var logCount = null;
  var logList = null;
  var linkModal = null;
  var linkDialog = null;
  var linkUrl = null;
  var linkCancelButton = null;
  var linkOpenButton = null;
  var pendingLinkUrl = "";
  var previousFocus = null;

  function getIframe() {
    return document.getElementById(__IFRAME_ID__);
  }

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

    var iframe = getIframe();
    if (iframe && iframe.parentNode) {
      iframe.parentNode.insertBefore(errorPanel, iframe);
    }
  }

  function ensureLogPanel() {
    if (logPanel) {
      return;
    }
    logPanel = document.createElement("details");
    logPanel.className = "datasette-app-log-panel";
    logPanel.hidden = true;

    var summary = document.createElement("summary");
    logCount = appendText(summary, "span", "datasette-app-log-count", "0 log entries");
    logPanel.appendChild(summary);

    logList = document.createElement("ol");
    logList.className = "datasette-app-log-list";
    logPanel.appendChild(logList);

    var iframe = getIframe();
    if (iframe && iframe.parentNode) {
      iframe.parentNode.insertBefore(logPanel, iframe.nextSibling);
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

  function logDetailsText(log) {
    var parts = [];
    if (log.method) {
      parts.push("Method: " + log.method);
    }
    if (log.database) {
      parts.push("Database: " + log.database);
    }
    if (log.sql) {
      parts.push("SQL: " + log.sql);
    }
    if (log.query) {
      parts.push("Query: " + log.query);
    }
    if (log.params && log.params !== "{}") {
      parts.push("Params: " + log.params);
    }
    if (log.arguments && log.arguments.length) {
      parts.push("Arguments: " + log.arguments.join("\\n"));
    }
    return parts.join("\\n");
  }

  function renderLogs() {
    ensureLogPanel();
    logPanel.hidden = logs.length === 0;
    logCount.textContent = logs.length + (
      logs.length === 1 ? " log entry" : " log entries"
    );
    logList.textContent = "";
    logs.slice().reverse().forEach(function(log) {
      var item = document.createElement("li");
      appendText(item, "strong", "datasette-app-log-kind", log.kind || "log");
      appendText(item, "div", "datasette-app-log-message", log.message || "");
      if (log.timestamp) {
        appendText(item, "time", "datasette-app-log-time", log.timestamp);
      }
      var details = logDetailsText(log);
      if (details) {
        appendText(item, "pre", "datasette-app-log-details", details);
      }
      logList.appendChild(item);
    });
  }

  function addAppError(error) {
    errors.push(error || {});
    errors = errors.slice(-50);
    renderErrors();
  }

  function addAppLog(log) {
    logs.push(log || {});
    logs = logs.slice(-100);
    renderLogs();
  }

  function normalizedExternalUrl(value) {
    if (typeof value !== "string") {
      return "";
    }
    value = value.trim();
    if (!/^https?:\\/\\//i.test(value)) {
      return "";
    }
    try {
      var url = new URL(value);
      if (url.protocol !== "http:" && url.protocol !== "https:") {
        return "";
      }
      return url.href;
    } catch (ignore) {
      return "";
    }
  }

  function hideLinkModal() {
    if (!linkModal) {
      return;
    }
    linkModal.hidden = true;
    pendingLinkUrl = "";
    if (previousFocus && typeof previousFocus.focus === "function") {
      try {
        previousFocus.focus();
      } catch (ignore) {
      }
    }
    previousFocus = null;
  }

  function ensureLinkModal() {
    if (linkModal) {
      return;
    }
    linkModal = document.createElement("div");
    linkModal.className = "datasette-app-link-modal";
    linkModal.hidden = true;

    linkDialog = document.createElement("div");
    linkDialog.className = "datasette-app-link-dialog";
    linkDialog.setAttribute("role", "dialog");
    linkDialog.setAttribute("aria-modal", "true");
    linkDialog.setAttribute("aria-labelledby", "datasette-app-link-title");
    linkModal.appendChild(linkDialog);

    appendText(linkDialog, "h2", null, "Open external link").id = "datasette-app-link-title";
    appendText(
      linkDialog,
      "p",
      "datasette-app-link-message",
      "You're leaving Datasette to visit an external link:"
    );
    linkUrl = appendText(linkDialog, "div", "datasette-app-link-url", "");

    var actions = document.createElement("div");
    actions.className = "datasette-app-link-actions";
    linkDialog.appendChild(actions);

    linkCancelButton = document.createElement("button");
    linkCancelButton.type = "button";
    linkCancelButton.className = "datasette-app-link-cancel";
    linkCancelButton.textContent = "Cancel";
    actions.appendChild(linkCancelButton);

    linkOpenButton = document.createElement("button");
    linkOpenButton.type = "button";
    linkOpenButton.className = "datasette-app-link-open";
    linkOpenButton.textContent = "Open link";
    actions.appendChild(linkOpenButton);

    linkCancelButton.addEventListener("click", hideLinkModal);
    linkOpenButton.addEventListener("click", function() {
      var url = pendingLinkUrl;
      hideLinkModal();
      if (url) {
        var opened = window.open(url, "_blank", "noopener,noreferrer");
        if (opened) {
          opened.opener = null;
        }
      }
    });
    linkModal.addEventListener("click", function(event) {
      if (event.target === linkModal) {
        hideLinkModal();
      }
    });
    document.addEventListener("keydown", function(event) {
      if (event.key === "Escape" && linkModal && !linkModal.hidden) {
        hideLinkModal();
      }
    });

    document.body.appendChild(linkModal);
  }

  function showLinkModal(url) {
    url = normalizedExternalUrl(url);
    if (!url) {
      return;
    }
    ensureLinkModal();
    previousFocus = document.activeElement;
    pendingLinkUrl = url;
    linkUrl.textContent = url;
    linkModal.hidden = false;
    linkCancelButton.focus();
  }

  async function handleBridgeMessage(event) {
    var message = event.data || {};
    if (message.type === "datasette-app-open-link") {
      showLinkModal(message.url || "");
      return;
    }
    if (message.type === "datasette-app-error") {
      addAppError(message.error || {});
      return;
    }
    if (message.type === "datasette-app-log") {
      addAppLog(message.log || {});
      return;
    }
    if (
      message.type !== "datasette-app-query" &&
      message.type !== "datasette-app-stored-query"
    ) {
      return;
    }
    var reply = {
      type: "datasette-app-response",
      id: message.id,
      ok: false,
      error: "Query request failed"
    };
    try {
      var response = await fetch(__QUERY_ENDPOINT__, {
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
    if (bridgePort) {
      bridgePort.postMessage(reply);
    }
  }

  function acceptBridgePort(event) {
    var iframe = getIframe();
    if (channelEstablished || !iframe || event.source !== iframe.contentWindow) {
      return;
    }
    var message = event.data || {};
    if (
      message.type !== "datasette-app-channel-ready" ||
      message.token !== channelToken ||
      !event.ports ||
      !event.ports[0]
    ) {
      return;
    }
    channelEstablished = true;
    bridgePort = event.ports[0];
    bridgePort.onmessage = handleBridgeMessage;
    if (typeof bridgePort.start === "function") {
      bridgePort.start();
    }
    window.removeEventListener("message", acceptBridgePort);
  }

  window.addEventListener("message", acceptBridgePort);
})();
</script>"""
    return (
        script.replace("__IFRAME_ID__", _json_script_string(iframe_id))
        .replace("__QUERY_ENDPOINT__", _json_script_string(query_endpoint))
        .replace("__CHANNEL_TOKEN__", _json_script_string(channel_token))
    )


def _json_script_string(value):
    return json.dumps(value).replace("</", "<\\/")


def build_app_srcdoc(source, csp, bridge_script=""):
    source = source or ""
    security_head = _csp_meta(csp) + (bridge_script or "")
    doctype_match = re.match(r"\s*<!doctype html\s*>", source, flags=re.IGNORECASE)
    if doctype_match:
        return (
            source[: doctype_match.end()]
            + security_head
            + source[doctype_match.end() :]
        )

    return security_head + source
