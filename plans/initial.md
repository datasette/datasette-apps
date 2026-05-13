# Initial plan

This is the first implementation phase for `datasette-apps`. The goal is to build the full initial version of the plugin described in the README: a Datasette interface for creating, browsing, searching, editing, and running sandboxed HTML/JavaScript apps, plus a central registry that other plugins can use to advertise their own unrestricted apps.

## Product shape

`datasette-apps` provides an app catalog at `/-/apps`.

There are two app sources, both represented in the same internal `apps` table:

- Stored apps: HTML/JavaScript apps created and edited through this plugin. These have `external = 0`, are versioned in `app_versions`, and are rendered in sandboxed iframes.
- External apps: apps provided by other Datasette plugins. These have `external = 1`, are registered in the central catalog using the `Registry` helper, and are otherwise owned by the plugin that serves them.

For phase one, stored apps are HTML apps only. We will not bring forward the old `markdown` and `svg` artifact types unless we decide they still fit the new product language.

The catalog needs to comfortably support hundreds or thousands of apps across both sources, so listing and search should be database-backed and paginated from the beginning.

## User-facing routes

- `GET /-/apps`: browse/search available apps from both sources. Requires a signed-in actor in phase one.
- `GET /-/apps/create`: form for creating a stored HTML app, including name, description, HTML source, and an LLM prompt helper for generating app HTML.
- `POST /-/apps/create`: create a stored HTML app and redirect to it.
- `GET /-/apps/{id}`: run a stored app full-screen and record access for the current actor.
- `GET /-/apps/{id}/launch`: record access for an external app and redirect to `apps.path`. Stored apps can use this route as a compatibility redirect, but direct stored app views are the canonical access-tracking point.
- `GET /-/apps/{id}/edit`: edit a stored app's name, description, HTML source, access rules, data grants, capabilities, and network allow-list.
- `POST /-/apps/{id}/edit`: save a new version of a stored app and redirect to it.
- `GET /-/apps/{id}.json`: JSON API for a stored app.

External apps appear in `/-/apps` using the path supplied by their owning plugin. They do not need to live under `/-/apps` in phase one, and they are visible by default to signed-in users unless the owning plugin or an app access rule restricts them later.

Catalog links for stored apps should point directly at `/-/apps/{id}`. Catalog links for external apps should point at `/-/apps/{id}/launch`, so `datasette-apps` can record that the current actor accessed the app before redirecting.

`GET /-/apps` should accept pagination and search parameters, likely:

- `q`: search text.
- `page` or `next`: pagination cursor.
- `type`: optional filter for stored/external/all.

## App registry data model

Use Datasette's internal database.

```sql
CREATE TABLE IF NOT EXISTS apps (
    id TEXT PRIMARY KEY,
    external INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    actor_id TEXT,
    current_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (external IN (0, 1))
);

CREATE TABLE IF NOT EXISTS app_versions (
    app_id TEXT NOT NULL REFERENCES apps(id),
    version INTEGER NOT NULL,
    html TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (app_id, version)
);

CREATE INDEX IF NOT EXISTS idx_apps_updated ON apps(updated_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_apps_external_updated ON apps(external, updated_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_apps_source ON apps(source);

CREATE VIRTUAL TABLE IF NOT EXISTS apps_fts
USING fts5(name, description, source, content='apps', content_rowid='rowid');

CREATE TRIGGER IF NOT EXISTS apps_ai AFTER INSERT ON apps BEGIN
    INSERT INTO apps_fts(rowid, name, description, source)
    VALUES (new.rowid, new.name, new.description, new.source);
END;

CREATE TRIGGER IF NOT EXISTS apps_ad AFTER DELETE ON apps BEGIN
    INSERT INTO apps_fts(apps_fts, rowid, name, description, source)
    VALUES ('delete', old.rowid, old.name, old.description, old.source);
END;

CREATE TRIGGER IF NOT EXISTS apps_au AFTER UPDATE ON apps BEGIN
    INSERT INTO apps_fts(apps_fts, rowid, name, description, source)
    VALUES ('delete', old.rowid, old.name, old.description, old.source);
    INSERT INTO apps_fts(rowid, name, description, source)
    VALUES (new.rowid, new.name, new.description, new.source);
END;

CREATE TABLE IF NOT EXISTS app_access (
    id INTEGER PRIMARY KEY,
    app_id TEXT REFERENCES apps(id),
    action TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT,
    allow INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (subject_type IN ('authenticated', 'actor')),
    CHECK (allow IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_app_access_lookup
    ON app_access(action, app_id, subject_type, subject_id);

CREATE TABLE IF NOT EXISTS app_sql_databases (
    app_id TEXT NOT NULL REFERENCES apps(id),
    database_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (app_id, database_name)
);

CREATE INDEX IF NOT EXISTS idx_app_sql_databases_app
    ON app_sql_databases(app_id, database_name);

CREATE TABLE IF NOT EXISTS app_csp_origins (
    id INTEGER PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES apps(id),
    directive TEXT NOT NULL DEFAULT 'connect-src',
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (directive IN ('connect-src')),
    UNIQUE (app_id, directive, origin)
);

CREATE INDEX IF NOT EXISTS idx_app_csp_origins_app
    ON app_csp_origins(app_id, directive);

CREATE TABLE IF NOT EXISTS app_capability_grants (
    app_id TEXT NOT NULL REFERENCES apps(id),
    capability TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (app_id, capability)
);

CREATE INDEX IF NOT EXISTS idx_app_capability_grants_capability
    ON app_capability_grants(capability, app_id);

CREATE TABLE IF NOT EXISTS app_user_state (
    actor_id TEXT NOT NULL,
    app_id TEXT NOT NULL REFERENCES apps(id),
    last_accessed_at TEXT,
    pinned_at TEXT,
    access_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (actor_id, app_id)
);

CREATE INDEX IF NOT EXISTS idx_app_user_state_actor_pinned
    ON app_user_state(actor_id, pinned_at DESC, last_accessed_at DESC, app_id);

CREATE INDEX IF NOT EXISTS idx_app_user_state_actor_recent
    ON app_user_state(actor_id, last_accessed_at DESC, app_id);
```

Stored app rows:

- `external`: `0`.
- `id`: raw lowercase monotonic ULID, using the same style as `llm.utils.monotonic_ulid()`.
- `name`: user-facing app name.
- `description`: catalog summary.
- `path`: generated path, e.g. `/-/apps/{id}`.
- `source`: `datasette-apps`.
- `metadata`: JSON object for future catalog metadata.
- `actor_id`: owner.
- `current_version`: current version in `app_versions`.

External app rows:

- `id`: stable plugin-owned ID, namespaced to avoid collisions, e.g. `myplugin:1`. Stored app IDs are ULIDs; plugins define external app IDs themselves.
- `external`: `1`.
- `name`: user-facing app name.
- `description`: catalog summary.
- `path`: launch path supplied by the plugin, e.g. `/-/myplugin-app-1`.
- `source`: plugin/source label.
- `metadata`: JSON object for plugin-supplied catalog metadata.
- `actor_id`: usually `NULL`.
- `current_version`: usually `NULL`.

Use keyset pagination rather than offset pagination for catalog pages. The main personalized catalog order is defined in the next section; non-personalized fallback ordering can use `apps.updated_at DESC, apps.id`.

Use FTS5 from the start for catalog search over `name`, `description`, and `source`, with triggers keeping `apps_fts` synchronized with `apps`. Search results still need to respect actor visibility and the personalized pinned/recent ordering.

## User app access and pins

`/-/apps` should require a signed-in actor in phase one, then personalize the catalog for that actor:

- Show only apps the actor can `view-app`.
- Order by pinned apps first.
- Within pinned apps, order by most recently accessed by that actor.
- Then show unpinned apps by most recently accessed by that actor.
- Apps the actor has never opened should still appear after accessed apps, using a stable fallback order such as `apps.updated_at DESC, apps.id`.

Pinned apps are not capped. If a user pins more than the page size, they page through pinned apps before unpinned apps appear. With a page size of 20, a user with 24 pinned apps sees 20 pinned apps on page 1 and the remaining 4 pinned apps at the top of page 2.

Track app access in `app_user_state`:

- `actor_id`: the actor ID. Personalization is only for signed-in actors in phase one.
- `app_id`: the app.
- `last_accessed_at`: updated when the app is opened.
- `pinned_at`: `NULL` unless pinned. Setting this timestamp pins the app; clearing it unpins the app.
- `access_count`: incremented when the app is opened.

Stored apps record access when `GET /-/apps/{id}` renders. External apps need the `/-/apps/{id}/launch` route so `datasette-apps` can record access before redirecting to the plugin-owned path.

Stored app IDs are raw lowercase monotonic ULIDs, as in `llm`. External plugin IDs used in launch URLs must still be safe as a single path segment; IDs containing `/` should be rejected or encoded by the registry API.

Pin/unpin routes can be small POST endpoints:

- `POST /-/apps/{id}/pin`
- `POST /-/apps/{id}/unpin`

The Datasette homepage should surface a compact set of pinned apps for the signed-in actor using `top_homepage()`. First version: show three app cards, using the pinned apps with the most recent `last_accessed_at`, falling back to `pinned_at` for pinned apps that have not been opened yet.

## App data access model

The most important job of a stored HTML app is to provide a clear, finely grained interface to data. App access is separate from human access:

- Human access answers: "Can this actor open, edit, or manage this app?"
- Data access answers: "What databases, tables, views, and columns is this app allowed to read?"

Do not let stored app HTML call Datasette's generic query JSON API directly. The bridge should call an app-scoped capability endpoint owned by `datasette-apps`:

- `POST /-/apps/{id}/capabilities/datasette.query`: run read-only SQL from a stored app.

Every app data request should check all of:

- the current actor can `view-app` for that app, and
- the app's own SQL database allow-list includes the requested database.

The actual SQL execution should be delegated to Datasette's own read-only query JSON API as the current actor. That means Datasette enforces `execute-sql`, SQL validation, query time limits, row truncation, and any other normal query behavior.

Phase-one data access:

- `sql-database`: allows `datasette.query()` to send read-only SQL to one named Datasette database.

Example:

- An app allowed to query `_memory` and `content` gets two `app_sql_databases` rows. If the viewing actor can normally execute SQL against `content` but not `_memory`, Datasette's query API denies `_memory`.

The Data access UI should list databases visible to the editing actor as checkbox options. Users can select none, one, or more databases.

`datasette.executeQuery()` and canned write-query execution are deliberately not in day one. The schema and capability design should leave an obvious later path for a `canned-query-execute` permission type, but the first implementation only exposes `datasette.query()`.

## App capabilities

Add a plugin hook for defining capabilities that stored HTML apps can request through the parent page using `postMessage`. A capability is a named operation exposed to app JavaScript, mediated by `datasette-apps`, and optionally implemented by another plugin.

This is different from external app registration:

- External app registration writes app rows into `apps`.
- Capability registration defines a small set of operations that stored HTML apps may request.

Built-in examples:

- `datasette.query`: read-only SQL, backed by the app's SQL database allow-list and Datasette's query JSON API.

Possible plugin examples:

- `llm.prompt`: send a prompt to a configured LLM purpose.
- `image.generate`: generate an image through an installed image plugin.
- `github.issueSearch`: query GitHub using plugin-managed credentials.

Hook sketch:

```python
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class AppCapability:
    name: str
    description: str
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    default_enabled: bool = False
    config_schema: dict[str, Any] | None = None
    handler: Callable[..., Awaitable[dict[str, Any]]] | None = None


@hookspec
def register_app_capabilities(datasette):
    "Return AppCapability objects."
```

Capability names should be globally unique and namespaced when they come from plugins, e.g. `llm.prompt`, `github.issueSearch`, `myplugin.doThing`. Built-in capabilities can use the `datasette.` prefix.

Capability names must be safe as a single URL path segment because they appear in `/-/apps/{id}/capabilities/{capability}`. Reject names containing `/`, `?`, `#`, or whitespace.

Handler signature:

```python
async def handler(datasette, request, app, actor, input, config):
    ...
```

- `request`: the app-scoped capability request.
- `app`: the row from `apps`.
- `actor`: the current actor.
- `input`: JSON payload from the sandbox.
- `config`: JSON-decoded grant config from `app_capability_grants`.

Every capability request should flow through one app-scoped endpoint:

- `POST /-/apps/{id}/capabilities/{capability}`

The endpoint should:

1. Check the current actor can `view-app` for the app.
2. Look up the registered capability descriptor.
3. Check the app has a grant for that capability unless `default_enabled=True`.
4. Validate the JSON input against `input_schema` if provided.
5. Call the capability handler.
6. Return JSON shaped as either `{ok: true, result: ...}` or `{ok: false, error: ...}`.

The sandbox bridge should expose a generic API:

```javascript
const result = await datasette.request("llm.prompt", {
  prompt: "Summarize this table"
});
```

Built-in convenience wrappers can sit on top:

```javascript
await datasette.query("main", "select * from stats limit 10");
```

Under the hood these wrappers should send `postMessage` requests to the parent page. The parent page then forwards them to `/-/apps/{id}/capabilities/...` using the current actor's session.

Capability grants should be visible in the app edit UI as explicit rows:

```text
Capabilities

Enabled capability     Configuration
datasette.query        Read stats, news
llm.prompt             Purpose: summarization
```

Capability plugins can optionally provide a short HTML form fragment or a JSON-schema-driven config UI later, but phase one can store raw JSON config for non-built-in capabilities while rendering name, description, enabled/disabled state, and config text.

Security rules:

- Capabilities are denied by default.
- Capability requests are always associated with one app ID and one actor.
- A capability grant is not enough by itself if the capability has deeper resource controls. For example, `datasette.query` still needs table-level app data permissions.
- Plugins should never need to inject arbitrary parent-page JavaScript for server-backed capabilities.
- Capability handlers should return JSON-serializable data only.
- Errors should be reported back to the iframe without leaking secrets.

## Per-app network access

Stored apps should default to no direct network access. Some apps will need carefully scoped outbound requests to public APIs, for example GitHub or iNaturalist. Support this with per-app CSP allow-lists rather than broad network permissions.

Phase-one scope:

- Support `connect-src` allow-list entries for exact origins.
- Example allowed origins: `https://api.github.com`, `https://api.inaturalist.org`.
- Do not support wildcard hosts, arbitrary schemes, or path-based rules in phase one.
- Continue to disallow direct access back to Datasette itself unless we deliberately add an origin for it later.

Default CSP:

```text
default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:;
```

With two allowed API origins:

```text
default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; connect-src https://api.github.com https://api.inaturalist.org;
```

Important browser behavior:

- CSP controls whether the app may attempt the request.
- CORS still controls whether the browser exposes the response to JavaScript.
- Allowing an origin does not bypass that API's CORS policy.

The app editing UI should show network access as an explicit list:

```text
Network access

Allowed fetch() origins:
  https://api.github.com
  https://api.inaturalist.org
```

The UI should make the default "No external network access" obvious, and should validate entries as exact `https://` origins.

Origin validation should parse and normalize entries before saving:

- require `https://`,
- require a host,
- allow an explicit port,
- reject paths, query strings, fragments, usernames, passwords, wildcards, and localhost origins,
- store the normalized origin without a trailing slash.

## App source editor

Use CodeMirror for stored app HTML/JavaScript editing in phase one. Datasette already bundles CodeMirror for SQL editing, so this should reuse existing Datasette static assets rather than introduce a new dependency.

Relevant Datasette assets/templates:

- `/-/static/cm-editor-6.0.1.bundle.js`
- `datasette/templates/_codemirror.html`
- `datasette/templates/_codemirror_foot.html`

Use the existing bundled CodeMirror integration for the moment. Do not rebuild the bundle or introduce a new mode in the first implementation pass; a plain code editor is acceptable if that is what the current bundle supports.

Phase-one requirements:

- Replace the plain `<textarea>` for app source with CodeMirror.
- Keep the underlying textarea synchronized on form submit.
- Make the editor comfortable for full HTML documents: large height, monospace, line wrapping optional, no tiny default SQL editor dimensions.
- Fall back to the textarea if CodeMirror fails to load.
- Do not add a new npm/package dependency or rebuild CodeMirror in phase one.

## Create-page LLM prompt helper

The `/-/apps/create` page should include a detailed prompt intended for copying into an LLM. This helps users generate a complete HTML app that works inside the `datasette-apps` sandbox.

The prompt should be shown in a copyable block with a copy-to-clipboard button. It should be generated dynamically for the current Datasette instance and actor.

The prompt should explain:

- The app must be a complete HTML document, including `<!DOCTYPE html>`, CSS, and JavaScript in one file.
- The app runs in a sandboxed iframe with strict CSP.
- Direct network access is disabled by default.
- The app cannot fetch from Datasette, localhost, or arbitrary origins.
- External `fetch()` requests only work for exact origins explicitly granted in the app's network access settings, and CORS still applies.
- Database access must use the injected `datasette.query(database, sql, params?)` helper.
- `datasette.query()` can only run read-only SQL.
- Query access is limited to databases enabled for the app, plus the current actor's normal Datasette SQL permissions.
- `datasette.executeQuery()` is not available in phase one.
- Plugin-defined capabilities, if enabled for the app, are requested with `datasette.request(capabilityName, input)`.

The prompt should dump enough schema context for the LLM to write useful SQL:

- available databases,
- tables and views visible to the current actor,
- columns for each table/view,
- primary keys where available,
- foreign keys where available,
- row counts when cheap enough to obtain,
- available app data grants already selected on the create/edit form, if any.

The prompt should also include a small canonical example:

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Example Datasette app</title>
</head>
<body>
  <h1>Recent rows</h1>
  <pre id="output">Loading...</pre>
  <script>
  async function main() {
    const rows = await datasette.query(
      "main",
      "select * from example_table limit 10"
    );
    document.getElementById("output").textContent =
      JSON.stringify(rows, null, 2);
  }
  main().catch(error => {
    document.getElementById("output").textContent = String(error);
  });
  </script>
</body>
</html>
```

The schema dump should only include resources the current actor can see. It should not leak hidden tables, hidden columns, or data from rows. Keep the prompt readable, but make it detailed enough that copying it into an LLM produces an app that uses the right bridge APIs without guessing.

## Registry helper for external apps

Do not add a `register_apps()` hook. Plugin-provided apps should be registered by writing to the central `apps` table through a helper API exported by `datasette_apps`.

Example:

```python
from datasette import hookimpl
from datasette_apps import Registry


@hookimpl
async def startup(datasette):
    registry = Registry(datasette)
    await registry.add_app(
        id="myplugin:1",
        name="My plugin app",
        description="A useful app served by my plugin",
        path="/-/myplugin-app-1",
        source="myplugin",
    )
```

Plugins can remove stale entries:

```python
await Registry(datasette).remove_app("myplugin:1")
```

Proposed `Registry` phase-one API:

- `await Registry(datasette).ensure_tables()`
- `await Registry(datasette).add_app(id, name, description, path, source=None, metadata=None)`
- `await Registry(datasette).add_apps(apps, source=None)` for efficient bulk upserts
- `await Registry(datasette).remove_app(id)`
- `await Registry(datasette).remove_apps_for_source(source)` for plugin cleanup
- `await Registry(datasette).get_app(id)`
- `await Registry(datasette).list_apps(...)`
- `await Registry(datasette).record_access(actor_id, app_id)`
- `await Registry(datasette).set_pinned(actor_id, app_id, pinned)`

`add_app()` should upsert rows with `external = 1` and update `updated_at`, making startup registration idempotent. Registered external apps should be visible by default to signed-in users in `/-/apps`.

External apps remain responsible for their own routes, templates, behavior, and route-level permission checks. `datasette-apps` owns the searchable catalog entry.

## Permissions model

Use Datasette's resource-based permission system rather than a standalone `visibility` column.

Phase one has no anonymous app access. App catalog, stored app rendering, app editing, pinning, and app capability requests all require `request.actor`.

Register app-specific resources:

- `AppsResource("apps")`: the app collection, used for actions that apply to all apps.
- `AppResource("apps", app_id)`: a specific app, used for per-app checks.

Register app-specific actions:

- `create-app` on `AppsResource`
- `view-app` on `AppResource`
- `edit-app` on `AppResource`
- `manage-app-access` on `AppResource`

Routes should call `datasette.allowed()` or `datasette.ensure_permission()` with these resources. The catalog should list apps the actor can `view-app`, ideally by joining against `datasette.allowed_resources_sql(action="view-app", actor=request.actor, parent="apps")`.

The app access UI should write permission rows that feed `permission_resources_sql()`. This gives us friendly controls like "private", "signed-in", and "specific users" without making those modes the underlying permission system. Public/anonymous app access is reserved for a later phase.

Owner defaults can be emitted from `apps.actor_id`: owners should be granted `view-app`, `edit-app`, and `manage-app-access` for their stored apps. External apps should get a default `view-app` allow rule for signed-in actors unless an explicit app access rule says otherwise.

## Rendering and sandboxing

Adapt the useful parts of `datasette-artifacts`, renamed around apps:

- Render stored app HTML in an `<iframe sandbox="allow-scripts">`.
- Insert a CSP meta tag at the earliest safe point in `srcdoc`, preserving an optional doctype but ensuring no user-controlled element is parsed before the CSP meta tag.
- Set an iframe `csp` attribute as Chromium defense in depth.
- Inject a small resize script into the iframe.
- Include a parent-page bridge script for iframe resizing.
- Preserve the `datasette.query(database, sql, params?)` bridge for read-only SQL queries, but route it through app-scoped authorization.
- Do not expose `datasette.executeQuery()` in the first implementation pass.
- Support full HTML documents, including documents that start with `<!DOCTYPE html>`.

Default CSP for stored HTML apps:

```text
default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:;
```

The app iframe cannot fetch directly from the network or Datasette unless the app has explicit per-app CSP `connect-src` origins. Database access goes through the parent bridge, then through app-scoped server endpoints that apply both actor permission checks and app data permission checks.

### CSP research findings to apply

Consulted `~/dev/research/test-csp-iframe-escape/README.md`. The phase-one renderer should follow these findings:

- `sandbox="allow-scripts"` alone is not enough. A sandboxed iframe can navigate itself to a `data:` URI and make network requests unless CSP is also enforced.
- The CSP meta tag is the critical cross-browser protection. Once parsed, it cannot be removed or weakened by JavaScript, `document.write()`, or a later permissive CSP tag.
- The CSP meta tag restrictions persist across `data:` URI navigation in both Chromium and Firefox.
- The iframe `csp` attribute is not a substitute for the meta tag because Firefox ignores it. We should still include it as defense in depth for browsers that support it.
- The CSP meta tag must be the first parsed element in the iframe `srcdoc`, after an optional doctype. The renderer must insert it ahead of all user-controlled document elements, rather than relying on the app author to include it.
- `navigator.sendBeacon()` may return `true` even when CSP blocks the request, so manual/browser tests should check server/network evidence, not just JavaScript return values.

Implementation implication: `build_app_srcdoc()` owns the document security wrapper. Stored app HTML is a full document or fragment, and the renderer should insert the CSP meta tag using careful string replacement:

- If the source starts with `<!DOCTYPE html>`, preserve that doctype.
- If a `<head>` tag exists, insert the CSP meta tag as the first child of `<head>`.
- If no `<head>` tag exists, create one immediately after the optional doctype/opening `<html>` tag.
- Never place user-controlled tags before the CSP meta tag.

We should include tests for documents with a doctype, documents with an existing head, documents without a head, and malicious content before the first normal element.

## Proposed package structure

```text
datasette_apps/
  __init__.py
  capabilities.py
  csp.py
  data_access.py
  db.py
  hookspecs.py
  permissions.py
  registry.py
  rendering.py
  views.py
  static/
    apps.css
    app-bridge.js
  templates/
    app_list.html
    app_create.html
    app_edit.html
    app_view.html
tests/
  test_apps.py
  test_capabilities.py
  test_csp.py
  test_data_access.py
  test_registry.py
  test_permissions.py
  test_rendering.py
```

## Implementation sequence

1. Add the core DB helpers:
   - create tables
   - create stored app
   - add/remove external app
   - fetch current app version
   - save new version
   - list/search/paginate catalog apps
   - record per-actor app access
   - pin/unpin apps
   - add indexes needed by catalog queries
2. Add rendering helpers:
   - build `srcdoc`
   - inject `datasette.query()`
   - build CSP
   - wire up iframe resize bridge
3. Add web routes and templates:
   - catalog
   - create form
   - create-page LLM prompt helper with copy-to-clipboard
   - CodeMirror-backed source editor
   - full-screen app view
   - launch route for access tracking and external redirects
   - pin/unpin routes
   - edit form
   - generic app-scoped capability endpoint
   - JSON API
4. Add `Registry` helper support for external plugin apps.
5. Add Datasette resource/action permission integration:
   - `AppsResource`
   - `AppResource`
   - `create-app`, `view-app`, `edit-app`, `manage-app-access`
   - `permission_resources_sql()` rows for owner defaults and app access UI rows
6. Add app data access enforcement:
   - `app_sql_databases` helpers
   - delegate read-only SQL to Datasette's query JSON API
   - clear edit UI for selecting allowed databases
7. Add per-app network access controls:
   - `app_csp_origins` helpers
   - CSP builder that includes exact `connect-src` origins
   - clear edit UI for allowed fetch origins
   - validation for exact `https://` origins
8. Add plugin-defined app capabilities:
   - `register_app_capabilities()` hookspec
   - `AppCapability` descriptor
   - `app_capability_grants` helpers
   - generic `datasette.request()` bridge
   - built-in `datasette.query` capability and wrapper
   - capability grant UI
9. Add tests for:
   - plugin installation
   - route availability
   - create/edit/version behavior
   - CodeMirror assets are included on create/edit pages and preserve textarea submit behavior
   - create-page LLM prompt includes bridge instructions, CSP limits, and actor-visible schema
   - create-page LLM prompt does not leak tables/columns the actor cannot view
   - catalog search and pagination
   - FTS5 search over name, description, and source
   - catalog ordering by pinned first, then actor-specific recent access
   - direct stored app view records access
   - launch route records access for external apps
   - pin/unpin behavior across pagination
   - homepage pinned app surfacing
   - external app registry add/remove/upsert
   - Datasette permission checks
   - table/view-read grants allow permitted SELECT queries
   - actor table/view permissions are intersected with app data grants
   - unauthorized tables/views are denied, including through joins/subqueries
   - write SQL is denied through the `datasette.query` capability
   - denied-by-default capability requests
   - granted plugin capabilities receive app, actor, input, and config
   - capability input validation failures
   - CSP defaults to no `connect-src`
   - configured CSP origins appear in the meta tag and iframe `csp` attribute
   - invalid CSP origins are rejected
   - sandbox/CSP rendering
   - CSP meta tag appears before stored app HTML
   - SQL bridge script injection

## Borrow from prototypes

From `datasette-agent-artifacts`:

- iframe resize script
- SQL bridge design
- versioned content editing approach
- exact-string and insert helpers, if we still want agent-friendly editing later

From `datasette-artifacts`:

- internal DB structure and helper separation
- CSP-aware rendering
- route/template split
- tests for route behavior and permissions

## Explicit non-goals for phase one

- Agent tool registration.
- LLM prompt bridge.
- `datasette.executeQuery()` and canned write-query execution.
- Markdown and SVG app types.
- App screenshots/previews.
- Forking, copying, publishing, or advanced sharing workflows.
- Plugin apps hosted directly by `datasette-apps`.
- Proving new CSP bypass behavior beyond preserving the researched safeguards in tests.

## Settled decisions

- Stored apps use raw lowercase monotonic ULIDs.
- Plugins define their own external app IDs.
- CSP network allow-lists require exact `https://` origins and must not allow localhost, even during development.
- Plugin-defined capability configuration can use raw JSON in phase one.
