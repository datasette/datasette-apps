# Browser tasks: a first-class primitive for datasette-agent

A spec for datasette-agent. Written from the experience of implementing
`app_debug` in datasette-apps, which today rides on `context.ask_user()`
— a mechanism designed for asking humans questions, pressed into service
as a way to get code running in the user's browser. It works, but every
seam shows. This document describes the primitive datasette-agent should
grow instead: **browser tasks** — tool-initiated units of work executed
by the user's connected browser, with durable suspend/resume, one-shot
execution, and a public completion API.

## Why ask_user() is the wrong primitive

`app_debug` needs three things from the agent runtime:

1. **Durable turn suspension** — the tool cannot finish until a browser
   has done work on its behalf, possibly minutes or days later.
2. **Script-executing HTML in the chat page** — the harness that claims
   the job, renders the hidden iframe, and relays bridge messages has to
   actually run in the user's browser.
3. **A resume signal carrying a result** — when the browser finishes,
   the suspended turn resumes and the tool call re-executes.

`ask_user(free_text=True, html=harness)` provides approximations of all
three, each with a defect:

- **The human-facing surface is a lie.** The user sees a free-text
  question with a textarea and an Answer button they are never supposed
  to touch. The prompt has to say "this completes automatically." If
  the user *does* type an answer, the tool resumes with a garbage
  string and has to detect that the job never ran. The question/answer
  data model (`question_type`, `options_json`, answer validation in the
  question-answer view) is dead weight for this use.

- **Question HTML does not execute scripts.** `renderQuestionForm()` in
  `agent.js` inserts `question.html` with `insertAdjacentHTML()` and —
  unlike the tool-result `_html` path, which deliberately re-creates
  `<script>` elements so they run — never executes scripts. So the
  hack does not even work without modifying datasette-agent, and the
  minimal fix (re-create scripts in question HTML too) widens the
  script-execution surface of *questions*, a human-interaction feature
  that never needed it.

- **Completion is DOM scraping.** The harness answers its own question
  by walking up to the enclosing `.agent-question` container, finding
  the `<textarea>`, setting a throwaway value, and calling
  `form.requestSubmit()`. That couples every self-answering tool to the
  question form's private markup. The plan for `debug_app` already
  flagged this as "the only coupling" and proposed a public hook; this
  spec is that hook, generalized.

- **Replay safety is outsourced to every tool.** Conversation history
  re-renders persisted HTML, so any script-bearing HTML re-executes on
  every page load. `app_debug` defends itself with a one-shot claim
  endpoint and a job table in datasette-apps. That gate is load-bearing
  and *every future browser-executing tool would have to rebuild it*.
  The runtime that renders the HTML should own the guarantee that a
  task executes at most once.

- **The result travels out of band.** The ask_user answer is a
  meaningless completion ping; the real result envelope goes through a
  datasette-apps `/result` endpoint into a datasette-apps job row, which
  the resumed tool reads back. Two persistence layers, two endpoints,
  and none of it reusable by the next tool.

The shape underneath is generic: *suspend the turn, hand the browser a
unit of work, resume with its result, guarantee at-most-once execution*.
That belongs in datasette-agent.

## Design goals

- One tool-facing call that subsumes suspension, rendering, execution,
  and result delivery.
- At-most-once execution enforced by the runtime, surviving page
  reloads, history replay, multiple open tabs, and server restarts.
- No fake question UI; an honest "working in your browser" affordance
  with a user escape hatch.
- A small public JavaScript API instead of DOM coupling.
- Same durability model as questions (a table keyed on
  `(conversation_id, call_key, index)`), so a suspended conversation
  resumes after a restart, and the same PauseChain semantics, so
  concurrent sibling tool calls complete before the turn suspends.
- Graceful degradation where no browser exists (background agents, CLI).

## Tool API

```python
result = await context.browser_task(
    html,              # trusted HTML rendered into the chat page; scripts run
    payload=None,      # JSON handed to the executing page, exactly once
    label=None,        # human-visible status line, e.g.
                       #   "Running debug script against Sales dashboard"
    timeout_ms=60_000, # server-enforced deadline for the whole task
)
```

Semantics, mirroring `ask_user()` where that model is right:

- If this task (identified by `(conversation_id, call_key, task_index)`
  — a per-invocation counter like `ask_index`) has a completed result,
  return it immediately. This is the replay path when the tool call
  re-executes after resume.
- Otherwise insert a `pending` task row and raise
  `BrowserTaskPending(llm.PauseChain)`. Concurrent sibling tool calls
  run to completion first, no provider call is made with a placeholder
  result, and the agent loop ends the turn — exactly the existing
  `QuestionPending` flow in `agent.py`.
- Re-raising for an already-pending task must not insert a duplicate
  row (same guard `ask_user` has).
- On successful tool completion the runtime marks the call's tasks
  consumed (the `mark_questions_consumed` pattern), so a later
  identical call runs fresh.

The return value is whatever the page posted: `{"ok": true, "result":
...}` or `{"ok": false, "error": {...}}` (see the complete endpoint).
Failure outcomes — timeout, user skip, browser error — come back as
data, not exceptions, because the tool re-executes from the top on
resume and needs to handle them as ordinary results:

```python
{"ok": False, "error": {...}, "outcome": "completed" | "expired" | "cancelled"}
```

Capability detection:

- `context.supports_browser_tasks` — `False` for background agents and
  CLI chat.
- Calling `browser_task()` unsupported raises
  `BrowserTasksNotSupported`, with the same contract as
  `QuestionsNotSupported`.
- `browser_task_callback` — the analogue of `ask_user_callback`: a host
  hook that satisfies tasks synchronously. Tests use it to fake the
  browser; a future headless host (Playwright worker) could implement
  it to run tasks without any human tab open. The tool contract does
  not change.

The model never sees `html` or `payload`, just as it never sees `_html`
(`prepare_tool_output_for_model` strips it). The tool's own return
value is the only thing that reaches the model.

## Task lifecycle

```
pending ──claim──▶ running ──complete──▶ completed
   │                  │
   │                  ├──deadline──▶ expired
   ├──deadline──▶ expired
   ├──user skip──▶ cancelled
   └──user skip (running)──▶ cancelled
```

**The claim transition is the heart of the design.** A page must claim
a task (`pending → running`, atomic compare-and-set) before it receives
the payload, and the claim succeeds exactly once. History replay,
duplicate tabs, and re-renders all hit "already claimed" and do
nothing. This moves the load-bearing gate that `app_debug` built in
datasette-apps into the runtime, where every browser-executing tool
gets it for free.

Terminal states all resume the suspended turn:

- `completed` — the page posted a result (which may itself report
  `ok: false`, e.g. a debug script that timed out inside the frame).
- `expired` — the server deadline (`created_at + timeout_ms`, with a
  sane cap, say 10 minutes) passed without completion. Enforced
  lazily: any claim or complete after the deadline converts the task
  and is rejected; the conversation view converts overdue tasks when it
  loads. The executing page should also keep its own earlier deadline
  and complete with a timeout result, so `expired` only covers crashed
  or closed tabs.
- `cancelled` — the user clicked Skip in the task UI. This is the
  escape hatch ask_user never had: a hung task cannot brick the
  conversation, because the user can always cancel it and let the tool
  resume with a failure result.

If no browser is connected, the task simply stays `pending` and the
turn stays suspended; reopening the conversation renders the pending
task and execution proceeds. Same graceful behavior the question flow
has today, but by design rather than by accident.

## Data model

```sql
CREATE TABLE IF NOT EXISTS agent_browser_tasks (
    id TEXT PRIMARY KEY,             -- ULID
    conversation_id TEXT NOT NULL,
    call_key TEXT NOT NULL,          -- same derivation as agent_questions
    task_index INTEGER NOT NULL,     -- per-invocation counter (cf. ask_index)
    tool_name TEXT NOT NULL,
    label TEXT,
    html TEXT NOT NULL,
    payload_json TEXT,
    timeout_ms INTEGER NOT NULL,
    status TEXT NOT NULL,            -- pending | running | completed
                                     --   | expired | cancelled | consumed
    result_json TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    completed_at TEXT,
    completed_by TEXT,               -- actor id that claimed/completed
    UNIQUE (conversation_id, call_key, task_index)
);
```

Notes:

- `html` is retained after completion for audit, but is never rendered
  again once the task leaves `pending` (see frontend rules). Replay
  safety comes from "only pending tasks render live HTML", with the
  one-shot claim as the backstop.
- `result_json` is size-capped on write (512 KB, say). An oversized
  completion is rejected with a structured error so the page can post a
  trimmed result; tools with bigger appetites keep their own storage,
  as `app_debug` does with its job table.
- `payload_json` is where per-run secrets (tokens, capability URLs)
  live. It is only ever handed out through the one-shot claim, never
  embedded in `html`, so the persisted HTML contains nothing sensitive.

## HTTP endpoints

All under the conversation, all requiring the conversation's actor
(same ownership check the question-answer view performs today).

**`POST /-/agent/{conversation_id}/task/{task_id}/claim`**

Atomically `pending → running`. Response on success:

```json
{"ok": true, "task": {"id": "...", "payload": {...}, "timeout_ms": 15000}}
```

On any other state: `{"ok": false, "state": "running" | "completed" |
"expired" | "cancelled"}` — the caller renders nothing and does
nothing. Past-deadline claims convert to `expired` and report it.

**`POST /-/agent/{conversation_id}/task/{task_id}/complete`**

Body: `{"ok": true, "result": ...}` or `{"ok": false, "error": {...}}`.
Valid only from `running` (or `pending`, for hosts that skip claiming —
callback executors); first write wins. Marks the task `completed`,
stores the envelope, and **resumes the suspended chain, streaming the
continuation back on this response** exactly as the question-answer
endpoint does (`AsgiStream` around `resume_agent`). The completing tab
is the tab watching the conversation, so it receives the resumed turn's
events on the same connection — no new transport needed.

**`POST /-/agent/{conversation_id}/task/{task_id}/cancel`**

User-initiated skip. `pending/running → cancelled`, resumes the chain
the same way. The tool sees `{"ok": false, "outcome": "cancelled",
"error": {"message": "Cancelled by the user"}}`.

## Agent loop and streaming

- `agent.py` catches `BrowserTaskPending` alongside `QuestionPending`
  and ends the turn after persisting history.
- The stream emits a `browser_task` SSE event `{id, label, html,
  tool_name, timeout_ms}` (peer of the existing `question` event).
- The conversation template embeds pending tasks the way it embeds
  `pending_question_json`, so a reloaded page picks up in-flight tasks
  and re-renders them (they render, attempt to claim, and quietly stand
  down if another tab already claimed).

## Frontend

**Rendering a pending task** (`renderBrowserTask(task)` in `agent.js`):

1. Append a status element to the transcript: spinner, `task.label`
   (fallback: "Working in your browser…"), the tool name, and a
   **Skip** button wired to the cancel endpoint.
2. Append the task HTML in a container and re-create its `<script>`
   elements so they execute — the exact mechanism `appendToolResult`
   already uses for `_html`. This is the sanctioned script-execution
   path; questions stay script-free forever.
3. Chat input remains disabled while the turn is suspended (as with
   questions), but the visible status + Skip button make the state
   legible and recoverable.

**Completed/expired/cancelled tasks and history replay:** render an
inert one-line record — label, outcome, duration. The stored `html` is
*never* re-rendered once the task has left `pending`. Server-side
history rendering follows the same rule: task HTML is excluded from
`{{ ... | safe }}` replay entirely. This is the structural fix for the
replay-execution hazard: the claim gate becomes defense-in-depth
instead of the only line of defense.

**Public JavaScript API**, the piece the debug plan called "a small
public hook in datasette-agent":

```js
window.datasetteAgent = {
  // One-shot: resolves {payload, timeoutMs} once per task, ever.
  claimTask(taskId) -> Promise<{ok, payload?, timeoutMs?, state?}>,
  completeTask(taskId, {ok, result?, error?}) -> Promise<void>,
  cancelTask(taskId) -> Promise<void>,
};
```

Task HTML calls these instead of fetching endpoints by hand or scraping
form markup. `completeTask` internally consumes the resumed-turn event
stream from the complete endpoint so the transcript continues rendering
seamlessly.

## Security model

- **Trusted HTML only.** Task `html` is authored by server-side tool
  code, the same trust level as tool-result `_html` today. Nothing
  model-generated or user-generated is ever rendered as task HTML.
- **Actor-bound.** Claim/complete/cancel require the conversation's
  actor; tasks are keyed to their conversation.
- **At-most-once, twice over.** Structurally (non-pending tasks never
  render their HTML) and transactionally (the claim CAS).
- **Secrets in payload, not markup.** Persisted HTML is safe to store
  and audit because everything sensitive rides the one-shot claim.
- **Audit.** The task row records what ran, initiated by which tool, in
  which conversation, claimed and completed by whom, when.
- **Results are data.** Task results flow into the resumed tool call,
  which decides what the model sees; the runtime never feeds page
  output to the model directly.

## What app_debug looks like afterwards

```python
outcome = await context.browser_task(
    html=build_debug_harness_html(datasette, job),
    payload={
        "frame_url": frame_url_with_token,
        "channel_token": channel_token,
        "javascript": javascript,
        "viewport": viewport,
        "timeout_ms": timeout_ms,
    },
    label=f"Running debug script against {app['name']}",
    timeout_ms=timeout_ms + 2000,
)
```

datasette-apps keeps what is app-domain: the `_app_debug_jobs` audit
row, the `/frame` endpoint (still capability-URL'd, since sandboxed
iframe loads may arrive without cookies), and the `/query` endpoint
that enforces the app's database and stored-query allow-lists
server-side. It sheds what was transport:

- the free-text question charade and its prompt copy;
- `answerQuestion()` form scraping in the harness — replaced by
  `datasetteAgent.completeTask(taskId, envelope)`;
- the `/claim` endpoint — payload delivery and one-shot gating move to
  the runtime (the job row keeps a status column purely as audit);
- the `/result` endpoint — the envelope arrives as `browser_task()`'s
  return value; the resumed tool writes it to the job row itself for
  the audit trail;
- the "user typed an answer by hand" failure mode — impossible, there
  is nothing to type into.

The bridge, the debug-only eval channel, `debug.waitFor`, and the
envelope shape are untouched: they were never part of the hack.

## Non-goals

- **Not a general RPC channel.** One task, one payload, one result. A
  tool needing several rounds issues several tasks; `task_index` keeps
  them ordered and replay-safe.
- **No partial/streaming results.** The envelope is atomic. Progress
  display, if ever wanted, is a UI concern layered on later.
- **No persistent page-side daemon.** Tasks are hermetic, like debug
  runs: render, execute, complete, tear down.
- **No model-authored task HTML.** If a future feature wants the model
  to compose browser work, it must go through a server-side tool that
  emits vetted HTML.

## Open questions

- Should Skip require a confirmation, given cancelling resumes the turn
  with a failure the model then reacts to? Leaning no — one click, with
  the button labelled "Skip this step".
- Multiple tasks pending simultaneously (parallel tool calls): render
  all, claim independently, resume once when the last blocking task
  finishes — PauseChain already defines turn semantics, but the UI
  ordering deserves a look during implementation.
- Whether `browser_task_callback` should be the same hook that a future
  headless executor uses, or whether headless deserves a registered
  executor abstraction. Callback first; promote if a second
  implementation appears.
- Whether the question flow should eventually adopt the same inert
  history rendering for `question.html` (it renders live HTML from
  history today, though without scripts). Out of scope here, but the
  precedent is set.
