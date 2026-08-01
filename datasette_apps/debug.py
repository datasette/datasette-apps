"""Debug jobs: run a JavaScript debug script inside a hidden, sandboxed
render of a stored app in the user's own browser.

Execution rides on datasette-agent's browser-task primitive: the
app_debug tool creates a job row, then suspends its turn with
context.browser_task(), passing a generic harness bootstrap as the task
HTML and everything run-specific (frame URL, tokens, the debug script)
as the task payload - which the runtime hands out exactly once through
its atomic claim. The harness renders the app in a hidden iframe,
executes the debug script through the bridge, and posts the result
envelope back with window.datasetteAgent.completeTask(); the resumed
tool call receives that envelope as browser_task()'s return value and
records it here.

A job row in _app_debug_jobs is the durable audit record of one debug
run - execution is invisible by design and runs queries as the user, so
every run keeps a record of exactly what script ran, initiated by whom,
against what. The row also gates the /frame and /query endpoints:
both stop serving once the job has its result or its window expires.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

from .ids import monotonic_ulid
from .registry import Registry

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
DEFAULT_TIMEOUT_MS = 15000
MIN_TIMEOUT_MS = 1000
MAX_TIMEOUT_MS = 60000
MIN_VIEWPORT_PX = 100
MAX_VIEWPORT_PX = 4000
# How long the /frame and /query endpoints keep serving a job that
# never received its result
JOB_EXPIRY_SECONDS = 15 * 60
MAX_EVENT_ERRORS = 50
MAX_EVENT_LOGS = 100
# Cap on the serialized envelope an app_debug tool call returns
RESULT_ENVELOPE_MAX_CHARS = 40000
# When truncating an oversized envelope, keep at most this many errors
TRUNCATED_MAX_ERRORS = 20
# Slack added to the browser task's deadline beyond the job's own
# timeout, so the harness can post a timed_out envelope itself rather
# than letting the runtime expire the task
BROWSER_TASK_SLACK_MS = 2000


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
    suspended call after its browser task finishes finds its job instead
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


def debug_task_payload(datasette, job):
    """The browser-task payload for a job: everything run-specific the
    harness needs, delivered exactly once through the runtime's claim.
    Per-run secrets live here, never in the task HTML."""
    config = job["config"]
    frame_url = datasette.urls.path(
        f"/-/apps/debug/{job['id']}/frame?"
        + urlencode({"token": config["frame_token"]})
    )
    return {
        "frame_url": frame_url,
        "query_url": datasette.urls.path(f"/-/apps/debug/{job['id']}/query"),
        "channel_token": config["channel_token"],
        "javascript": job["javascript"],
        "viewport": config["viewport"],
        "timeout_ms": config["timeout_ms"],
    }


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
    """Record the run's result envelope on the audit row: pending ->
    completed, first write wins. Called by the resumed tool call with
    the envelope browser_task() returned. Returns False if the job
    already has a result or expired."""
    result_json = json.dumps(normalize_envelope(envelope))
    now = _now()

    def complete(conn):
        return conn.execute(
            "UPDATE _app_debug_jobs SET status = 'completed', "
            "completed_at = ?, result = ? WHERE id = ? AND status = 'pending'",
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


# The task HTML is a generic bootstrap: it discovers its task id from
# the runtime's rendered task element, claims the task through
# window.datasetteAgent (the one-shot claim is the only source of the
# payload), runs the debug script in a hidden iframe, and posts the
# envelope back with completeTask(). Nothing job-specific and no
# secrets live here, so the persisted HTML is inert by construction -
# and the runtime never re-renders task HTML after the task leaves
# pending anyway.
_HARNESS_HTML = """<div class="datasette-app-debug-harness"></div>
<script>
(function() {
  var currentScript = document.currentScript;

  function findTaskId() {
    // Sanctioned contract: the runtime renders task HTML inside a
    // container carrying data-task-id.
    var container = currentScript
      ? currentScript.closest("[data-task-id]")
      : null;
    return container ? container.dataset.taskId : null;
  }

  var agent = window.datasetteAgent;
  var taskId = findTaskId();
  if (!agent || !taskId) {
    return;
  }

  var finished = false;
  var startedAt = Date.now();
  var events = {errors: [], logs: []};
  var iframe = null;
  var bridgePort = null;
  var deadlineTimer = null;
  var payload = null;

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
    if (iframe && iframe.parentNode) {
      iframe.parentNode.removeChild(iframe);
    }
    agent.completeTask(taskId, envelope);
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
      var response = await fetch(payload.query_url, {
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
      message.token !== payload.channel_token ||
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
      code: payload.javascript
    });
  }

  function renderFrame(document_) {
    if (finished) {
      return;
    }
    window.addEventListener("message", acceptBridgePort);
    iframe = document.createElement("iframe");
    iframe.setAttribute("sandbox", "allow-scripts allow-forms");
    // Hidden but still laid out, so getBoundingClientRect and
    // getComputedStyle keep returning real measurements.
    iframe.style.cssText =
      "position: fixed; left: 0; top: 0; border: 0; " +
      "opacity: 0; pointer-events: none;";
    iframe.style.width = payload.viewport.width + "px";
    iframe.style.height = payload.viewport.height + "px";
    // srcdoc, matching how stored apps render in production: the
    // untrusted document never has an addressable URL a browser could
    // load outside the sandbox.
    iframe.srcdoc = document_;
    document.body.appendChild(iframe);
  }

  agent.claimTask(taskId).then(function(claim) {
    if (!claim || !claim.ok) {
      // Another tab claimed it, or the task already finished or
      // expired: stand down.
      return;
    }
    payload = claim.payload;
    // Started before the document fetch so a hung fetch still times out
    deadlineTimer = setTimeout(function() {
      finish({
        ok: false,
        error: {
          message: "Debug run timed out after " + payload.timeout_ms + "ms"
        },
        timed_out: true
      });
    }, Math.max(1000, payload.timeout_ms));
    fetch(payload.frame_url, {credentials: "same-origin"}).then(
      function(response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.json();
      }
    ).then(function(body) {
      renderFrame(String(body.document || ""));
    }).catch(function(error) {
      finish({
        ok: false,
        error: {
          message: "Failed to load debug frame: " +
            String((error && error.message) || error)
        },
        timed_out: false
      });
    });
  }).catch(function() {
  });
})();
</script>"""


def build_debug_harness_html():
    return _HARNESS_HTML
