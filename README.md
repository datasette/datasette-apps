# datasette-apps

[![PyPI](https://img.shields.io/pypi/v/datasette-apps.svg)](https://pypi.org/project/datasette-apps/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-apps?include_prereleases&label=changelog)](https://github.com/datasette/datasette-apps/releases)
[![Tests](https://github.com/datasette/datasette-apps/actions/workflows/test.yml/badge.svg)](https://github.com/datasette/datasette-apps/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-apps/blob/main/LICENSE)

Apps that live inside Datasette

## Installation

Install this plugin in the same environment as Datasette.
```bash
datasette install datasette-apps
```
## Usage

This plugin introduces a new interface at `/-/apps` for searching browsing available apps.

There are two types of app:

- HTML and JavaScript apps that are managed by this plugin, which run in a sandbox to prevent them from damaging or stealing your data (if they are buggy or malicious)
- Apps provided by other plugins that are written in Python and HTML/JavaScript which are unrestricted but can do more things

This plugin allows you to create and modify HTML apps, and provides a plugin hook to enable other plugins to add their own Python apps to the system.

- `/-/apps` lets you browse available apps
- `/-/apps/ULID` to interact with a full screen HTML app
- `/-/apps/create` for creating a new app
- `/-/apps/ULID/edit` to edit an existing app

Signed-in users get an "Apps" link in Datasette's top-right menu.

HTML apps managed by this plugin use lowercase monotonic ULIDs as their IDs and store every edit as a new row in `app_versions`.

Stored apps are rendered inside a sandboxed iframe. The plugin injects a Content Security Policy into the iframe `srcdoc`: direct network access is blocked unless the app has exact `https://` `connect-src` origins configured, and localhost origins are never allowed.

Stored apps can query Datasette data using the injected `datasette.query(database, sql, params)` helper. The iframe sends those requests to the parent page with `postMessage`, and the parent page forwards them to an app-scoped capability endpoint. Those queries are read-only and are limited to the intersection of the current actor's Datasette permissions and the app's own table/view/column grants.

The plugin registers Datasette permissions for `create-app`, `view-app`, `edit-app`, and `manage-app-access`. Stored app owners can view, edit, and manage their own apps; external apps registered by plugins are visible to signed-in users by default.

Signed-in users can pin apps from the catalog. Pinned apps appear first on `/-/apps`, and the three most recently used pinned apps are shown on the Datasette homepage using `top_homepage()`.

The `/-/apps` catalog is searchable and paginated, using a `next` cursor in the URL for subsequent pages.

The create page includes a copyable prompt for an LLM. The prompt explains the sandbox, the `datasette.query()` bridge, CSP restrictions, and includes a schema summary limited to tables and views the current actor can see.

The create and edit pages use Datasette's existing bundled CodeMirror editor for the HTML source textarea.

Other plugins can expose server-backed capabilities to stored HTML apps by implementing `register_app_capabilities(datasette)` and returning `AppCapability` objects. Non-built-in capability grants are stored with raw JSON configuration in phase one.

The edit page includes explicit controls for app access (private, signed-in users, or specific actor IDs), read-only table/view grants, allowed network origins, and raw JSON capability grants.

Plugins can add their own apps to the central catalog during startup:

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

## Development

To set up this plugin locally, first checkout the code. You can confirm it is available like this:
```bash
cd datasette-apps
# Confirm the plugin is visible
uv run datasette plugins
```
This repository's `uv` configuration uses the sibling `../datasette` checkout in editable mode for local development.

To run the tests:
```bash
uv run pytest
```
