from __future__ import annotations

import difflib
import html
import json
from urllib.parse import urlencode

from datasette import Forbidden, NotFound, Response
from datasette.resources import DatabaseResource

from .csp import build_csp
from .data_access import AppQueryError, run_app_query
from .permissions import AppResource, AppsResource
from .prompt import build_llm_prompt
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
    cm.editorFromTextArea(htmlInput, {schema: {}});
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


async def _selected_sql_databases(datasette, actor, post):
    visible_database_names = set(await _visible_database_names(datasette, actor))
    return [
        database_name
        for database_name in post.getlist("sql_databases")
        if database_name in visible_database_names
    ]


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
        prompt = await build_llm_prompt(datasette, actor)
        sql_database_options = [
            {"name": database_name, "selected": False}
            for database_name in await _visible_database_names(datasette, actor)
        ]
        return Response.html(
            await datasette.render_template(
                "app_create.html",
                {
                    "llm_prompt": prompt,
                    "sql_database_options": sql_database_options,
                    "codemirror_assets": _codemirror_assets(),
                },
                request=request,
            )
        )

    post = await request.form()
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id=_actor_id(actor),
        name=post.get("name") or "Untitled app",
        description=post.get("description") or "",
        html=post.get("html") or "",
    )
    access_mode = _access_mode_from_post(post)
    if access_mode:
        await registry.set_access_mode(app["id"], access_mode)
    if "sql_databases_present" in post:
        await registry.set_sql_databases(
            app["id"], await _selected_sql_databases(datasette, actor, post)
        )
    if "csp_origins" in post:
        await registry.set_csp_origins(app["id"], _csp_origins_from_post(post))
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
        )
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
        revisions = await registry.list_versions(app_id)
        access_mode = await registry.get_access_mode(app_id)
        sql_databases = set(await registry.get_sql_databases(app_id))
        sql_database_options = [
            {"name": database_name, "selected": database_name in sql_databases}
            for database_name in await _visible_database_names(datasette, actor)
        ]
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
                    "csp_origins": csp_origins,
                    "codemirror_assets": _codemirror_assets(),
                },
                request=request,
            )
        )

    post = await request.form()
    await registry.update_stored_app(
        app_id,
        post.get("name") or app["name"],
        post.get("description") or "",
        post.get("html") or "",
    )
    access_mode = _access_mode_from_post(post)
    if access_mode:
        await registry.set_access_mode(app_id, access_mode)
    if "sql_databases_present" in post:
        await registry.set_sql_databases(
            app_id, await _selected_sql_databases(datasette, actor, post)
        )
    if "csp_origins" in post:
        await registry.set_csp_origins(app_id, _csp_origins_from_post(post))
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
    return Response.html(
        await datasette.render_template(
            "app_revision.html",
            {
                "app": app,
                "version": version,
                "previous": previous,
                "diff_lines": _revision_diff_lines(previous, version),
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
