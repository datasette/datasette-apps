from __future__ import annotations

import html
import json

from datasette.resources import QueryResource
from datasette_agent_edit import EditError, EditToolset, Editable, NotFound

from .csp import configured_csp_allowlist, resolve_csp_origins
from .debug import (
    BROWSER_TASK_SLACK_MS,
    build_debug_harness_html,
    cap_envelope,
    complete_debug_job,
    create_debug_job,
    debug_task_payload,
    get_debug_job_for_call,
    normalize_envelope,
)
from .permissions import AppResource, AppsResource
from .registry import Registry, _fts_query

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
    "list": (
        "List or search stored Datasette apps that the current user can edit. "
        "Use this before app_view or app_edit when the user identifies an app "
        "by name or description but no app_id is known. Pass only identifying "
        "words from the app name or description; omit query to list recently "
        "used editable apps. If several apps plausibly match, ask the user "
        "which one. If none match, do not assume the user wants a new app."
    ),
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
    "add_stored_queries": (
        "Give an existing stored Datasette app access to one or more stored "
        "queries, so its HTML can call them with "
        "await datasette.storedQuery(database, query, params?). Pass each "
        "query as a database/query string. The queries must already exist "
        "(use save_query first to create a new one). Read-only queries are "
        "granted immediately. Write queries (INSERT/UPDATE/DELETE) let the "
        "app modify data as whoever views it, so the user is shown the SQL "
        "and asked to approve first; if they decline, do not retry unless "
        "they ask you to. Requires edit permission for that app."
    ),
    "remove_stored_queries": (
        "Remove stored query access from an existing stored Datasette app. "
        "Pass each query as a database/query string. Requires edit "
        "permission for that app."
    ),
    "render": "Render links to the current stored Datasette app after editing.",
    "debug": (
        "Run a JavaScript debug script inside a live sandboxed render of a "
        "stored Datasette app and return its result plus every error and log "
        "the app produced while rendering. The app's most recent revision "
        "loads invisibly in the user's open browser tab, so this requires an "
        "interactive conversation and edit permission for that app. The "
        "javascript argument is the body of an async function executed in "
        "the app's global scope; its awaited return value is the result and "
        "must be JSON-serializable - return .textContent or measurements, "
        "not DOM elements. Apps render asynchronously, so wait for readiness "
        "with await debug.waitFor(fn, {timeout, interval}), which polls fn "
        "until it returns a truthy value, for example await "
        'debug.waitFor(() => document.querySelector("#chart")). Layout '
        "measurements need the same treatment: the hidden frame's first "
        "layout can lag DOM readiness, so before reading sizes wait with "
        "await debug.waitFor(() => window.innerWidth). Captured "
        "events include JavaScript errors, unhandled rejections, CSP "
        "violations, failed fetches, console output and datasette.query() "
        "calls, from page load through script completion. Caveat: synthetic "
        "events dispatched by the debug script have isTrusted: false, so a "
        "few native browser behaviors (external-link interception, popup "
        "allowances) will not trigger."
    ),
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


def _app_widget_html(name, status, view_path, edit_path):
    name = html.escape(name)
    view_path = html.escape(f"{view_path}?full=1", quote=True)
    edit_path = html.escape(edit_path, quote=True)
    return (
        '<div class="datasette-app-agent-tool">'
        '<span class="datasette-app-agent-tool-status">'
        f"<strong>{name}</strong>{status}"
        "</span>"
        '<span class="datasette-app-agent-tool-actions">'
        f'<a class="datasette-app-button" href="{view_path}">View app</a>'
        f'<a class="datasette-app-button" href="{edit_path}">Edit</a>'
        "</span>"
        "</div>"
    )


class StoredQueryAccessError(ValueError):
    pass


async def _resolve_stored_queries(datasette, actor, keys):
    """Validate database/query strings the actor may grant to an app.

    Mirrors the web form: the query must exist and the actor must have
    view-query on it. Returns one dict per unique key, in input order.
    """
    if keys is None:
        return []
    if isinstance(keys, str) or not isinstance(keys, list):
        raise StoredQueryAccessError(
            "stored_queries must be a list of database/query strings"
        )
    resolved = []
    seen = set()
    for key in keys:
        if not isinstance(key, str):
            raise StoredQueryAccessError(
                "stored_queries must be a list of database/query strings"
            )
        key = key.strip()
        database_name, _, query_name = key.partition("/")
        if not database_name or not query_name:
            raise StoredQueryAccessError(
                'Invalid stored query "{}" - use database/query'.format(key)
            )
        if key in seen:
            continue
        seen.add(key)
        stored_query = None
        if database_name in datasette.databases:
            stored_query = await datasette.get_query(database_name, query_name)
        if stored_query is None or not await datasette.allowed(
            action="view-query",
            resource=QueryResource(database=database_name, query=query_name),
            actor=actor,
        ):
            raise StoredQueryAccessError("Stored query not found: {}".format(key))
        resolved.append(
            {
                "key": key,
                "database": database_name,
                "name": query_name,
                "title": getattr(stored_query, "title", None) or "",
                "sql": stored_query.sql,
                "is_write": bool(stored_query.is_write),
            }
        )
    return resolved


def _write_query_approval(app_name, write_queries):
    sections = []
    text_sections = []
    for query in write_queries:
        sections.append(
            "<p><strong>{}</strong>{}</p><pre>{}</pre>".format(
                html.escape(query["key"]),
                (
                    " &ndash; {}".format(html.escape(query["title"]))
                    if query["title"]
                    else ""
                ),
                html.escape(query["sql"]),
            )
        )
        text_sections.append("{}\n{}".format(query["key"], query["sql"]))
    warning = (
        "Write queries can insert, update or delete data. The app runs them "
        "with the permissions of whoever is viewing it."
    )
    html_body = (
        '<div class="datasette-app-query-approval">'
        "<p>App: <strong>{}</strong></p><p>{}</p>{}</div>".format(
            html.escape(app_name), html.escape(warning), "".join(sections)
        )
    )
    text_body = "App: {}\n{}\n\n{}".format(
        app_name, warning, "\n\n".join(text_sections)
    )
    if len(write_queries) == 1:
        prompt = 'Allow app "{}" to run write query "{}"?'.format(
            app_name, write_queries[0]["key"]
        )
    else:
        prompt = 'Allow app "{}" to run {} write queries?'.format(
            app_name, len(write_queries)
        )
    return prompt, html_body, text_body


async def _approve_write_queries(context, app_name, queries):
    """Ask the user before granting write queries to an app.

    Returns None if approved (or nothing needs approval), otherwise an
    error/cancellation payload string. Read-only queries never prompt.
    Must run before any side effects: the tool call re-executes from the
    start once the user answers.
    """
    write_queries = [query for query in queries if query["is_write"]]
    if not write_queries:
        return None
    not_supported = (
        "Granting write queries requires user approval, which is not "
        "available in this conversation. Ask the user to grant them from "
        "the app's edit page instead."
    )
    if context is None or not hasattr(context, "ask_user"):
        return _error(not_supported)
    prompt, html_body, text_body = _write_query_approval(app_name, write_queries)
    try:
        approved = await context.ask_user(prompt, html=html_body, text=text_body)
    except Exception as e:
        # Matched by name so datasette-agent stays an optional dependency;
        # QuestionPending (an llm.PauseChain) must keep propagating.
        if e.__class__.__name__ == "QuestionsNotSupported":
            return _error(not_supported)
        raise
    if not approved:
        return json.dumps(
            {
                "cancelled": True,
                "message": (
                    "The user declined to grant the write queries "
                    "{}. Do not retry unless they ask you to.".format(
                        ", ".join(query["key"] for query in write_queries)
                    )
                ),
            }
        )
    return None


def _render_app(editable, status=None):
    metadata = editable.metadata
    if status is None:
        status = f" updated to v{html.escape(str(editable.version))}."
    return {
        "_html": _app_widget_html(
            metadata.get("name") or metadata["app_id"],
            status,
            metadata["path"],
            f"{metadata['path']}/edit",
        ),
    }


def _render_app_unchanged(editable):
    return _render_app(
        editable, status=f" v{html.escape(str(editable.version))}."
    )


def _render_created_app(app, view_path, edit_path):
    return _app_widget_html(app["name"] or app["id"], " created.", view_path, edit_path)


class AppEditToolset(EditToolset):
    """The render tool only displays the app, so its status must not claim
    the app was updated."""

    async def render(self, external_id):
        self.render_hook = _render_app_unchanged
        try:
            return await super().render(external_id)
        finally:
            self.render_hook = _render_app


def _toolset(datasette, actor):
    return AppEditToolset(
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
    context=None,
):
    if not await _can_create_app(datasette, actor):
        return _error("Permission denied: create-app")
    try:
        csp_origins = await resolve_csp_origins(datasette, actor, csp_origins or [])
        queries = await _resolve_stored_queries(datasette, actor, stored_queries)
    except ValueError as e:
        return _error(str(e))
    stored_queries = [query["key"] for query in queries]
    refusal = await _approve_write_queries(context, name or "Untitled app", queries)
    if refusal is not None:
        return refusal
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


async def search_allowed_apps(
    datasette, actor, action, query=None, limit=10, stored_only=True
):
    """Apps the actor can perform action on, best matches first.

    Filters on permissions in SQL so that limit applies to allowed apps. A
    query with no searchable tokens matches nothing.
    """
    await Registry(datasette).ensure_tables()
    allowed_sql, allowed_params = await datasette.allowed_resources_sql(
        action=action, actor=actor
    )
    actor_id = _actor_id(actor)
    params = dict(allowed_params)
    params.update(
        {
            "current_actor_id": actor_id,
            "limit": limit,
        }
    )

    join_search = ""
    search_filter = ""
    search_order = ""
    stored_filter = "AND apps.external = 0" if stored_only else ""
    if query:
        fts_query = _fts_query(query)
        if not fts_query:
            return []
        join_search = "JOIN apps_fts ON apps.rowid = apps_fts.rowid"
        search_filter = "AND apps_fts MATCH :fts_query"
        search_order = """
            CASE
                WHEN lower(apps.name) = lower(:raw_query) THEN 0
                WHEN lower(apps.name) LIKE lower(:name_prefix) THEN 1
                ELSE 2
            END,
            bm25(apps_fts, 10.0, 1.0, 0.0),
        """
        params.update(
            {
                "fts_query": fts_query,
                "raw_query": query,
                "name_prefix": query + "%",
            }
        )

    return (
        await datasette.get_internal_database().execute(
            """
            WITH allowed_apps AS (
                {allowed_sql}
            )
            SELECT DISTINCT
                apps.id,
                apps.name,
                apps.description,
                apps.current_version,
                apps.updated_at,
                CASE
                    WHEN :current_actor_id IS NOT NULL
                     AND apps.actor_id = :current_actor_id THEN 1
                    ELSE 0
                END AS owned_by_current_actor,
                app_user_state.pinned_at,
                app_user_state.last_accessed_at
            FROM apps
            JOIN allowed_apps
              ON allowed_apps.parent = 'apps'
             AND allowed_apps.child = apps.id
            {join_search}
            LEFT JOIN app_user_state
              ON app_user_state.app_id = apps.id
             AND app_user_state.actor_id = :current_actor_id
            WHERE apps.deleted_at IS NULL
              {stored_filter}
              {search_filter}
            ORDER BY
                {search_order}
                app_user_state.pinned_at IS NOT NULL DESC,
                app_user_state.last_accessed_at DESC,
                apps.updated_at DESC,
                apps.id
            LIMIT :limit
            """.format(
                allowed_sql=allowed_sql,
                join_search=join_search,
                stored_filter=stored_filter,
                search_filter=search_filter,
                search_order=search_order,
            ),
            params,
        )
    ).rows


async def _app_list(datasette, actor, query=None, limit=10):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return _error("limit must be an integer between 1 and 20")
    if limit < 1 or limit > 20:
        return _error("limit must be an integer between 1 and 20")

    query = query.strip() if isinstance(query, str) else None
    rows = await search_allowed_apps(
        datasette, actor, "edit-app", query=query, limit=limit + 1
    )
    has_more = len(rows) > limit
    apps = [
        {
            "app_id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "current_version": row["current_version"],
            "updated_at": row["updated_at"],
            "owned_by_current_actor": bool(row["owned_by_current_actor"]),
        }
        for row in rows[:limit]
    ]
    return json.dumps(
        {
            "query": query,
            "apps": apps,
            "returned_count": len(apps),
            "has_more": has_more,
        }
    )


async def _with_app_edit_permission(datasette, actor, app_id, callback):
    if not await _can_edit_app(datasette, actor, app_id):
        return _error("Permission denied: edit-app", app_id=app_id)
    return await callback()


async def _app_update_stored_queries(datasette, actor, context, app_id, queries, add):
    if not await _can_edit_app(datasette, actor, app_id):
        return _error("Permission denied: edit-app", app_id=app_id)
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None or app["external"] or app.get("deleted_at"):
        return _error("App not found", app_id=app_id)
    current = await registry.get_stored_queries(app_id)
    if add:
        try:
            resolved = await _resolve_stored_queries(datasette, actor, queries)
        except ValueError as e:
            return _error(str(e), app_id=app_id)
        new_queries = [query for query in resolved if query["key"] not in current]
        refusal = await _approve_write_queries(
            context, app["name"] or app_id, new_queries
        )
        if refusal is not None:
            return refusal
        updated = current + [query["key"] for query in new_queries]
        changed = [query["key"] for query in new_queries]
    else:
        if isinstance(queries, str) or not isinstance(queries, list):
            return _error(
                "stored_queries must be a list of database/query strings",
                app_id=app_id,
            )
        remove = {key.strip() for key in queries if isinstance(key, str)}
        updated = [key for key in current if key not in remove]
        changed = [key for key in current if key in remove]
    try:
        await registry.set_stored_queries(app_id, updated, actor_id=_actor_id(actor))
    except KeyError:
        return _error("App not found", app_id=app_id)
    return json.dumps(
        {
            "app_id": app_id,
            "added" if add else "removed": changed,
            "stored_queries": sorted(dict.fromkeys(updated)),
        }
    )


def _csp_origins_schema_description(datasette=None):
    description = (
        "Optional exact https:// origins this app may contact "
        "for scripts, styles, images, and fetch requests"
    )
    if datasette is None:
        return description
    # The schema is built once at registration time, not per-actor, so this
    # describes the rules for users without the apps-set-csp permission.
    allowlist = configured_csp_allowlist(datasette)
    if allowlist:
        return description + (
            ". Origins must come from this allow-list: {} "
            "(the apps-set-csp permission lifts this restriction)".format(
                ", ".join(allowlist)
            )
        )
    return description + (
        ". Requires the apps-set-csp permission; no origin allow-list is "
        "configured, so users without that permission cannot set origins"
    )


async def _app_debug(
    datasette, actor, context, app_id, javascript, viewport=None, timeout_ms=None
):
    if not await _can_edit_app(datasette, actor, app_id):
        return _error("Permission denied: edit-app", app_id=app_id)
    conversation_id = getattr(context, "conversation_id", None)
    call_key = getattr(context, "call_key", None)
    # Re-executing a suspended call (after its browser task finishes)
    # must find the job it already created, not create an orphan row. A
    # completed job means this is a fresh identical call: run fresh.
    job = await get_debug_job_for_call(datasette, conversation_id, call_key)
    if (
        job is None
        or job["status"] != "pending"
        or job["app_id"] != app_id
        or job["javascript"] != javascript
    ):
        try:
            job = await create_debug_job(
                datasette,
                actor_id=_actor_id(actor),
                app_id=app_id,
                javascript=javascript,
                viewport=viewport,
                timeout_ms=timeout_ms,
                conversation_id=conversation_id,
                call_key=call_key,
            )
        except ValueError as e:
            return _error(str(e), app_id=app_id)
    app = await Registry(datasette).get_app(app_id)
    try:
        outcome = await context.browser_task(
            build_debug_harness_html(),
            payload=debug_task_payload(datasette, job),
            label="Running debug script against {}".format(
                (app and app["name"]) or app_id
            ),
            timeout_ms=job["config"]["timeout_ms"] + BROWSER_TASK_SLACK_MS,
        )
    except Exception as e:
        # Matched by name so datasette-agent stays an optional dependency;
        # BrowserTaskPending (an llm.PauseChain) must keep propagating.
        if e.__class__.__name__ == "BrowserTasksNotSupported":
            return _error(
                "app_debug requires an interactive conversation with the "
                "chat open in a browser; it is not available here",
                app_id=app_id,
            )
        raise
    if not isinstance(outcome, dict):
        outcome = {}
    envelope = normalize_envelope(outcome)
    # The audit record: what ran and what came back
    await complete_debug_job(datasette, job["id"], envelope)
    payload = {
        "app_id": app_id,
        "version": job["version"],
        "outcome": outcome.get("outcome") or "completed",
    }
    payload.update(cap_envelope(envelope))
    if payload.get("error") is None:
        payload.pop("error", None)
    return json.dumps(payload)


def get_app_edit_tools(AgentTool, datasette=None):
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

    async def app_add_stored_queries(datasette, actor, context, app_id, stored_queries):
        return await _app_update_stored_queries(
            datasette, actor, context, app_id, stored_queries, add=True
        )

    async def app_remove_stored_queries(datasette, actor, app_id, stored_queries):
        return await _app_update_stored_queries(
            datasette, actor, None, app_id, stored_queries, add=False
        )

    stored_queries_property = {
        "type": "array",
        "description": "Stored queries as database/query strings",
        "items": {"type": "string"},
    }

    async def app_render(datasette, actor, app_id):
        return await _with_app_edit_permission(
            datasette,
            actor,
            app_id,
            lambda: _toolset(datasette, actor).render(app_id),
        )

    return [
        AgentTool(
            name="app_list",
            description=APP_TOOL_DESCRIPTIONS["list"],
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional identifying words from the app name or "
                            "description"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of apps to return; defaults to 10",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
            },
            fn=_app_list,
        ),
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
                            "Optional existing stored queries this app may "
                            "call, as database/query strings. Write queries "
                            "require user approval"
                        ),
                        "items": {"type": "string"},
                    },
                    "csp_origins": {
                        "type": "array",
                        "description": _csp_origins_schema_description(datasette),
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
            name="app_add_stored_queries",
            description=APP_TOOL_DESCRIPTIONS["add_stored_queries"],
            input_schema={
                "type": "object",
                "properties": {
                    "app_id": {
                        "type": "string",
                        "description": "The stored Datasette app ID",
                    },
                    "stored_queries": stored_queries_property,
                },
                "required": ["app_id", "stored_queries"],
            },
            fn=app_add_stored_queries,
        ),
        AgentTool(
            name="app_remove_stored_queries",
            description=APP_TOOL_DESCRIPTIONS["remove_stored_queries"],
            input_schema={
                "type": "object",
                "properties": {
                    "app_id": {
                        "type": "string",
                        "description": "The stored Datasette app ID",
                    },
                    "stored_queries": stored_queries_property,
                },
                "required": ["app_id", "stored_queries"],
            },
            fn=app_remove_stored_queries,
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
        AgentTool(
            name="app_debug",
            description=APP_TOOL_DESCRIPTIONS["debug"],
            input_schema={
                "type": "object",
                "properties": {
                    "app_id": {
                        "type": "string",
                        "description": "The stored Datasette app ID",
                    },
                    "javascript": {
                        "type": "string",
                        "description": (
                            "Body of an async function executed inside the "
                            "app iframe; its awaited return value must be "
                            "JSON-serializable"
                        ),
                    },
                    "viewport": {
                        "type": "object",
                        "description": (
                            "Optional iframe size, e.g. "
                            '{"width": 375, "height": 812}; defaults to '
                            "1280x800"
                        ),
                        "properties": {
                            "width": {"type": "integer"},
                            "height": {"type": "integer"},
                        },
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": (
                            "Hard cap in milliseconds on the whole run; "
                            "defaults to 15000, maximum 60000"
                        ),
                    },
                },
                "required": ["app_id", "javascript"],
            },
            fn=_app_debug,
        ),
    ]
