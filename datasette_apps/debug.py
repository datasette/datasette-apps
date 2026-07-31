"""Debug jobs: run a JavaScript debug script inside a hidden, sandboxed
render of a stored app in the user's own browser.

A job row in _app_debug_jobs is the durable record of one debug run. The
agent tool creates a job and suspends its turn with ask_user(); the
harness HTML (rendered in the chat page) claims the job exactly once,
renders the app in a hidden iframe, executes the debug script through
the bridge, POSTs the result envelope back, and answers the question so
the turn resumes. The table doubles as the audit trail: execution is
invisible by design and runs queries as the user, so every run keeps a
record of exactly what script ran, initiated by whom, against what.
"""

from __future__ import annotations

import html as html_module
import json
import secrets
from datetime import datetime, timezone

from .ids import monotonic_ulid
from .registry import Registry

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
DEFAULT_TIMEOUT_MS = 15000
MIN_TIMEOUT_MS = 1000
MAX_TIMEOUT_MS = 60000
MIN_VIEWPORT_PX = 100
MAX_VIEWPORT_PX = 4000
JOB_EXPIRY_SECONDS = 15 * 60
MAX_EVENT_ERRORS = 50
MAX_EVENT_LOGS = 100
# Cap on the serialized envelope an app_debug tool call returns
RESULT_ENVELOPE_MAX_CHARS = 40000
# When truncating an oversized envelope, keep at most this many errors
TRUNCATED_MAX_ERRORS = 20


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row_to_job(row):
    if row is None:
        return None
    job = dict(row)
    job["config"] = json.loads(job["config"] or "{}")
    job["result"] = json.loads(job["result"]) if job["result"] else None
    return job


def validate_viewport(viewport):
    if viewport is None:
        return dict(DEFAULT_VIEWPORT)
    error = ValueError(
        "viewport must be an object with integer width and height "
        f"between {MIN_VIEWPORT_PX} and {MAX_VIEWPORT_PX}"
    )
    if not isinstance(viewport, dict):
        raise error
    normalized = {}
    for key in ("width", "height"):
        value = viewport.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise error
        if not (MIN_VIEWPORT_PX <= value <= MAX_VIEWPORT_PX):
            raise error
        normalized[key] = value
    return normalized


def clamp_timeout_ms(timeout_ms):
    if timeout_ms is None:
        return DEFAULT_TIMEOUT_MS
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
        raise ValueError("timeout_ms must be a number of milliseconds")
    return max(MIN_TIMEOUT_MS, min(MAX_TIMEOUT_MS, int(timeout_ms)))


async def create_debug_job(
    datasette,
    *,
    actor_id,
    app_id,
    javascript,
    viewport=None,
    timeout_ms=None,
    conversation_id=None,
    call_key=None,
):
    registry = Registry(datasette)
    await registry.ensure_tables()
    app = await registry.get_app(app_id)
    if app is None or app["external"]:
        raise ValueError("App not found")
    version = await registry.get_current_version(app_id)
    if version is None:
        raise ValueError("App has no saved revision to debug")
    if not javascript or not isinstance(javascript, str):
        raise ValueError("javascript is required")
    config = {
        "viewport": validate_viewport(viewport),
        "timeout_ms": clamp_timeout_ms(timeout_ms),
        "channel_token": secrets.token_urlsafe(32),
        "frame_token": secrets.token_urlsafe(32),
    }
    job = {
        "id": monotonic_ulid(),
        "actor_id": actor_id,
        "conversation_id": conversation_id,
        "call_key": call_key,
        "app_id": app_id,
        "version": version["version"],
        "javascript": javascript,
        "config": config,
        "status": "pending",
        "result": None,
        "created_at": _now(),
        "claimed_at": None,
        "completed_at": None,
    }
    await datasette.get_internal_database().execute_write(
        """
        INSERT INTO _app_debug_jobs (
            id, actor_id, conversation_id, call_key, app_id, version,
            javascript, config, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        [
            job["id"],
            actor_id,
            conversation_id,
            call_key,
            app_id,
            job["version"],
            javascript,
            json.dumps(config),
            job["created_at"],
        ],
    )
    return job


async def get_debug_job(datasette, job_id):
    row = (
        await datasette.get_internal_database().execute(
            "SELECT * FROM _app_debug_jobs WHERE id = ?", [job_id]
        )
    ).first()
    return _row_to_job(row)


async def get_debug_job_for_call(datasette, conversation_id, call_key):
    """The most recent job created for one tool call, so re-executing a
    suspended call after the user's browser answers finds its job instead
    of creating an orphan row."""
    if not call_key:
        return None
    row = (
        await datasette.get_internal_database().execute(
            """
            SELECT * FROM _app_debug_jobs
            WHERE conversation_id IS ? AND call_key = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            [conversation_id, call_key],
        )
    ).first()
    return _row_to_job(row)


def debug_job_is_expired(job):
    created_at = datetime.fromisoformat(job["created_at"])
    age = datetime.now(timezone.utc) - created_at
    return age.total_seconds() > JOB_EXPIRY_SECONDS


async def expire_debug_job(datasette, job_id):
    await datasette.get_internal_database().execute_write(
        "UPDATE _app_debug_jobs SET status = 'expired' "
        "WHERE id = ? AND status = 'pending'",
        [job_id],
    )


async def claim_debug_job(datasette, job_id):
    """Atomically claim a pending job. Returns the claimed job, or None
    if the job was already claimed, completed or expired - the one-shot
    gate that makes script-bearing question HTML safe to re-render from
    conversation history."""
    now = _now()

    def claim(conn):
        return conn.execute(
            "UPDATE _app_debug_jobs SET status = 'claimed', claimed_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now, job_id),
        ).rowcount

    updated = await datasette.get_internal_database().execute_write_fn(claim)
    if not updated:
        return None
    return await get_debug_job(datasette, job_id)


def _event_list(value, cap):
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)][:cap]


def normalize_envelope(envelope):
    """Coerce a browser-supplied result envelope into the stored shape."""
    if not isinstance(envelope, dict):
        raise ValueError("Result envelope must be an object")
    events = envelope.get("events")
    if not isinstance(events, dict):
        events = {}
    error = envelope.get("error")
    duration_ms = envelope.get("duration_ms")
    return {
        "ok": bool(envelope.get("ok")),
        "result": envelope.get("result"),
        "error": error if isinstance(error, dict) else None,
        "events": {
            "errors": _event_list(events.get("errors"), MAX_EVENT_ERRORS),
            "logs": _event_list(events.get("logs"), MAX_EVENT_LOGS),
        },
        "duration_ms": (
            int(duration_ms)
            if isinstance(duration_ms, (int, float))
            and not isinstance(duration_ms, bool)
            else None
        ),
        "timed_out": bool(envelope.get("timed_out")),
    }


async def complete_debug_job(datasette, job_id, envelope):
    """Store the result envelope for a claimed job. Returns False if the
    job is not currently claimed (never claimed, or already completed)."""
    result_json = json.dumps(normalize_envelope(envelope))
    now = _now()

    def complete(conn):
        return conn.execute(
            "UPDATE _app_debug_jobs SET status = 'completed', "
            "completed_at = ?, result = ? WHERE id = ? AND status = 'claimed'",
            (now, result_json, job_id),
        ).rowcount

    updated = await datasette.get_internal_database().execute_write_fn(complete)
    return bool(updated)


def cap_envelope(envelope, max_chars=RESULT_ENVELOPE_MAX_CHARS):
    """Size-cap a result envelope with explicit truncation markers,
    preferring to keep events.errors (errors are why you called)."""
    envelope = dict(envelope)
    envelope["events"] = dict(envelope.get("events") or {})
    truncated = []

    def oversized():
        return len(json.dumps(envelope, default=repr)) > max_chars

    if not oversized():
        return envelope
    logs = envelope["events"].get("logs") or []
    if logs:
        envelope["events"]["logs"] = []
        truncated.append(f"events.logs ({len(logs)} entries dropped)")
        envelope["truncated"] = truncated
    if oversized() and envelope.get("result") is not None:
        dropped = len(json.dumps(envelope["result"], default=repr))
        envelope["result"] = f"[truncated - {dropped} characters dropped]"
        truncated.append(f"result ({dropped} characters dropped)")
        envelope["truncated"] = truncated
    errors = envelope["events"].get("errors") or []
    if oversized() and len(errors) > TRUNCATED_MAX_ERRORS:
        envelope["events"]["errors"] = errors[:TRUNCATED_MAX_ERRORS]
        truncated.append(
            f"events.errors (kept first {TRUNCATED_MAX_ERRORS} of {len(errors)})"
        )
        envelope["truncated"] = truncated
    return envelope


_HARNESS_TEMPLATE = """<div class="datasette-app-debug-harness" \
data-debug-job-id="__JOB_ID_HTML__"></div>
<script>
(function() {
  var claimUrl = __CLAIM_URL__;
  var resultUrl = __RESULT_URL__;
  var queryUrl = __QUERY_URL__;
  var currentScript = document.currentScript;
  var finished = false;
  var startedAt = Date.now();
  var events = {errors: [], logs: []};
  var iframe = null;
  var bridgePort = null;
  var deadlineTimer = null;
  var jobInfo = null;

  function collectError(error) {
    if (events.errors.length < 50) {
      events.errors.push(error || {});
    }
  }

  function collectLog(log) {
    if (events.logs.length < 100) {
      events.logs.push(log || {});
    }
  }

  function findQuestionContainer() {
    var element = currentScript;
    while (element && element !== document) {
      if (element.classList && element.classList.contains("agent-question")) {
        return element;
      }
      element = element.parentNode;
    }
    return null;
  }

  function answerQuestion(value) {
    // V1 integration point with datasette-agent: submit the enclosing
    // free-text question form so the suspended turn resumes through the
    // page's own answer flow.
    var container = findQuestionContainer();
    if (!container) {
      return;
    }
    var textarea = container.querySelector(
      ".agent-question-controls textarea, textarea"
    );
    var form = textarea ? textarea.closest("form") : null;
    if (!textarea || !form) {
      return;
    }
    textarea.value = value;
    if (typeof form.requestSubmit === "function") {
      form.requestSubmit();
    } else {
      form.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
    }
  }

  function finish(envelope) {
    if (finished) {
      return;
    }
    finished = true;
    if (deadlineTimer) {
      clearTimeout(deadlineTimer);
    }
    envelope.events = events;
    envelope.duration_ms = Date.now() - startedAt;
    fetch(resultUrl, {
      method: "POST",
      headers: {"content-type": "application/json"},
      credentials: "same-origin",
      body: JSON.stringify(envelope)
    }).catch(function() {
    }).then(function() {
      if (iframe && iframe.parentNode) {
        iframe.parentNode.removeChild(iframe);
      }
      answerQuestion("completed");
    });
  }

  async function handleBridgeMessage(event) {
    var message = event.data || {};
    if (message.type === "datasette-app-error") {
      collectError(message.error);
      return;
    }
    if (message.type === "datasette-app-log") {
      collectLog(message.log);
      return;
    }
    if (message.type === "datasette-app-debug-result") {
      if (message.ok) {
        finish({
          ok: true,
          result: message.result === undefined ? null : message.result,
          timed_out: false
        });
      } else {
        finish({
          ok: false,
          error: message.error || {message: "Debug script failed"},
          timed_out: false
        });
      }
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
      var response = await fetch(queryUrl, {
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
    if (!iframe || event.source !== iframe.contentWindow) {
      return;
    }
    var message = event.data || {};
    if (
      message.type !== "datasette-app-channel-ready" ||
      message.token !== jobInfo.channel_token ||
      !event.ports ||
      !event.ports[0]
    ) {
      return;
    }
    window.removeEventListener("message", acceptBridgePort);
    bridgePort = event.ports[0];
    bridgePort.onmessage = handleBridgeMessage;
    if (typeof bridgePort.start === "function") {
      bridgePort.start();
    }
    bridgePort.postMessage({
      type: "datasette-app-debug-eval",
      id: 1,
      code: jobInfo.javascript
    });
  }

  fetch(claimUrl, {
    method: "POST",
    credentials: "same-origin"
  }).then(function(response) {
    return response.json();
  }).then(function(data) {
    if (!data || !data.ok) {
      // Already executed (e.g. history replay re-rendering this HTML),
      // expired, or not ours: do nothing.
      return;
    }
    jobInfo = data.job;
    window.addEventListener("message", acceptBridgePort);
    iframe = document.createElement("iframe");
    iframe.setAttribute("sandbox", "allow-scripts allow-forms");
    // Hidden but still laid out, so getBoundingClientRect and
    // getComputedStyle keep returning real measurements.
    iframe.style.cssText =
      "position: fixed; left: 0; top: 0; border: 0; " +
      "opacity: 0; pointer-events: none;";
    iframe.style.width = jobInfo.viewport.width + "px";
    iframe.style.height = jobInfo.viewport.height + "px";
    iframe.src = jobInfo.frame_url;
    document.body.appendChild(iframe);
    deadlineTimer = setTimeout(function() {
      finish({
        ok: false,
        error: {
          message: "Debug run timed out after " + jobInfo.timeout_ms + "ms"
        },
        timed_out: true
      });
    }, Math.max(1000, jobInfo.timeout_ms - 500));
  }).catch(function() {
  });
})();
</script>"""


def _json_script_string(value):
    return json.dumps(value).replace("</", "<\\/")


def build_debug_harness_html(datasette, job):
    """The HTML rendered above the suspended ask_user question. Contains
    no secrets and no debug script: those arrive via the claim response,
    and the one-shot claim gate means re-rendering this HTML from
    conversation history never re-runs the job."""
    job_id = job["id"]
    urls = datasette.urls

    def endpoint(suffix):
        return urls.path(f"/-/apps/debug/{job_id}/{suffix}")

    return (
        _HARNESS_TEMPLATE.replace(
            "__JOB_ID_HTML__", html_module.escape(job_id, quote=True)
        )
        .replace("__CLAIM_URL__", _json_script_string(endpoint("claim")))
        .replace("__RESULT_URL__", _json_script_string(endpoint("result")))
        .replace("__QUERY_URL__", _json_script_string(endpoint("query")))
    )
