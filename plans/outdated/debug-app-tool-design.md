# debug_app() tool design

A feedback loop for agent-authored apps. Today the agent writes app HTML
blind: it cannot tell whether the app rendered, errored, or laid out
correctly, so the human transcribes stack traces back into the chat. The
`debug_app()` tool closes that loop: the agent loads the app in a real
sandboxed iframe, executes JavaScript inside it, and gets back a
JSON-serializable result plus every error the app produced while
rendering — all in a single tool call.

The design goal is the **tool contract**. How execution happens is
incidental and can change; the first implementation runs the app
invisibly in the user's own browser via the chat page.

## Tool contract

```
debug_app(
    app_id,            # stored app to debug, OR:
    html, config,      # raw candidate HTML + app config (databases,
                       # stored queries, CSP origins) - exactly one of
                       # app_id / html must be given
    version=None,      # optional revision number (app_id form only;
                       # defaults to current version)
    javascript,        # async-capable JS executed inside the app iframe;
                       # its (awaited) return value is the result
    viewport=None,     # optional {"width": int, "height": int};
                       # defaults to a standard desktop size
    timeout_ms=15000,  # hard cap on the whole run
)
```

Example call:

```js
// javascript argument:
const chart = await debug.waitFor(() => document.querySelector("#chart"));
return {
  chartWidth: chart.getBoundingClientRect().width,
  inputValue: document.querySelector("input[name=q]").value,
  rowCount: document.querySelectorAll("tbody tr").length,
};
```

Example result:

```json
{
  "ok": true,
  "result": {"chartWidth": 350, "inputValue": "some value", "rowCount": 12},
  "events": {
    "errors": [],
    "logs": [
      {"kind": "datasette-call",
       "message": "datasette.query(main, select ...)"}
    ]
  },
  "duration_ms": 1840,
  "timed_out": false
}
```

### Result shape

Every run returns the same envelope. `events` carries everything the
existing iframe bridge already captures — JavaScript errors, unhandled
rejections, CSP violations, failed fetches, `console.error`,
`console.log`, failed `datasette.query()` / `datasette.storedQuery()`
calls — collected from page load through script completion.

Three failure modes, all distinguishable, all still returning captured
events (a timeout with 12 CSP violations attached is a useful answer,
not a failure):

1. **The app errored while rendering.** `events.errors` is populated;
   the debug script may still have run and produced a result.
2. **The debug script threw.** `ok: false`, `error` holds the script's
   error (message + stack), `events` holds whatever the app did — which
   is usually the explanation.
3. **Timeout.** `timed_out: true`, plus everything captured so far.

Results must be JSON-serializable. DOM nodes, functions, and other
non-serializable values are rejected with a corrective error message
("return .textContent or measurements, not elements"). Payloads are
size-capped with explicit truncation markers.

### Readiness is the script's problem, not the harness's

Apps render asynchronously: load, `await datasette.query(...)`, then
build the DOM. A script that runs at load time measures an empty page.
Rather than a settle-detection heuristic in the harness (unreliable for
apps that poll on a timer, and a knob nobody can tune), the debug script
controls its own waiting:

- The `javascript` argument is the body of an async function; its
  resolved value is the result.
- The harness provides exactly one helper inside the frame:
  `await debug.waitFor(fn, {timeout, interval})` — polls `fn` until it
  returns a truthy value, resolves with that value, rejects on timeout.

The agent writes the readiness condition per test
(`debug.waitFor(() => document.querySelector("#chart"))`), which it is
good at. This is one polling helper, not a test DSL — assertions,
measurements, and interactions are plain JavaScript.

### Viewport as a parameter

The harness controls the hidden iframe's size, so expose it. For CSS
work this is the most valuable knob on the tool: test the mobile
breakpoint at `{"width": 375, "height": 812}`, then desktop at
`{"width": 1280, "height": 800}`, in two calls. The iframe is hidden
with `opacity: 0; pointer-events: none` — **not** `display: none`,
which kills layout and makes every measurement return 0.

### Raw HTML input absorbs the preview problem

`debug_app(app_id=...)` tests what is saved. `debug_app(html=...,
config=...)` tests a candidate without saving anything, so the agent's
iterate-until-clean loop writes no revisions: iterate against raw HTML,
save once with the normal app tools when the run comes back clean. This
removes the need for a separate ephemeral-preview subsystem in the agent
loop. The `config` object uses the same validation paths as stored
apps — database names through the visible-database filter, stored
queries through the view-query filter, CSP origins through
`resolve_csp_origins()` with the same `apps-set-csp` gating. Preview
config is not a bypass for any of it.

## How it executes

Execution rides on infrastructure that already exists in
`datasette-agent` and `datasette-apps`. Verified against both codebases:

- `datasette-agent` renders tool-result `_html` **with scripts
  executing**, deliberately, in both paths: the live stream re-creates
  `<script>` elements after `insertAdjacentHTML` so they run
  (`static/agent.js`), and conversation-history replay renders
  `{{ html_content | safe }}` server-side, where parser-inserted scripts
  execute on every page load. The model never sees `_html`
  (`prepare_tool_output_for_model` strips it).
- `context.ask_user(html=...)` suspends the turn durably
  (`QuestionPending` / `llm.PauseChain`, persisted in
  `agent_questions`), renders trusted HTML in the chat, and resumes the
  tool with the posted answer. The SQL write-approval flow already uses
  exactly this.

### Job lifecycle

1. **Create.** The tool validates permissions and config, stores a job
   row (`_app_debug_jobs`), and calls `context.ask_user()` with the
   harness markup as the question `html`. The turn suspends.
2. **Claim.** The harness script (now running in the user's chat page)
   POSTs `/-/apps/debug/{job_id}/claim`. The server marks the job
   claimed; any subsequent claim gets "already executed" and the harness
   does nothing. This one-shot gate is load-bearing, not optional
   hardening: history replay re-executes `_html` scripts on **every**
   conversation reload, and without the claim step, reopening an old
   conversation would silently re-run debug scripts — including any
   that touch write stored queries.
3. **Render.** The harness creates the hidden sandboxed iframe
   (`sandbox="allow-scripts"`, sized to `viewport`, `opacity: 0;
   pointer-events: none`) loading
   `/-/apps/debug/{job_id}/frame` — a page served by datasette-apps
   built with the same `build_app_srcdoc()` pipeline as stored app
   views: production CSP meta tag plus the injected bridge. The harness
   is a new parent, not a new sandbox.
4. **Bridge + relay.** The bridge connects to the harness over the
   existing MessageChannel handshake. The harness relays
   `datasette.query()` / `datasette.storedQuery()` calls to
   `/-/apps/debug/{job_id}/query`, which enforces the job's allow-lists
   (for `app_id` jobs, the stored app's grants; for raw-HTML jobs, the
   validated `config`) and forwards as the current actor, exactly like
   `/-/apps/{id}/query`.
5. **Eval.** The harness sends the debug script over the MessagePort as
   a `datasette-app-debug-eval` message. The bridge — extended with this
   message type **only when injected in debug mode** — executes it by
   inserting an inline `<script>` element that wraps the code in an
   async function with reporter callbacks:
   `try { report(id, await (async () => { ...code... })()) } catch (e)
   { reportError(id, e) }`. Inline script elements are already permitted
   by the production `script-src 'unsafe-inline'` policy, so arbitrary
   execution requires **no CSP loosening** — no `'unsafe-eval'`, no
   debug-only policy variant. The code runs in the app's real global
   scope: `getComputedStyle`, `getBoundingClientRect`, calling the
   app's own functions, `await datasette.query(...)` all work.
6. **Collect + answer.** The harness accumulates bridge events from
   frame load onward, receives the eval result (or error, or hits its
   own deadline just under `timeout_ms`), POSTs the full envelope to
   `/-/apps/debug/{job_id}/result`, removes the iframe, then completes
   the suspended `ask_user` question through the page's own answer
   flow so the resumed turn streams into the page normally. The answer
   itself is just a completion signal; the tool reads the result
   envelope from the job row and returns it as an ordinary tool result.

If the browser tab is closed mid-run, the turn stays suspended until the
user reopens the conversation — at which point the pending question's
HTML re-renders, the harness claims and runs the job, and the turn
resumes. Graceful "no browser connected" behavior for free; the claim
gate prevents double-execution once the job has run.

### Integration point with datasette-agent

Step 6 needs the harness to submit the answer for its own question. V1
can locate its enclosing question container and submit the rendered form
programmatically (the question flow's existing UI path). A cleaner
follow-up is a small public hook in datasette-agent — e.g.
`window.datasetteAgent.answerQuestion(questionId, answer)` — so tools
with self-answering `_html` do not depend on form markup. Either way,
this is the only coupling; datasette-agent needs no changes for v1.

While the question is pending the chat input is disabled, so the harness
enforces a firm client-side deadline and answers with
`timed_out: true` rather than leaving the turn suspended.

## Data model

```sql
CREATE TABLE IF NOT EXISTS _app_debug_jobs (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    app_id TEXT,               -- NULL for raw-HTML jobs
    version INTEGER,           -- NULL means current
    html TEXT,                 -- raw-HTML jobs only
    config TEXT,               -- validated JSON: databases, queries,
                               -- csp_origins, viewport
    javascript TEXT NOT NULL,
    status TEXT NOT NULL,      -- pending | claimed | completed | expired
    result TEXT,               -- full result envelope JSON
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    completed_at TEXT
);
```

Lives in the internal database alongside the other datasette-apps
tables. Jobs expire after a short window (pending jobs that were never
claimed) and completed jobs are pruned on an age/count cap. The table
doubles as the audit trail: execution is invisible by design and runs
queries as the user, so every run keeps a durable record of exactly what
script ran, initiated by whom, against what. The rendered tool call in
the transcript is the visible indicator; this table is the record.

## Security model

- **Permission gating.** `debug_app(app_id=...)` requires `edit-app` on
  that app; the raw-HTML form requires `create-app`. Debug jobs are
  author-initiated by construction — the tool only runs inside a
  conversation the actor is driving.
- **Trust is unchanged.** The agent already authors 100% of the app's
  HTML and JavaScript; agent-evaled JS runs in the same sandbox, same
  production CSP, same query allow-lists, same actor. The eval channel
  adds capability for the agent, not privilege.
- **Sandbox fidelity.** The frame is built by the same
  `build_app_srcdoc()` + CSP pipeline as production app views, with the
  same `sandbox` attributes. Debug results are predictive of production
  because nothing is relaxed.
- **The eval channel exists only in debug frames.** The bridge accepts
  `datasette-app-debug-eval` only when injected with the debug flag;
  ordinary app views have no eval surface.
- **One-shot execution.** The claim gate (above) is what makes
  script-bearing `_html` safe to persist in conversation history.
- **Writes.** If the app's grants include write stored queries, debug
  scripts can trigger them — as the current actor, through Datasette's
  own permission checks, identical to the user clicking the app's UI.
  The audit row records it.
- Known caveat, worth a line in the tool description: synthetic events
  dispatched by debug JS have `isTrusted: false` — app event handlers
  fire normally, but a few native browser behaviors (e.g. the bridge's
  own external-link interception, popup allowances) will not trigger.

## Non-goals

- **No Playwright / headless version.** We are not building it. The
  tool works when a human has the conversation open in a browser;
  background agents (where `ask_user` raises `QuestionsNotSupported`)
  get a clear error from `debug_app` explaining it needs an interactive
  conversation. The tool contract would permit a headless host later,
  but nothing in this design depends on one.
- **No persistent debug session.** Every call is hermetic: fresh page
  load, run script, tear down. Unit-test semantics, not REPL semantics —
  no stale DOM state between iterations, no attach/detach lifecycle, no
  "is a browser connected" bookkeeping. If a live-page use case appears
  later it can be a `reuse` variant of this tool, not a new
  architecture.
- **No test-script DSL.** `debug.waitFor` is the entire helper surface.
  Assertions are plain JavaScript returning plain JSON.
- **No CSP or sandbox loosening.** Debug frames run the production
  policy; eval works via inline script elements that the existing policy
  already allows.
- **No settle-detection heuristic.** Readiness is expressed in the
  debug script, per run.

## Relationship to the earlier proposal

This tool replaces the center of the previous plan
(telemetry-persistence-first) with a request/response design:

- **Ephemeral previews** for the agent loop are absorbed by the
  raw-HTML form. A human-viewable preview URL may still be worth having
  someday, but the iterate-until-clean loop no longer needs one.
- **Settle snapshots** shrink to "the agent asks for what it wants"
  (`document.body.innerText`, specific measurements) — the
  hardest-to-tune part of the bridge-extension section is deleted.
- **Persistent `_app_events` telemetry** is deferred. `debug_app`
  returns events inline, which covers the agent loop; `_app_debug_jobs`
  covers the audit need. A persisted event stream for *production*
  page loads (real users hitting errors) remains a possible future
  addition with its own confidentiality design — viewer-session
  telemetry contains data the viewing actor was allowed to see, and
  must not leak to readers with different permissions.
- **The `test` channel / `datasette.reportTest()`** is unnecessary:
  a debug script simply returns `{passed: [...], failed: [...]}`.

## Open questions

- Should `debug_app` also accept `html` *plus* `app_id` (candidate HTML
  with a stored app's config), to test an edit against existing grants
  without re-specifying them? Probably yes; cheap to add.
- Result payload cap size, and whether truncation should prefer keeping
  `events.errors` over `result` (probably: errors are why you called).
- Whether the harness should cap total `datasette.query()` calls per
  job as a runaway guard, mirroring the panel's existing 50/100 event
  caps.
