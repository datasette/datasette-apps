from __future__ import annotations

import html
import json

from datasette import Forbidden, NotFound, Response

from .csp import build_csp
from .data_access import AppQueryError, run_app_query
from .rendering import build_app_srcdoc
from .registry import Registry


def _require_actor(request):
    if not request.actor:
        raise Forbidden("Apps require a signed-in actor")
    return request.actor


def _actor_id(actor):
    return str(actor.get("id") or "")


def _page(title, body):
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  {body}
</body>
</html>"""


def _app_link(app):
    if app["external"]:
        return f"/-/apps/{app['id']}/launch"
    return app["path"]


async def apps_index(datasette, request):
    _require_actor(request)
    registry = Registry(datasette)
    apps = await registry.list_apps(q=request.args.get("q"))
    items = []
    for app in apps:
        href = html.escape(_app_link(app), quote=True)
        items.append(
            "<li>"
            f"<a href=\"{href}\">{html.escape(app['name'])}</a>"
            f"<p>{html.escape(app['description'])}</p>"
            "</li>"
        )
    body = """
    <p><a href="/-/apps/create">Create app</a></p>
    <form method="get" action="/-/apps">
      <input type="search" name="q" placeholder="Search apps">
      <button type="submit">Search</button>
    </form>
    <ul>{}</ul>
    """.format(
        "\n".join(items)
    )
    return Response.html(_page("Apps", body))


async def create_app(datasette, request):
    actor = _require_actor(request)
    if request.method == "GET":
        body = """
        <form method="post">
          <p><label>Name <input type="text" name="name"></label></p>
          <p><label>Description <input type="text" name="description"></label></p>
          <p><label>HTML <textarea name="html"></textarea></label></p>
          <p><button type="submit">Create app</button></p>
        </form>
        """
        return Response.html(_page("Create app", body))

    post = await request.post_vars()
    app = await Registry(datasette).create_stored_app(
        actor_id=_actor_id(actor),
        name=post.get("name") or "Untitled app",
        description=post.get("description") or "",
        html=post.get("html") or "",
    )
    return Response.redirect(app["path"])


async def view_app(datasette, request):
    actor = _require_actor(request)
    app_id = request.url_vars["id"]
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None or app["external"]:
        raise NotFound("App not found")
    version = await registry.get_current_version(app_id)
    await registry.record_access(_actor_id(actor), app_id)
    csp = build_csp(await registry.get_csp_origins(app_id))
    srcdoc = html.escape(build_app_srcdoc(version["html"], csp), quote=True)
    body = f"""
    <p><a href="/-/apps/{html.escape(app_id)}/edit">Edit app</a></p>
    <iframe sandbox="allow-scripts" csp="{html.escape(csp, quote=True)}" srcdoc="{srcdoc}" style="width: 100%; min-height: 70vh; border: 1px solid #ccc;"></iframe>
    """
    return Response.html(_page(app["name"], body))


async def edit_app(datasette, request):
    _require_actor(request)
    app_id = request.url_vars["id"]
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None or app["external"]:
        raise NotFound("App not found")
    if request.method == "GET":
        version = await registry.get_current_version(app_id)
        body = f"""
        <form method="post">
          <p><label>Name <input type="text" name="name" value="{html.escape(app['name'], quote=True)}"></label></p>
          <p><label>Description <input type="text" name="description" value="{html.escape(app['description'], quote=True)}"></label></p>
          <p><label>HTML <textarea name="html">{html.escape(version['html'])}</textarea></label></p>
          <p><button type="submit">Save app</button></p>
        </form>
        """
        return Response.html(_page(f"Edit {app['name']}", body))

    post = await request.post_vars()
    await registry.update_stored_app(
        app_id,
        post.get("name") or app["name"],
        post.get("description") or "",
        post.get("html") or "",
    )
    return Response.redirect(f"/-/apps/{app_id}")


async def app_json(datasette, request):
    _require_actor(request)
    app_id = request.url_vars["id"]
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None or app["external"]:
        raise NotFound("App not found")
    version = await registry.get_current_version(app_id)
    return Response.json({"app": app, "version": version})


async def capability_request(datasette, request):
    actor = _require_actor(request)
    app_id = request.url_vars["id"]
    capability = request.url_vars["capability"]
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None or app["external"]:
        raise NotFound("App not found")
    if capability != "datasette.query":
        return Response.json(
            {"ok": False, "error": f"Unknown capability: {capability}"},
            status=404,
        )
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
    actor = _require_actor(request)
    app_id = request.url_vars["id"]
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None:
        raise NotFound("App not found")
    if not app["external"]:
        return Response.redirect(app["path"])
    await registry.record_access(_actor_id(actor), app_id)
    return Response.redirect(app["path"])
