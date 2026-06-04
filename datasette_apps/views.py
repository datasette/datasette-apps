from __future__ import annotations

import difflib
import html
import json
from urllib.parse import urlencode

from datasette import Forbidden, NotFound, Response
from datasette.resources import DatabaseResource, QueryResource

from .csp import APP_VIEW_PARENT_CSP, build_csp
from .data_access import AppQueryError, run_app_query, run_app_stored_query
from .permissions import AppResource, AppsResource
from .prompt import build_llm_prompt_data, stored_query_options
from .rendering import build_app_srcdoc, iframe_bridge_script, parent_bridge_script
from .registry import Registry


def _require_actor(request):
    if not request.actor:
        raise Forbidden("Apps require a signed-in actor")
    return request.actor


def _actor_id(actor):
    return str(actor.get("id") or "")


async def _ensure_app_permission(datasette, actor, action, app_id):
    if not await datasette.allowed(
        action=action, resource=AppResource(app_id), actor=actor
    ):
        raise Forbidden(f"Permission denied: {action}")


def _codemirror_assets():
    return """
<script src="/-/static/cm-editor-6.0.1.bundle.js"></script>
<style>
  .cm-editor {
    resize: vertical;
    overflow: hidden;
    width: min(100%, 1000px);
    min-height: 28rem;
    border: 1px solid #ddd;
  }
  .cm-editor.cm-readonly .cm-content {
    cursor: default;
  }
</style>
<script>
window.addEventListener("DOMContentLoaded", function() {
  var htmlInput = document.querySelector("textarea#html-editor");
  if (htmlInput && window.cm && window.cm.editorFromTextArea) {
    htmlInput.datasetteAppsEditorView = cm.editorFromTextArea(htmlInput, {schema: {}});
    htmlInput.dispatchEvent(new CustomEvent("datasette-app-editor-ready", {bubbles: true}));
  }
});
</script>
"""


def _readonly_codemirror_assets(textarea_selector):
    return _codemirror_assets() + """
<script>
window.addEventListener("DOMContentLoaded", function() {
  var sourceInput = document.querySelector("%s");
  if (sourceInput && window.cm && window.cm.editorFromTextArea) {
    var view = cm.editorFromTextArea(sourceInput, {schema: {}});
    view.dom.classList.add("cm-readonly");
    view.contentDOM.setAttribute("contenteditable", "false");
    view.contentDOM.setAttribute("aria-readonly", "true");
  }
});
</script>
""" % textarea_selector


def _app_link(app):
    if app["external"]:
        return f"/-/apps/{app['id']}/launch"
    return app["path"]


async def _visible_database_names(datasette, actor):
    names = []
    for database_name in datasette.databases:
        if database_name == "_internal":
            continue
        if await datasette.allowed(
            action="view-database",
            resource=DatabaseResource(database=database_name),
            actor=actor,
        ):
            names.append(database_name)
    return names


def _csp_origins_from_post(post):
    return [
        origin.strip()
        for origin in (post.get("csp_origins") or "").splitlines()
        if origin.strip()
    ]


def _access_mode_from_post(post):
    if "is_private" in post:
        return "private" if "1" in post.getlist("is_private") else "not-private"
    return None


def _revision_diff_lines(previous, version):
    previous_lines = (previous["html"] if previous else "").splitlines()
    version_lines = version["html"].splitlines()
    lines = difflib.unified_diff(
        previous_lines,
        version_lines,
        fromfile=f"v{previous['version']}" if previous else "empty",
        tofile=f"v{version['version']}",
        lineterm="",
    )
    diff_lines = []
    for line in lines:
        class_name = ""
        if line.startswith("+") and not line.startswith("+++"):
            class_name = "datasette-app-diff-added"
        elif line.startswith("-") and not line.startswith("---"):
            class_name = "datasette-app-diff-removed"
        elif line.startswith("@@"):
            class_name = "datasette-app-diff-hunk"
        diff_lines.append({"class": class_name, "text": line})
    return diff_lines


_REVISION_FIELD_LABELS = {
    "name": "Name",
    "description": "Description",
    "html": "HTML",
    "is_private": "Privacy",
    "sql_databases": "Read-only data access",
    "stored_queries": "Stored query access",
    "csp_origins": "Network access",
}


def _revision_value_for_display(field, value):
    if field == "is_private" and value is not None:
        return {"text": "Private" if value else "Not private", "empty": False}
    if field in {"sql_databases", "stored_queries", "csp_origins"}:
        value = "\n".join(value) if value else ""
    if value in {None, ""}:
        return {"text": "- unset -", "empty": True}
    return {"text": str(value), "empty": False}


def _revision_changes(previous, version):
    changes = []
    for field in version["changed_fields"]:
        if field == "html":
            continue
        changes.append(
            {
                "label": _REVISION_FIELD_LABELS[field],
                "before": _revision_value_for_display(
                    field, previous[field] if previous else None
                ),
                "after": _revision_value_for_display(field, version[field]),
            }
        )
    return changes


def _revision_field_labels(version):
    return [
        _REVISION_FIELD_LABELS.get(field, field) for field in version["changed_fields"]
    ]


async def _selected_sql_databases(datasette, actor, post):
    visible_database_names = set(await _visible_database_names(datasette, actor))
    return [
        database_name
        for database_name in post.getlist("sql_databases")
        if database_name in visible_database_names
    ]


async def _selected_stored_queries(datasette, actor, post):
    selected = []
    seen = set()
    for value in post.getlist("stored_queries"):
        value = value.strip()
        if "/" not in value or value in seen:
            continue
        database_name, query_name = value.split("/", 1)
        if not database_name or not query_name:
            continue
        stored_query = await datasette.get_query(database_name, query_name)
        if stored_query is None:
            continue
        if not await datasette.allowed(
            action="view-query",
            resource=QueryResource(database=database_name, query=query_name),
            actor=actor,
        ):
            continue
        seen.add(value)
        selected.append(value)
    return selected


async def _redirect_after_pin(request):
    if request.method == "POST":
        post = await request.post_vars()
        next_url = post.get("next")
        if next_url and next_url.startswith("/") and not next_url.startswith("//"):
            return Response.redirect(next_url)
    return Response.redirect("/-/apps")


async def apps_index(datasette, request):
    actor = request.actor
    registry = Registry(datasette)
    page_size = 20
    offset = int(request.args.get("next") or "0")
    apps = await registry.list_apps(
        q=request.args.get("q"),
        limit=page_size + 1,
        offset=offset,
        actor_id=_actor_id(actor) if actor else None,
    )
    has_next = len(apps) > page_size
    apps = apps[:page_size]
    visible_apps = []
    for app in apps:
        if not await datasette.allowed(
            action="view-app", resource=AppResource(app["id"]), actor=actor
        ):
            continue
        app = dict(app)
        app["href"] = _app_link(app)
        app["pinned"] = bool(app.get("pinned_at"))
        visible_apps.append(app)
    next_url = None
    if has_next:
        next_offset = offset + page_size
        params = {"next": next_offset}
        if request.args.get("q"):
            params["q"] = request.args.get("q")
        next_url = "/-/apps?" + urlencode(params)
    return Response.html(
        await datasette.render_template(
            "app_list.html",
            {
                "apps": visible_apps,
                "q": request.args.get("q"),
                "next_url": next_url,
                "current_path": request.full_path,
                "can_create": await datasette.allowed(
                    action="create-app", resource=AppsResource(), actor=actor
                ),
                "can_pin": bool(actor),
            },
            request=request,
        )
    )


async def create_app(datasette, request):
    actor = _require_actor(request)
    if not await datasette.allowed(
        action="create-app", resource=AppsResource(), actor=actor
    ):
        raise Forbidden("Permission denied: create-app")
    if request.method == "GET":
        sql_database_options = [
            {"name": database_name, "selected": False}
            for database_name in await _visible_database_names(datasette, actor)
        ]
        return Response.html(
            await datasette.render_template(
                "app_create.html",
                {
                    "llm_prompt_data": await build_llm_prompt_data(datasette, actor),
                    "sql_database_options": sql_database_options,
                    "stored_query_options": [],
                    "query_search_url": datasette.urls.path("/-/queries.json"),
                    "codemirror_assets": _codemirror_assets(),
                },
                request=request,
            )
        )

    post = await request.form()
    registry = Registry(datasette)
    actor_id = _actor_id(actor)
    access_mode = _access_mode_from_post(post) or "private"
    sql_databases = []
    if "sql_databases_present" in post:
        sql_databases = await _selected_sql_databases(datasette, actor, post)
    stored_queries = []
    if "stored_queries_present" in post:
        stored_queries = await _selected_stored_queries(datasette, actor, post)
    csp_origins = []
    if "csp_origins" in post:
        csp_origins = _csp_origins_from_post(post)
    app = await registry.create_stored_app(
        actor_id=actor_id,
        name=post.get("name") or "Untitled app",
        description=post.get("description") or "",
        html=post.get("html") or "",
        is_private=access_mode == "private",
        sql_databases=sql_databases,
        stored_queries=stored_queries,
        csp_origins=csp_origins,
    )
    return Response.redirect(app["path"])


async def view_app(datasette, request):
    actor = request.actor
    app_id = request.url_vars["id"]
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None or app["external"]:
        raise NotFound("App not found")
    await _ensure_app_permission(datasette, actor, "view-app", app_id)
    version = await registry.get_current_version(app_id)
    state = None
    if actor:
        actor_id = _actor_id(actor)
        await registry.record_access(actor_id, app_id)
        state = await registry.get_user_state(actor_id, app_id)
    csp = build_csp(await registry.get_csp_origins(app_id))
    srcdoc = build_app_srcdoc(version["html"], csp, iframe_bridge_script())
    can_edit = await datasette.allowed(
        action="edit-app", resource=AppResource(app_id), actor=actor
    )
    return Response.html(
        await datasette.render_template(
            "app_view.html",
            {
                "app": app,
                "csp": csp,
                "srcdoc": srcdoc,
                "parent_bridge": parent_bridge_script(app_id),
                "pinned": bool(state and state["pinned_at"]),
                "current_path": request.path,
                "can_edit": can_edit,
                "can_pin": bool(actor),
            },
            request=request,
        ),
        headers={"Content-Security-Policy": APP_VIEW_PARENT_CSP},
    )


async def edit_app(datasette, request):
    actor = _require_actor(request)
    app_id = request.url_vars["id"]
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None or app["external"]:
        raise NotFound("App not found")
    await _ensure_app_permission(datasette, actor, "edit-app", app_id)
    if request.method == "GET":
        version = await registry.get_current_version(app_id)
        revisions = []
        for revision in await registry.list_versions(app_id):
            revision = dict(revision)
            revision["created_revision"] = revision["version"] == 1
            revision["changed_field_labels"] = (
                [] if revision["created_revision"] else _revision_field_labels(revision)
            )
            revisions.append(revision)
        access_mode = await registry.get_access_mode(app_id)
        sql_databases = set(await registry.get_sql_databases(app_id))
        sql_database_options = [
            {"name": database_name, "selected": database_name in sql_databases}
            for database_name in await _visible_database_names(datasette, actor)
        ]
        stored_queries = await registry.get_stored_queries(app_id)
        csp_origins = "\n".join(await registry.get_csp_origins(app_id))
        return Response.html(
            await datasette.render_template(
                "app_edit.html",
                {
                    "app": app,
                    "html_source": version["html"],
                    "revisions": revisions,
                    "access_mode": access_mode,
                    "sql_database_options": sql_database_options,
                    "stored_query_options": await stored_query_options(
                        datasette, stored_queries
                    ),
                    "query_search_url": datasette.urls.path("/-/queries.json"),
                    "csp_origins": csp_origins,
                    "llm_prompt_data": await build_llm_prompt_data(datasette, actor),
                    "codemirror_assets": _codemirror_assets(),
                },
                request=request,
            )
        )

    post = await request.form()
    actor_id = _actor_id(actor)
    access_mode = _access_mode_from_post(post)
    update_kwargs = {}
    if access_mode:
        update_kwargs["is_private"] = access_mode == "private"
    if "sql_databases_present" in post:
        update_kwargs["sql_databases"] = await _selected_sql_databases(
            datasette, actor, post
        )
    if "stored_queries_present" in post:
        update_kwargs["stored_queries"] = await _selected_stored_queries(
            datasette, actor, post
        )
    if "csp_origins" in post:
        update_kwargs["csp_origins"] = _csp_origins_from_post(post)
    await registry.update_stored_app(
        app_id,
        post.get("name") or app["name"],
        post.get("description") or "",
        post.get("html") or "",
        actor_id=actor_id,
        **update_kwargs,
    )
    return Response.redirect(f"/-/apps/{app_id}")


async def app_revision(datasette, request):
    actor = _require_actor(request)
    app_id = request.url_vars["id"]
    revision_number = int(request.url_vars["version"])
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None or app["external"]:
        raise NotFound("App not found")
    await _ensure_app_permission(datasette, actor, "edit-app", app_id)
    version = await registry.get_version(app_id, revision_number)
    if version is None:
        raise NotFound("Revision not found")
    previous = None
    if revision_number > 1:
        previous = await registry.get_version(app_id, revision_number - 1)
    html_changed = "html" in version["changed_fields"]
    return Response.html(
        await datasette.render_template(
            "app_revision.html",
            {
                "app": app,
                "version": version,
                "previous": previous,
                "html_changed": html_changed,
                "revision_changes": _revision_changes(previous, version),
                "diff_lines": (
                    _revision_diff_lines(previous, version) if html_changed else []
                ),
                "codemirror_assets": _readonly_codemirror_assets(
                    "textarea#revision-html-source"
                ),
            },
            request=request,
        )
    )


async def app_json(datasette, request):
    actor = request.actor
    app_id = request.url_vars["id"]
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None or app["external"]:
        raise NotFound("App not found")
    await _ensure_app_permission(datasette, actor, "view-app", app_id)
    version = await registry.get_current_version(app_id)
    return Response.json({"app": app, "version": version})


async def pin_app(datasette, request):
    actor = _require_actor(request)
    app_id = request.url_vars["id"]
    registry = Registry(datasette)
    if await registry.get_app(app_id) is None:
        raise NotFound("App not found")
    await _ensure_app_permission(datasette, actor, "view-app", app_id)
    await registry.set_pinned(_actor_id(actor), app_id, True)
    return await _redirect_after_pin(request)


async def unpin_app(datasette, request):
    actor = _require_actor(request)
    app_id = request.url_vars["id"]
    registry = Registry(datasette)
    if await registry.get_app(app_id) is None:
        raise NotFound("App not found")
    await _ensure_app_permission(datasette, actor, "view-app", app_id)
    await registry.set_pinned(_actor_id(actor), app_id, False)
    return await _redirect_after_pin(request)


async def app_query(datasette, request):
    actor = request.actor
    app_id = request.url_vars["id"]
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None or app["external"]:
        raise NotFound("App not found")
    await _ensure_app_permission(datasette, actor, "view-app", app_id)
    try:
        body = json.loads((await request.post_body()).decode("utf-8") or "{}")
        if "query" in body:
            # Keep stored query execution server-side so permission revocations
            # apply immediately to app pages that are already loaded in browsers.
            result = await run_app_stored_query(
                datasette,
                app,
                actor,
                body["database"],
                body["query"],
                body.get("params"),
            )
        else:
            result = await run_app_query(
                datasette,
                app,
                actor,
                body["database"],
                body["sql"],
                body.get("params"),
            )
        return Response.json({"ok": True, "result": result})
    except (KeyError, json.JSONDecodeError) as e:
        return Response.json({"ok": False, "error": f"Invalid request: {e}"})
    except AppQueryError as e:
        return Response.json({"ok": False, "error": str(e)})


async def launch_app(datasette, request):
    actor = request.actor
    app_id = request.url_vars["id"]
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None:
        raise NotFound("App not found")
    await _ensure_app_permission(datasette, actor, "view-app", app_id)
    if not app["external"]:
        return Response.redirect(app["path"])
    if actor:
        await registry.record_access(_actor_id(actor), app_id)
    return Response.redirect(app["path"])


async def top_homepage_html(datasette, request):
    if not request.actor:
        return ""
    actor = request.actor
    registry = Registry(datasette)
    apps = []
    for app in await registry.list_pinned_apps(_actor_id(actor), limit=3):
        if await datasette.allowed(
            action="view-app", resource=AppResource(app["id"]), actor=actor
        ):
            apps.append(app)
    if not apps:
        return ""
    cards = []
    for app in apps:
        cards.append(
            '<article class="datasette-app-card">'
            f'<h3><a href="{html.escape(_app_link(app), quote=True)}">{html.escape(app["name"])}</a></h3>'
            f'<p>{html.escape(app["description"])}</p>'
            "</article>"
        )
    return (
        '<section class="datasette-apps-homepage">'
        "<h2>Pinned apps</h2>"
        '<div class="datasette-app-card-grid">' + "".join(cards) + "</div></section>"
    )
