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
- `/-/apps/panels/add` to pin an app to the top of a page - see [Panels](#panels-apps-pinned-to-pages-experimental)

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

The plugin registers Datasette permissions for `create-app`, `view-app`, `edit-app`, `delete-app`, `manage-app-access`, `apps-set-csp`, and `apps-share-panels`. Stored app owners can always view, edit, delete, and manage their own apps. Apps marked private are visible only to their owner, even if other users have broad `view-app` permission grants.

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

### Panels: apps pinned to pages (experimental)

A stored app can be pinned as a **panel**: a framed, sized copy of the app shown at the top of the homepage, a database page or a table page. A small app - a headline number, a sparkline, a chart - becomes a dashboard tile for the data it describes.

To add one, use **Add app panel** in the homepage, database or table actions menu, or **Pin to a page** on an app's own page. Each panel has:

- a **width**, as a span on a 12 column grid: full, two thirds, half, one third or one quarter. Panels on the same page flow left to right and wrap, so four quarter-width tiles make a row. On narrow screens they stack.
- a **height** in pixels, from 40 to 1200.
- a position, and whether the app name is shown above the frame.

Hover a panel for its **Open** and **Settings** links; settings is where a panel is resized, reordered or removed.

Panels are personal by default: only the user who pinned one sees it. Users with the `apps-share-panels` permission can mark a panel as shared, which shows it to everyone who can view the app (a private app stays visible only to its owner):

```yaml
permissions:
  apps-share-panels:
    id: admin
```

A panel is the same sandboxed iframe as the full app page, with the same CSP and the same data access rules: queries run as the person viewing the page, against the app's allow-listed databases. Pages that show panels are served with `Content-Security-Policy: frame-src 'none'`, as the app page is, so a panel cannot navigate its own frame to another site.

Inside a pinned app, `datasette.context` describes where it is running - `{target: "table", database: "shop", table: "orders", panel: {width, height}}`, or `{target: "app"}` on the app's own page - and `datasette.resize(height)` asks the host page for a different frame height.

#### Panels that follow the page's filters

A panel pinned to a table page can optionally be told about the filters currently applied to that page, so a chart above the table shows the same rows the table does. This is off by default and is switched on per panel with **Let this app see the page's current filters** when pinning, or later in the panel's settings.

With it on, the app gets:

- `datasette.context.filters` - `{query_string, params, active}`. `query_string` is the page's raw query string, `params` is the list of `[key, value]` pairs that actually filter rows (column filters, `_search`, `_where`, `_through`), and `active` says whether there are any.
- `await datasette.pageQuery(sql, params)` - runs read-only SQL in which `{{filtered}}` stands for the table with the page's filters applied:

  ```javascript
  const result = await datasette.pageQuery(
    "select status, count(*) as n from {{filtered}} group by 1"
  );
  ```

- `await datasette.pageRows({size})` - the rows the page is showing, in the page's sort order, as `{columns, rows, count}` (up to 100 rows).

Datasette's table pages reload when a filter changes, so the panel always loads with the current filters. Page size, pagination, sort order and column selection are ignored when working out `{{filtered}}`: it is every row matching the filters.

The filtered query is built by Datasette itself - the server asks the table's own JSON API for the SQL behind the page and substitutes that for `{{filtered}}` - so it matches the page exactly and runs with the viewer's permissions. It grants no new access: the table's database must already be on the app's list of SQL query databases, and the page says which panel is asking, not the app.

Panel templates understand `{{filtered}}` in `CONFIG.sql`. When the panel does not have access to the page's filters, or the app is opened on its own page, `{{filtered}}` falls back to the whole table (`CONFIG.table`). A small "Filtered" badge appears next to the label whenever the numbers reflect a filter. Templates started from a table's **Add app panel** page use `{{filtered}}` and have filter-following ticked by default.

#### Panel templates

The create page offers a gallery of **panel templates**, so that panels made by different people (and by agents) look like one family:

| Template | Shows | The query returns |
|---|---|---|
| `stat` | One headline number, with an optional change | one row: `value`, optional `previous` |
| `stat-row` | A strip of up to six numbers | one row of named columns, or rows of `label`, `value` |
| `sparkline` | A number above a sparkline | rows of `x`, `value`, oldest first |
| `timeseries` | A line chart with gridlines and a hover readout | rows of `x`, `value`, oldest first |
| `bars` | Ranked horizontal bars | rows of `label`, `value` |
| `recent` | A compact list of the latest rows | rows of `title`, optional `subtitle`, `meta` |
| `meter` | Progress towards a goal | one row: `value`, optional `goal` |
| `calendar` | A heatmap of activity by day | rows of `day`, `value` |
| `note` | A text callout | no query |

Each template is an ordinary self-contained app. It starts with a `CONFIG` block - label, database, SQL, number format - which is the only part that normally needs editing, followed by a shared "panel kit" of styles and helpers that is identical in every template. Writing `{{table}}` in the SQL means "the table this panel is pinned to", so one app can be pinned to several tables, and `{{filtered}}` means that table narrowed to the page's current filters.

Starting a template from a table's **Add app panel** page pre-fills the query from that table's columns (a date-like column to trend over, a low-cardinality text column to group by) and pins the new app back to the table when it is created.

`/-/apps/templates.json` lists the templates and their settings.

If [datasette-agent](https://github.com/datasette/datasette-agent) is installed the agent gets matching tools: `app_panel_templates` lists the templates, `app_create` accepts `template` and `template_config` in place of `html`, and `app_pin_panel` / `app_unpin_panel` place and remove panels (`share_filters` makes a table panel follow the page's filters). Asking "put a chart of orders per month at the top of the orders table" then costs the agent one SQL query and a few settings rather than a page of HTML.

To try all of this against sample data:

```bash
uv run python demo/panels_demo.py /tmp/panels-demo
uv run datasette /tmp/panels-demo/shop.db --internal /tmp/panels-demo/internal.db \
    -c /tmp/panels-demo/datasette.yaml --root
```

### App authoring

The create and edit pages include a copyable prompt for an LLM. The prompt is assembled in the browser from the current form state, so unsaved changes to selected databases, stored queries, and network origins are reflected immediately. It explains the sandbox, the `datasette.query()` and `datasette.storedQuery()` bridges, CSP restrictions, the current schema summary, and only the stored queries selected for that app.

The create and edit pages use Datasette's existing bundled CodeMirror editor for the HTML source textarea.

The edit page includes a private checkbox, SQL query database access, stored query access, and allowed network origins. The network origins control is a free-form list for users with the `apps-set-csp` permission, a set of checkboxes when an `allowed_csp_origins` allow-list is configured, and hidden otherwise.

### Tagging apps in datasette-agent

If [datasette-agent](https://github.com/datasette/datasette-agent) is installed, apps can be tagged with `#` in a chat message. The autocomplete lists the apps you have permission to view, including external apps. The agent is told the tagged app's ID and URL and whether you can edit it. Its HTML source is not attached: the agent reads that using the `app_view` tool, which requires `edit-app` permission.

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
