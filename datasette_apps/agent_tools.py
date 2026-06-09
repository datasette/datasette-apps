from __future__ import annotations

import html
import json

from datasette_agent_edit import EditError, EditToolset, Editable, NotFound

from .permissions import AppResource, AppsResource
from .registry import Registry

APP_RUNTIME_API_GUIDANCE = (
    " Apps run in a sandboxed iframe. For data access, use "
    "await datasette.query(database, sql, params?) for read-only SQL against "
    "allow-listed databases, or await datasette.storedQuery(database, query, "
    "params?) for allow-listed stored queries. params is an optional object of "
    "named SQL parameters, for example {id: 1} for SQL containing :id. These "
    "awaitables resolve to an object, not an array: "
    "{columns: [...], rows: [{...}, ...]}. Access row data as "
    "result.rows[0].column_name, for example: const result = await "
    'datasette.query("main", "select count(*) as count from sqlite_master"); '
    "const count = result.rows[0].count. Do not use result[0]."
)


APP_TOOL_DESCRIPTIONS = {
    "create": (
        "Create a new stored Datasette HTML app. Use this when the user asks "
        "you to build a new app and you do not already have an app_id."
        + APP_RUNTIME_API_GUIDANCE
    ),
    "view": (
        "View the current HTML source for a stored Datasette app, with "
        "line numbers. Requires edit permission for that app."
    ),
    "str_replace": (
        "Edit a stored Datasette app by replacing one exact, unique "
        "string in its HTML source. Requires edit permission for that app."
        + APP_RUNTIME_API_GUIDANCE
    ),
    "insert": (
        "Edit a stored Datasette app by inserting text after a line "
        "number in its HTML source. Requires edit permission for that app."
        + APP_RUNTIME_API_GUIDANCE
    ),
    "edit": (
        "Apply a batch of HTML source edits to a stored Datasette app. "
        "Each edit is a str_replace or insert operation, and the batch "
        "is saved as one app revision." + APP_RUNTIME_API_GUIDANCE
    ),
    "render": "Render links to the current stored Datasette app after editing.",
}


def _actor_id(actor):
    return str(actor.get("id") or "") if actor else None


async def _can_edit_app(datasette, actor, app_id):
    return await datasette.allowed(
        action="edit-app", resource=AppResource(app_id), actor=actor
    )


async def _can_create_app(datasette, actor):
    return await datasette.allowed(
        action="create-app", resource=AppsResource(), actor=actor
    )


class StoredAppHtmlStore:
    """Expose stored app HTML as a datasette-agent-edit store."""

    def __init__(self, datasette, actor):
        self.datasette = datasette
        self.actor = actor
        self.registry = Registry(datasette)

    async def create(self, content, *, ref=None, metadata=None):
        raise NotImplementedError("Use the datasette-apps create page to create apps")

    async def read(self, ref):
        app = await self.registry.get_app(ref)
        if app is None or app["external"]:
            raise NotFound(ref)
        version = await self.registry.get_current_version(ref)
        if version is None:
            raise NotFound(ref)
        return Editable(
            ref=ref,
            content=version["html"],
            metadata={
                "app_id": ref,
                "name": app["name"],
                "description": app["description"],
                "path": self.datasette.urls.path(app["path"]),
                "version": version["version"],
            },
            version=version["version"],
        )

    async def edit(self, ref, transform):
        try:
            version = await self.registry.edit_stored_app_html(
                ref, transform, actor_id=_actor_id(self.actor)
            )
        except KeyError as e:
            raise NotFound(ref) from e
        except ValueError as e:
            if isinstance(e, EditError):
                raise
            raise NotFound(ref) from e
        if version is None:
            raise NotFound(ref)
        app = await self.registry.get_app(ref)
        return Editable(
            ref=ref,
            content=version["html"],
            metadata={
                "app_id": ref,
                "name": app["name"],
                "description": app["description"],
                "path": self.datasette.urls.path(app["path"]),
                "version": version["version"],
            },
            version=version["version"],
        )

    async def delete(self, ref):
        raise NotImplementedError("Use the datasette-apps delete page to delete apps")


def _render_app(editable):
    metadata = editable.metadata
    name = html.escape(metadata.get("name") or metadata["app_id"])
    path = html.escape(metadata["path"], quote=True)
    edit_path = html.escape(f"{metadata['path']}/edit", quote=True)
    version = html.escape(str(editable.version))
    return {
        "_html": (
            '<div class="datasette-app-agent-tool">'
            '<span class="datasette-app-agent-tool-status">'
            f"<strong>{name}</strong> updated to v{version}."
            "</span>"
            '<span class="datasette-app-agent-tool-actions">'
            f'<a class="datasette-app-button" href="{path}">View app</a>'
            f'<a class="datasette-app-button" href="{edit_path}">Edit</a>'
            "</span>"
            "</div>"
        ),
    }


def _render_created_app(app, view_path, edit_path):
    name = html.escape(app["name"] or app["id"])
    path = html.escape(view_path, quote=True)
    edit_path = html.escape(edit_path, quote=True)
    return (
        '<div class="datasette-app-agent-tool">'
        '<span class="datasette-app-agent-tool-status">'
        f"<strong>{name}</strong> created."
        "</span>"
        '<span class="datasette-app-agent-tool-actions">'
        f'<a class="datasette-app-button" href="{path}">View app</a>'
        f'<a class="datasette-app-button" href="{edit_path}">Edit</a>'
        "</span>"
        "</div>"
    )


def _toolset(datasette, actor):
    return EditToolset(
        StoredAppHtmlStore(datasette, actor),
        name_prefix="app",
        id_field="app_id",
        render=_render_app,
        descriptions=APP_TOOL_DESCRIPTIONS,
    )


def _error(message, app_id=None):
    payload = {"error": message}
    if app_id is not None:
        payload["app_id"] = app_id
    return json.dumps(payload)


async def _app_create(
    datasette,
    actor,
    name,
    html,
    description="",
    is_private=True,
    sql_databases=None,
    stored_queries=None,
    csp_origins=None,
):
    if not await _can_create_app(datasette, actor):
        return _error("Permission denied: create-app")
    try:
        app = await Registry(datasette).create_stored_app(
            actor_id=_actor_id(actor),
            name=name or "Untitled app",
            description=description or "",
            html=html or "",
            is_private=True if is_private is None else bool(is_private),
            sql_databases=sql_databases or [],
            stored_queries=stored_queries or [],
            csp_origins=csp_origins or [],
        )
    except ValueError as e:
        return _error(str(e))
    return json.dumps(
        {
            "app_id": app["id"],
            "name": app["name"],
            "version": app["current_version"],
            "status": (
                "Created app. The user can open it with the rendered View app "
                "link above."
            ),
            "_html": _render_created_app(
                app,
                datasette.urls.path(app["path"]),
                datasette.urls.path(f"{app['path']}/edit"),
            ),
        }
    )


async def _with_app_edit_permission(datasette, actor, app_id, callback):
    if not await _can_edit_app(datasette, actor, app_id):
        return _error("Permission denied: edit-app", app_id=app_id)
    return await callback()


def get_app_edit_tools(AgentTool):
    async def app_view(datasette, actor, app_id, view_range=""):
        return await _with_app_edit_permission(
            datasette,
            actor,
            app_id,
            lambda: _toolset(datasette, actor).view(app_id, view_range),
        )

    async def app_str_replace(datasette, actor, app_id, old_str, new_str):
        return await _with_app_edit_permission(
            datasette,
            actor,
            app_id,
            lambda: _toolset(datasette, actor).str_replace(app_id, old_str, new_str),
        )

    async def app_insert(datasette, actor, app_id, insert_line, insert_text):
        return await _with_app_edit_permission(
            datasette,
            actor,
            app_id,
            lambda: _toolset(datasette, actor).insert(app_id, insert_line, insert_text),
        )

    async def app_edit(datasette, actor, app_id, edits):
        return await _with_app_edit_permission(
            datasette,
            actor,
            app_id,
            lambda: _toolset(datasette, actor).edit(app_id, edits),
        )

    async def app_render(datasette, actor, app_id):
        return await _with_app_edit_permission(
            datasette,
            actor,
            app_id,
            lambda: _toolset(datasette, actor).render(app_id),
        )

    return [
        AgentTool(
            name="app_create",
            description=APP_TOOL_DESCRIPTIONS["create"],
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short display name for the app",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional one-sentence app description",
                    },
                    "html": {
                        "type": "string",
                        "description": "Complete HTML source for the new app",
                    },
                    "is_private": {
                        "type": "boolean",
                        "description": "Whether the app should be private; defaults to true",
                    },
                    "sql_databases": {
                        "type": "array",
                        "description": (
                            "Optional Datasette database names this app may query "
                            "with datasette.query()"
                        ),
                        "items": {"type": "string"},
                    },
                    "stored_queries": {
                        "type": "array",
                        "description": (
                            "Optional stored queries this app may call, as "
                            "database/query strings"
                        ),
                        "items": {"type": "string"},
                    },
                    "csp_origins": {
                        "type": "array",
                        "description": (
                            "Optional exact https:// origins this app may contact "
                            "for scripts, styles, images, and fetch requests"
                        ),
                        "items": {"type": "string"},
                    },
                },
                "required": ["name", "html"],
            },
            fn=_app_create,
        ),
        AgentTool(
            name="app_view",
            description=APP_TOOL_DESCRIPTIONS["view"],
            input_schema={
                "type": "object",
                "properties": {
                    "app_id": {
                        "type": "string",
                        "description": "The stored Datasette app ID",
                    },
                    "view_range": {
                        "type": "string",
                        "description": (
                            'Optional line range "start,end" '
                            "(1-indexed, -1 for end-of-file)"
                        ),
                    },
                },
                "required": ["app_id"],
            },
            fn=app_view,
        ),
        AgentTool(
            name="app_str_replace",
            description=APP_TOOL_DESCRIPTIONS["str_replace"],
            input_schema={
                "type": "object",
                "properties": {
                    "app_id": {
                        "type": "string",
                        "description": "The stored Datasette app ID",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "Exact text to find; must appear once",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement text",
                    },
                },
                "required": ["app_id", "old_str", "new_str"],
            },
            fn=app_str_replace,
        ),
        AgentTool(
            name="app_insert",
            description=APP_TOOL_DESCRIPTIONS["insert"],
            input_schema={
                "type": "object",
                "properties": {
                    "app_id": {
                        "type": "string",
                        "description": "The stored Datasette app ID",
                    },
                    "insert_line": {
                        "type": "integer",
                        "description": "Line number after which to insert; 0 for beginning",
                    },
                    "insert_text": {
                        "type": "string",
                        "description": "Text to insert",
                    },
                },
                "required": ["app_id", "insert_line", "insert_text"],
            },
            fn=app_insert,
        ),
        AgentTool(
            name="app_edit",
            description=APP_TOOL_DESCRIPTIONS["edit"],
            input_schema={
                "type": "object",
                "properties": {
                    "app_id": {
                        "type": "string",
                        "description": "The stored Datasette app ID",
                    },
                    "edits": {
                        "type": "array",
                        "description": "Edit operations applied sequentially",
                        "items": {
                            "type": "object",
                            "properties": {
                                "operation": {
                                    "type": "string",
                                    "enum": ["str_replace", "insert"],
                                },
                                "old_str": {"type": "string"},
                                "new_str": {"type": "string"},
                                "insert_line": {"type": "integer"},
                                "insert_text": {"type": "string"},
                            },
                            "required": ["operation"],
                        },
                    },
                },
                "required": ["app_id", "edits"],
            },
            fn=app_edit,
        ),
        AgentTool(
            name="app_render",
            description=APP_TOOL_DESCRIPTIONS["render"],
            input_schema={
                "type": "object",
                "properties": {
                    "app_id": {
                        "type": "string",
                        "description": "The stored Datasette app ID",
                    },
                },
                "required": ["app_id"],
            },
            fn=app_render,
        ),
    ]
