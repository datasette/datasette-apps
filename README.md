# datasette-apps

[![PyPI](https://img.shields.io/pypi/v/datasette-apps.svg)](https://pypi.org/project/datasette-apps/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-apps?include_prereleases&label=changelog)](https://github.com/datasette/datasette-apps/releases)
[![Tests](https://github.com/datasette/datasette-apps/actions/workflows/test.yml/badge.svg)](https://github.com/datasette/datasette-apps/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-apps/blob/main/LICENSE)

Create apps that live inside Datasette

## Installation

Install this plugin in the same environment as Datasette.

Read [Host applications inside Datasette with Datasette Apps](https://datasette.io/blog/2026/datasette-apps/) for more on this project.
```bash
datasette install datasette-apps
```
## Usage

This plugin introduces a new interface at `/-/apps` for browsing and searching available apps.

There are two types of app:

- HTML and JavaScript apps managed by this plugin, which run in a sandbox that limits them to approved Datasette data access and blocks communication with external sites unless those hosts have been explicitly allowed.
- Apps provided by other plugins that are written in Python and HTML/JavaScript which are unrestricted but can do more things

This plugin allows you to create and modify HTML apps, and provides a plugin hook to enable other plugins to add their own Python apps to the system.

- `/-/apps` lets you browse available apps
- `/-/apps/ULID` to interact with a full screen HTML app
- `/-/apps/create` for creating a new app
- `/-/apps/ULID/edit` to edit an existing app

Signed-in users get an "Apps" link in Datasette's top-right menu.

HTML apps managed by this plugin use lowercase monotonic ULIDs as their IDs and track every edit as a new row in `app_revisions`.

### Sandboxed apps

Stored apps are rendered inside a sandboxed iframe. The plugin injects a Content Security Policy into the iframe `srcdoc`.

Direct network access is blocked unless the app has exact `https://` origins configured. Those same origins are allowed for remote images, external script tags, and external stylesheet links/style elements. Localhost origins are never allowed. Local file previews using `data:` and `blob:` image URLs are allowed.

Configuring those origins on an app is restricted, because an origin can be used to leak private data out of the app. Users with the `apps-set-csp` permission can set any valid `https://` origin using a free-form field on the create and edit pages. Other users can only pick from the allow-list configured by the `allowed_csp_origins` plugin setting, if one exists - otherwise they cannot configure network access at all. When editing an app that already has origins outside that allow-list - for example origins added earlier by a privileged user - those existing origins can be kept or removed but new arbitrary origins cannot be added.

To configure the allow-list:

```yaml
plugins:
  datasette-apps:
    allowed_csp_origins:
    - https://cdn.jsdelivr.net
    - cdnjs.cloudflare.com
```

Entries without a scheme are treated as `https://` origins.

The iframe bridge reports JavaScript errors, unhandled promise rejections, CSP violations, failed resources, fetch failures, `console.error()` calls, and failed Datasette data queries back to the parent page. The app page shows these in a small expandable error panel above the iframe. It also captures `console.log()` messages and Datasette data helper calls in a collapsed log panel below the iframe.

The bridge also replaces `history.replaceState()`, `history.pushState()`, `history.back()`, `history.forward()`, and `history.go()` with no-op functions inside the sandboxed iframe, avoiding browser errors from apps that try to manage URL state.

### Data access

Stored apps can query Datasette data using the injected `datasette.query(database, sql, params)` helper.

They can also run allow-listed stored queries using `datasette.storedQuery(database, query, params)`. The iframe sends those requests to the parent page with `postMessage`, and the parent page forwards them to an app-scoped query endpoint.

Apps have allow-lists configured on the edit page. If the requested database or stored query is allowed, the request is forwarded to Datasette's own JSON APIs using the current actor, so Datasette's normal SQL and query permissions still apply.

Failed stored-query attempts, including attempts to call a query that is not allow-listed or that the current actor cannot run, are reported in the app page error panel.

Stored query access is configured using a picker on the create and edit pages. The picker searches Datasette's `/-/queries.json?q=search-term` API and stores selected queries as `database-name/query-name` strings. Removing a query uses the `x` button next to that selected query. Additions and removals are not applied until the page is saved.

### Permissions

The plugin registers Datasette permissions for `create-app`, `view-app`, `edit-app`, `delete-app`, `manage-app-access`, and `apps-set-csp`. Stored app owners can always view, edit, delete, and manage their own apps. Apps marked private are visible only to their owner, even if other users have broad `view-app` permission grants.

Creating stored apps requires an explicit `create-app` permission grant. To grant that to all signed-in users:

```yaml
permissions:
  create-app:
    id: "*"
```

The `apps-set-csp` permission controls who can set arbitrary CSP origins on an app - see [Sandboxed apps](#sandboxed-apps). Nobody has it by default. To grant it to a specific user:

```yaml
permissions:
  apps-set-csp:
    id: admin
```

Deleting a stored app hides it from the catalog and disables access to its pages and query API. Its `app_revisions` rows remain in the database so a database administrator can recover it if needed.

Apps that are not private can be viewed by actors with the `view-app` permission. To let all signed-in users view all non-private apps, configure:

```yaml
permissions:
  view-app:
    id: "*"
```

External apps registered by plugins are not private by default, so they also require `view-app` permission unless the registering plugin supplies its own permission rules.

Signed-in users can pin apps from the catalog and from individual stored app pages. Pinned apps appear first on `/-/apps`, and the three most recently used pinned apps are shown on the Datasette homepage using `top_homepage()`.

The `/-/apps` catalog is searchable and paginated.

### App authoring

The create and edit pages include a copyable prompt for an LLM. The prompt is assembled in the browser from the current form state, so unsaved changes to selected databases, stored queries, and network origins are reflected immediately. It explains the sandbox, the `datasette.query()` and `datasette.storedQuery()` bridges, CSP restrictions, the current schema summary, and only the stored queries selected for that app.

The create and edit pages use Datasette's existing bundled CodeMirror editor for the HTML source textarea.

The edit page includes a private checkbox, SQL query database access, stored query access, and allowed network origins. The network origins control is a free-form list for users with the `apps-set-csp` permission, a set of checkboxes when an `allowed_csp_origins` allow-list is configured, and hidden otherwise.

### External apps from plugins

Datasette plugins can register their own apps that are independent of the HTML sandbox system. This allows apps with more complex requirements to still participate in other features built around the app registry.

Apps that are not stored as HTML are called **external apps**. They can be added and removed using the `Registry` class.

This example registers an additional app on startup:

```python
from datasette import hookimpl
from datasette_apps import Registry


@hookimpl
async def startup(datasette):
    await Registry(datasette).add_app(
        id="myplugin:example",
        name="Example plugin app",
        description="A plugin-owned app that appears in /-/apps",
        path="/-/myplugin-example",
        source="myplugin",
    )
```

Here's how to remove a single external app from the catalog by ID:

```python
await Registry(datasette).remove_app("myplugin:example")
```

## Telemetry

datasette-apps emits [OpenTelemetry](https://opentelemetry.io/) spans and metrics under the instrumentation scope `datasette_apps`, using Datasette's plugin telemetry kit (requires Datasette 1.0a41+). It depends on `opentelemetry-api` only and never installs a provider or an exporter: nothing is recorded unless an OpenTelemetry SDK is configured. See Datasette's telemetry documentation for running under `opentelemetry-instrument`, or use [datasette-otel-viewer](https://github.com/datasette/datasette-otel-viewer) to browse this instance's own telemetry at `/-/otel`.

The plugin adds one span of its own, `apps.query`, and otherwise adds attributes to spans Datasette core (or datasette-agent) already creates. A running app calling `datasette.query()` produces this tree (core's spans marked):

```
POST /-/apps/{id}/query                     SERVER   (core, apps.app_id)
├── db.query  (app lookup, permissions)     CLIENT   (core)
└── apps.query                              INTERNAL
    ├── db.query  (allowlist)               CLIENT   (core)
    └── GET /{database}/-/query.json        SERVER   (core, datasette.internal_client)
        └── db.query  select ...            CLIENT   (core)
```

Request span names are abbreviated here: core names them `{method} {http.route}`, using Datasette's route pattern (for example `POST ^/-/apps/(?P<id>[^/]+)/query$`).

The nested `GET` is Datasette's JSON API called through `datasette.client`, marked `datasette.internal_client: true` - filter on that to keep request counts honest. It nests under `apps.query` because this plugin injects W3C trace context headers into that internal request, a workaround for [datasette#2969](https://github.com/simonw/datasette/issues/2969).

`apps.debug.runs` counts each terminal `app_debug` tool execution. For a run that reaches the browser, that is the resumed call that records the result; the first execution, which suspends waiting for the browser, records nothing. The debug run's `/frame` and `/query` requests carry `apps.debug.job_id` and `apps.app_id` on core's request spans.

Agent tools don't open spans of their own. They add `apps.app_id` and `apps.tool.result` (plus `apps.debug.job_id` and `apps.debug.outcome` for `app_debug`) to the current span - datasette-agent's `execute_tool` span when the installed datasette-agent has telemetry. These attributes are set when a tool returns; an exception escaping a tool leaves them unset and records no `apps.debug.runs` (a datasette-agent version with telemetry records it on its own span as `error.type`).

**What is never recorded:** app HTML, names or descriptions; SQL text (core's `db.query` spans record SQL, and the internal request's `url.path` includes a stored query's name - this plugin does not add either again); SQL parameter values; debug scripts and their results, logs and errors; CSP origins; actor or conversation IDs; the frame, channel and bridge tokens; exception messages. App IDs, debug job IDs and database names appear on spans only, never as metric dimensions. `tests/test_telemetry_conformance.py` plants sentinel values for these in a broad workload: app HTML, names, descriptions, parameter values, debug scripts and results, CSP origins, actor and conversation IDs, and tokens are checked against every span and metric, core's included. SQL text and stored query names are checked against this plugin's scope only, because core records them by design.

Every span, metric and attribute below is declared in `datasette_apps/telemetry_registry.py`; the conformance test holds the code to this reference in both directions, and `uv run scripts/telemetry-doc.py` regenerates it.

<!-- telemetry-reference:start -->

#### Spans

**`apps.query`** - One query from a running app (or an `app_debug` run) through the plugin's query endpoint: body parsing, the allowlist check and the internal `datasette.client` call to Datasette's JSON API, which nests inside as a SERVER span tagged `datasette.internal_client`. Only an unexpected exception (outcome `error`) sets status `ERROR`; exception messages are never recorded. `db.namespace` reuses core's attribute, which is declared as always present, but here it is set only once the database has passed the app's allowlist - so it is absent for `invalid` and `not_allowed` queries, and the requested database name is never recorded unvetted. The internal request nests under this span because the plugin injects W3C trace context headers into it with the global propagator, a workaround for https://github.com/simonw/datasette/issues/2969: with the default propagator core would otherwise start it as a new trace. With `OTEL_PROPAGATORS=none` nothing is injected, but core extracts nothing either, so it still nests.

Attributes: `apps.app_id`, `apps.query.source`, `apps.query.outcome`, `apps.query.kind` *(optional)*, `apps.query.write` *(optional)*, `db.namespace`, `apps.query.rows` *(optional)*, `apps.debug.job_id` *(optional)*, `error.type` *(optional)*.

#### Attributes on spans this plugin does not own

- On core's HTTP request spans (scope `datasette`): `apps.app_id`, `apps.debug.job_id` *(optional)*.
- On the agent tool span (datasette-agent's `execute_tool {tool}`, scope `datasette_agent`, when it has telemetry): `apps.app_id`, `apps.debug.job_id` *(optional)*, `apps.tool.result`, `apps.debug.outcome`.

#### Metrics

**`apps.queries`** *(Counter, unit `{query}`)* - App queries handled, by source, outcome, kind and whether a stored query was a write.

Attributes: `apps.query.source`, `apps.query.outcome`, `apps.query.kind` *(optional)*, `apps.query.write` *(optional)*.

**`apps.query.duration`** *(Histogram, unit `s`)* - Time to handle one app query end to end, as the app sees it. Uses core's duration buckets so it lines up with `db.client.operation.duration`.

Attributes: `apps.query.source`, `apps.query.outcome`, `apps.query.kind` *(optional)*.

Bucket boundaries: 0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10.

**`apps.changes`** *(Counter, unit `{change}`)* - App changes (create, edit, delete, pin, unpin), by operation and whether they came from the web UI or the agent. Counted only after the write succeeded. An edit is one submitted and saved edit form or agent edit call (an `app_edit` batch counts once), even when nothing changed and no revision was written. Apps registered by other plugins through the `Registry` API (`add_app`/`add_apps`) are not counted.

Attributes: `apps.change.operation`, `apps.change.via`.

**`apps.debug.runs`** *(Counter, unit `{run}`)* - Terminal `app_debug` tool executions, by outcome. The first execution that suspends waiting for the browser is not counted, and neither is an execution that ends in an escaped exception (there is no `error` outcome).

Attributes: `apps.debug.outcome`.

**`apps.debug.run.duration`** *(Histogram, unit `s`)* - Wall-clock time from debug job creation to the resumed tool call recording its result, including the browser claiming the task. Only for outcomes that came back from the browser.

Attributes: `apps.debug.outcome`.

Bucket boundaries: 0.5, 1, 2.5, 5, 10, 15, 30, 60, 120, 300, 900.

**`apps.debug.script.duration`** *(Histogram, unit `s`)* - The debug script's own run time as reported by the browser (`duration_ms`), capped by the 60s maximum script timeout.

Attributes: `apps.debug.outcome`.

Bucket boundaries: 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 30, 60.

**`apps.debug.jobs.expired`** *(Counter, unit `{job}`)* - Pending debug jobs flipped to `expired` because a `/frame` or `/query` request arrived after the 15 minute job window. Counted only when a row actually changed.

#### Attributes

- `apps.app_id` - The app's ID: a ULID for stored apps, or a plugin-supplied ID for external apps. Unbounded, so it rides on spans only - **never on a metric**. Besides `apps.query`, it is added to spans this plugin does not own: core's HTTP request span for the app's routes (view, JSON, edit, delete, revision, pin, unpin, launch, query, and the `app_debug` frame and query routes), and the current span inside this plugin's agent tools, which with datasette-agent's telemetry is its `execute_tool` span in the `datasette_agent` scope. Only recorded once the ID is known to name a real app, so a made-up ID in a URL or a tool argument is never recorded. Used on: `apps.query`, core's HTTP request spans, the agent tool span.
- `apps.debug.job_id` *(optional)* - The `_app_debug_jobs.id` (a ULID) of an `app_debug` run. Spans only - **never on a metric**. Present on `apps.query` only for queries made by a debug run. Also added to core's HTTP request span for the debug run's `/frame` and `/query` routes (once the job is known to be running and to belong to the actor) and to the `app_debug` tool span. Used on: `apps.query`, core's HTTP request spans, the agent tool span.
- `apps.query.source` - Which endpoint the app's query arrived on: `app` (`/-/apps/{id}/query`, a running app) or `debug` (`/-/apps/debug/{job}/query`, an `app_debug` run). One of: `app`, `debug`. Used on: `apps.query`, `apps.queries`, `apps.query.duration`.
- `apps.query.kind` *(optional)* - `sql` for `datasette.query()`, `stored` for `datasette.storedQuery()`. Absent when the request body is not valid JSON. One of: `sql`, `stored`. Used on: `apps.query`, `apps.queries`, `apps.query.duration`.
- `apps.query.write` *(optional)* - True for a stored query declared `is_write`. Only known once the stored query has been resolved, so absent otherwise. One of: `false`, `true`. Used on: `apps.query`, `apps.queries`.
- `apps.query.outcome` - How the app query ended. `ok`: the query ran and returned a result. `invalid`: the request body or parameters were malformed. `not_allowed`: the database or stored query is not on the app's allowlist. `not_found`: the database or stored query doesn't exist. `denied`: Datasette's permission checks refused the actor. `failed`: Datasette rejected or failed the query (SQL error, time limit, write denied by a read-only query). `error`: an unexpected exception escaped - the only outcome that sets span status `ERROR`. Clamped at the call site: anything unrecognised becomes `error`. One of: `denied`, `error`, `failed`, `invalid`, `not_allowed`, `not_found`, `ok`. Used on: `apps.query`, `apps.queries`, `apps.query.duration`.
- `apps.query.rows` *(optional)* - Number of rows returned. Only on `ok` read queries. Used on: `apps.query`.
- `apps.change.operation` - What changed: an app was created, edited, deleted, pinned or unpinned. One of: `create`, `delete`, `edit`, `pin`, `unpin`. Used on: `apps.changes`.
- `apps.change.via` - Where the change came from: `web` (the plugin's HTML forms) or `agent` (datasette-agent tools). One of: `agent`, `web`. Used on: `apps.changes`.
- `apps.tool.result` - How one of this plugin's agent tools ended: `ok`, `error` or `denied` (the actor lacked permission). Set on the current tool span (datasette-agent's `execute_tool` span), not on a span of our own, so it sits in the `datasette_agent` scope; with a datasette-agent that has no telemetry it lands on whatever span is current, or nowhere. Set only when the tool returns: an exception escaping the tool leaves it unset (a datasette-agent version with telemetry records `error.type` on its own span). One of: `denied`, `error`, `ok`. Used on: the agent tool span.
- `apps.debug.outcome` - How an `app_debug` run ended. `ok`: the script ran and the envelope reported success. `script_error`: the script threw, or the harness reported an error. `timed_out`: the script hit its timeout. `task_expired` / `task_cancelled`: the browser task expired or was cancelled before a result came back. `denied`: the actor may not edit the app. `invalid`: the debug request was rejected (e.g. a bad viewport). `unsupported`: the agent host does not support browser tasks. Also set on the `app_debug` tool span alongside `apps.tool.result`. There is no `error` outcome: an exception escaping the tool records nothing. One of: `denied`, `invalid`, `ok`, `script_error`, `task_cancelled`, `task_expired`, `timed_out`, `unsupported`. Used on: `apps.debug.runs`, `apps.debug.run.duration`, `apps.debug.script.duration`, the agent tool span.
- `db.namespace` - Name of the database being queried. Used on: `apps.query`.
- `error.type` *(optional)* - The exception class name for a failed operation. On HTTP spans, also set to the status code as a string for 5xx responses. A 4xx response alone does not set this attribute or an error status. Used on: `apps.query`.

<!-- telemetry-reference:end -->

## Development

To set up this plugin locally, first checkout the code. You can confirm it is available like this:
```bash
cd datasette-apps
# Confirm the plugin is visible
uv run datasette plugins
```

To run the tests:
```bash
uv run pytest
```

After changing `datasette_apps/telemetry_registry.py`, regenerate the telemetry reference above (a test checks it is fresh):
```bash
uv run scripts/telemetry-doc.py
```

### Viewing telemetry locally

With a checkout of [datasette-otel-viewer](https://github.com/datasette/datasette-otel-viewer) next to this one, run Datasette with it installed, sign in with the `--root` link and open `/-/otel` to see the spans and metrics this plugin emits:
```bash
uv run --with ../datasette-otel-viewer datasette data.db --root
```
