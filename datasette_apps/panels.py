"""Panels: apps pinned in a sized frame at the top of the homepage, a
database page or a table page."""

from __future__ import annotations

import html
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, unquote, urlencode

from datasette import Forbidden, NotFound, Response
from datasette.resources import DatabaseResource, TableResource

from .csp import APP_VIEW_PARENT_CSP, build_csp
from .data_access import AppQueryError, run_app_query
from .panel_kit import (
    DEFAULT_PANEL_HEIGHT,
    DEFAULT_PANEL_WIDTH,
    PANEL_WIDTH_VALUES,
    PANEL_WIDTHS,
    TEMPLATES,
    template_of_html,
)
from .permissions import AppResource, AppsResource
from .registry import Registry
from .rendering import (
    PANEL_MAX_HEIGHT,
    PANEL_MIN_HEIGHT,
    build_app_srcdoc,
    iframe_bridge_script,
    parent_bridge_script,
)

TARGET_TYPES = ("homepage", "database", "table")
MAX_PANELS_PER_PAGE = 12
# Set by the asgi_wrapper, flipped by render_panels(): pages that show
# panels get the same frame-src lockdown as the app view page.
SCOPE_STATE_KEY = "datasette_apps_state"

PANEL_COLUMNS = """
    app_panels.id, app_panels.app_id, app_panels.target_type,
    app_panels.database_name, app_panels.table_name, app_panels.actor_id,
    app_panels.is_shared, app_panels.width, app_panels.height,
    app_panels.show_header, app_panels.share_filters, app_panels.sort_order, app_panels.created_at,
    app_panels.updated_at
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _actor_id(actor):
    return str(actor.get("id") or "") if actor else ""


class PanelError(ValueError):
    pass


def clean_width(value, default=DEFAULT_PANEL_WIDTH):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    # Snap to the nearest supported column span
    return min(PANEL_WIDTH_VALUES, key=lambda width: abs(width - value))


def clean_height(value, default=DEFAULT_PANEL_HEIGHT):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(PANEL_MIN_HEIGHT, min(PANEL_MAX_HEIGHT, value))


def normalize_target(target_type, database=None, table=None):
    if target_type not in TARGET_TYPES:
        raise PanelError("target must be one of: " + ", ".join(TARGET_TYPES))
    database = (database or "") if target_type != "homepage" else ""
    table = (table or "") if target_type == "table" else ""
    if target_type in {"database", "table"} and not database:
        raise PanelError(f"A {target_type} panel needs a database")
    if target_type == "table" and not table:
        raise PanelError("A table panel needs a table")
    return target_type, database, table


def target_path(datasette, target_type, database="", table=""):
    if target_type == "table":
        return datasette.urls.table(database, table)
    if target_type == "database":
        return datasette.urls.database(database)
    return datasette.urls.instance()


def target_label(target_type, database="", table=""):
    if target_type == "table":
        return f"table {database} / {table}"
    if target_type == "database":
        return f"database {database}"
    return "the homepage"


async def can_view_target(datasette, actor, target_type, database="", table=""):
    if target_type == "homepage":
        return await datasette.allowed(action="view-instance", actor=actor)
    if database not in datasette.databases or database == "_internal":
        return False
    if target_type == "database":
        return await datasette.allowed(
            action="view-database",
            resource=DatabaseResource(database=database),
            actor=actor,
        )
    db = datasette.databases[database]
    if not (await db.table_exists(table) or await db.view_exists(table)):
        return False
    return await datasette.allowed(
        action="view-table", resource=TableResource(database, table), actor=actor
    )


async def can_share_panels(datasette, actor):
    return bool(actor) and await datasette.allowed(
        action="apps-share-panels", resource=AppsResource(), actor=actor
    )


async def can_manage_panel(datasette, actor, panel):
    if not actor:
        return False
    if panel["actor_id"] == _actor_id(actor):
        return True
    return bool(panel["is_shared"]) and await can_share_panels(datasette, actor)


async def create_panel(
    datasette,
    actor,
    app_id,
    target_type,
    database=None,
    table=None,
    width=None,
    height=None,
    is_shared=False,
    show_header=None,
    share_filters=False,
):
    """Pin an app to a page for this actor. Raises PanelError with a
    message that is safe to show to the user (or an agent).

    show_header=None picks automatically: apps built from a panel template
    carry their own label, so the app-name header would only repeat it."""
    if not actor:
        raise PanelError("Pinning a panel requires a signed-in actor")
    target_type, database, table = normalize_target(target_type, database, table)
    registry = Registry(datasette)
    app = await registry.get_app(app_id)
    if app is None or app["external"]:
        raise PanelError(f"App {app_id} not found")
    if not await datasette.allowed(
        action="view-app", resource=AppResource(app_id), actor=actor
    ):
        raise PanelError("Permission denied: view-app")
    if not await can_view_target(datasette, actor, target_type, database, table):
        raise PanelError(f"Cannot pin to {target_label(target_type, database, table)}")
    if is_shared and not await can_share_panels(datasette, actor):
        raise PanelError("Permission denied: apps-share-panels")
    if show_header is None:
        version = await registry.get_current_version(app_id)
        show_header = template_of_html(version["html"] if version else "") is None
    db = datasette.get_internal_database()
    now = _now()

    def insert(conn):
        count = conn.execute(
            """
            SELECT count(*) FROM app_panels
            WHERE target_type = ? AND database_name = ? AND table_name = ?
              AND (is_shared = 1 OR actor_id = ?)
            """,
            (target_type, database, table, _actor_id(actor)),
        ).fetchone()[0]
        if count >= MAX_PANELS_PER_PAGE:
            raise PanelError(
                f"That page already has {MAX_PANELS_PER_PAGE} panels - remove one first"
            )
        sort_order = conn.execute(
            """
            SELECT COALESCE(MAX(sort_order), 0) + 10 FROM app_panels
            WHERE target_type = ? AND database_name = ? AND table_name = ?
            """,
            (target_type, database, table),
        ).fetchone()[0]
        cursor = conn.execute(
            """
            INSERT INTO app_panels (
                app_id, target_type, database_name, table_name, actor_id,
                is_shared, width, height, show_header, share_filters,
                sort_order, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                app_id,
                target_type,
                database,
                table,
                _actor_id(actor),
                int(bool(is_shared)),
                clean_width(width),
                clean_height(height),
                int(bool(show_header)),
                int(bool(share_filters) and target_type == "table"),
                sort_order,
                now,
                now,
            ),
        )
        return cursor.lastrowid

    panel_id = await db.execute_write_fn(insert)
    return await get_panel(datasette, panel_id)


async def get_panel(datasette, panel_id):
    result = await datasette.get_internal_database().execute(
        f"SELECT {PANEL_COLUMNS} FROM app_panels WHERE id = ?", [panel_id]
    )
    row = result.first()
    return dict(row) if row else None


async def update_panel(datasette, panel_id, **values):
    allowed = {
        "width",
        "height",
        "is_shared",
        "show_header",
        "share_filters",
        "sort_order",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key} = :{key}" for key in values)
    await datasette.get_internal_database().execute_write(
        f"UPDATE app_panels SET {assignments}, updated_at = :now WHERE id = :id",
        {**values, "now": _now(), "id": panel_id},
    )


async def delete_panel(datasette, panel_id):
    await datasette.get_internal_database().execute_write(
        "DELETE FROM app_panels WHERE id = ?", [panel_id]
    )


async def list_panels(datasette, actor, target_type, database="", table=""):
    """Panels the actor should see on a page: their own plus shared ones,
    for apps that still exist and that they may view."""
    result = await datasette.get_internal_database().execute(
        f"""
        SELECT {PANEL_COLUMNS}, apps.name AS app_name, apps.path AS app_path
        FROM app_panels
        JOIN apps ON apps.id = app_panels.app_id
        WHERE app_panels.target_type = :target_type
          AND app_panels.database_name = :database
          AND app_panels.table_name = :table
          AND (app_panels.is_shared = 1 OR app_panels.actor_id = :actor_id)
          AND apps.deleted_at IS NULL
          AND apps.external = 0
        ORDER BY app_panels.sort_order, app_panels.id
        LIMIT :limit
        """,
        {
            "target_type": target_type,
            "database": database or "",
            "table": table or "",
            # Anonymous visitors only ever match shared panels
            "actor_id": _actor_id(actor) or None,
            "limit": MAX_PANELS_PER_PAGE * 2,
        },
    )
    panels = []
    for row in result.rows:
        if await datasette.allowed(
            action="view-app", resource=AppResource(row["app_id"]), actor=actor
        ):
            panels.append(dict(row))
    return panels[:MAX_PANELS_PER_PAGE]


def _e(value):
    return html.escape(str(value), quote=True)


async def render_panels(datasette, request, target_type, database="", table=""):
    """HTML for the top_homepage / top_database / top_table hooks."""
    actor = request.actor
    panels = await list_panels(datasette, actor, target_type, database, table)
    if not panels:
        return ""
    registry = Registry(datasette)
    share_allowed = await can_share_panels(datasette, actor)
    articles = []
    for panel in panels:
        version = await registry.get_current_version(panel["app_id"])
        if version is None:
            continue
        csp = build_csp(await registry.get_csp_origins(panel["app_id"]))
        token = secrets.token_urlsafe(32)
        iframe_id = f"datasette-app-panel-frame-{panel['id']}"
        context = {
            "target": target_type,
            "database": database or None,
            "table": table or None,
            "panel": {"width": panel["width"], "height": panel["height"]},
        }
        if target_type == "table" and panel["share_filters"]:
            context["filters"] = page_filters(request.query_string)
        srcdoc = build_app_srcdoc(
            version["html"], csp, iframe_bridge_script(token, context=context)
        )
        app_path = datasette.urls.path(panel["app_path"])
        manageable = bool(actor) and (
            panel["actor_id"] == _actor_id(actor)
            or (panel["is_shared"] and share_allowed)
        )
        links = [f'<a href="{_e(app_path)}">Open</a>']
        if manageable:
            links.append(
                '<a href="{}">Settings</a>'.format(
                    _e(datasette.urls.path(f"/-/apps/panels/{panel['id']}"))
                )
            )
        header = (
            '<header class="datasette-app-panel-header">'
            f'<h3><a href="{_e(app_path)}">{_e(panel["app_name"])}</a></h3>'
            f'<span class="datasette-app-panel-links">{" ".join(links)}</span>'
            "</header>"
        )
        classes = "datasette-app-panel"
        if not panel["show_header"]:
            classes += " datasette-app-panel-headless"
        articles.append(
            f'<article class="{classes}" style="--panel-span: {int(panel["width"])}"'
            f' data-panel-id="{panel["id"]}" data-app-id="{_e(panel["app_id"])}">'
            + header
            + parent_bridge_script(
                panel["app_id"],
                iframe_id=iframe_id,
                channel_token=token,
                panel=True,
                panel_id=panel["id"],
            )
            + f'<iframe id="{iframe_id}" class="datasette-app-panel-frame"'
            f' title="{_e(panel["app_name"])}" sandbox="allow-scripts allow-forms"'
            f' csp="{_e(csp)}" style="height: {int(panel["height"])}px"'
            f' srcdoc="{_e(srcdoc)}"></iframe>'
            "</article>"
        )
    if not articles:
        return ""
    state = request.scope.get(SCOPE_STATE_KEY)
    if state is not None:
        state["frames"] = True
    return (
        '<section class="datasette-app-panels" aria-label="App panels">'
        + "".join(articles)
        + "</section>"
    )


# --- Page filters --------------------------------------------------------

# Query string arguments that narrow which rows a table page shows. Other
# underscore arguments (_sort, _size, _next, _col, _facet...) change how
# the page is displayed, not which rows match.
_FILTER_UNDERSCORE_ARGS = {"_search", "_where", "_through"}
_SORT_ARGS = {"_sort", "_sort_desc"}
PAGE_ROWS_MAX = 100


def _is_filter_arg(key):
    return (
        not key.startswith("_")
        or key in _FILTER_UNDERSCORE_ARGS
        or key.startswith("_search_")
    )


def filter_args(query_string, keep_sort=False):
    return [
        (key, value)
        for key, value in parse_qsl(query_string or "", keep_blank_values=True)
        if _is_filter_arg(key) or (keep_sort and key in _SORT_ARGS)
    ]


def page_filters(query_string):
    """What a filter-sharing panel is told about the page it is on."""
    args = filter_args(query_string)
    return {
        "query_string": query_string or "",
        "params": [list(pair) for pair in args],
        "active": bool(args),
    }


class PageDataError(Exception):
    pass


async def _table_json(datasette, actor, database, table, args):
    path = datasette.urls.table(database, table, format="json")
    response = await datasette.client.get(path + "?" + urlencode(args), actor=actor)
    try:
        data = response.json()
    except ValueError as e:
        raise PageDataError("Could not read the page data") from e
    if response.status_code in (401, 403):
        raise PageDataError("Permission denied by Datasette")
    if response.status_code != 200 or data.get("ok") is False:
        raise PageDataError(data.get("error") or "Could not read the page data")
    return data


_FILTERED_PLACEHOLDER = re.compile(r"\{\{\s*filtered\s*\}\}")
_TRAILING_LIMIT = re.compile(r"\s+limit\s+\d+\s*$", re.IGNORECASE)


async def filtered_table_sql(datasette, actor, database, table, query_string):
    """The SQL (and named parameters) selecting every row that matches the
    page's filters. Datasette builds the query, so facets, search, _where
    and its permission checks all behave exactly as they do on the page;
    we only drop the page-size LIMIT."""
    data = await _table_json(
        datasette,
        actor,
        database,
        table,
        filter_args(query_string) + [("_extra", "query"), ("_size", "0")],
    )
    query = data.get("query") or {}
    if not query.get("sql"):
        raise PageDataError("Could not read the page query")
    return _TRAILING_LIMIT.sub("", query["sql"]), dict(query.get("params") or {})


async def run_page_request(datasette, actor, app, body):
    """Serve datasette.pageRows() / datasette.pageQuery() for one panel."""
    panel = None
    try:
        panel = await get_panel(datasette, int(body.get("panel_id")))
    except (TypeError, ValueError):
        pass
    if (
        panel is None
        or panel["app_id"] != app["id"]
        or not (panel["is_shared"] or panel["actor_id"] == _actor_id(actor))
    ):
        raise PageDataError("Page data is only available to pinned panels")
    if panel["target_type"] != "table":
        raise PageDataError("Page data is only available on table pages")
    if not panel["share_filters"]:
        raise PageDataError(
            "This panel is not allowed to see the page filters - switch that "
            "on in the panel settings"
        )
    database, table = panel["database_name"], panel["table_name"]
    # No new access: the app must already be allowed to query this database
    if database not in await Registry(datasette).get_sql_databases(app["id"]):
        raise PageDataError("This app is not allowed to query that database")
    query_string = body.get("query_string")
    if not isinstance(query_string, str):
        query_string = ""

    if body.get("kind") == "rows":
        try:
            size = int(body.get("size") or PAGE_ROWS_MAX)
        except (TypeError, ValueError):
            size = PAGE_ROWS_MAX
        data = await _table_json(
            datasette,
            actor,
            database,
            table,
            filter_args(query_string, keep_sort=True)
            + [
                ("_shape", "objects"),
                ("_extra", "columns,count"),
                ("_size", str(max(1, min(PAGE_ROWS_MAX, size)))),
            ],
        )
        return {
            "columns": data.get("columns") or [],
            "rows": data.get("rows") or [],
            "count": data.get("count"),
        }

    sql = body.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise PageDataError("pageQuery() needs a SQL string")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise PageDataError("Query parameters must be an object of named values")
    filtered_sql, filtered_params = await filtered_table_sql(
        datasette, actor, database, table, query_string
    )
    clash = set(params) & set(filtered_params)
    if clash:
        raise PageDataError(
            "Parameter names are reserved for the page filters: "
            + ", ".join(sorted(clash))
        )
    sql = _FILTERED_PLACEHOLDER.sub(lambda match: f"({filtered_sql})", sql)
    try:
        return await run_app_query(
            datasette, app, actor, database, sql, {**filtered_params, **params}
        )
    except AppQueryError as e:
        raise PageDataError(str(e)) from e


def frame_csp_asgi_wrapper(app):
    """Pages that render panels get Content-Security-Policy: frame-src
    'none', exactly like the app view page. A sandboxed app cannot be
    stopped from navigating its own frame except by the parent's frame-src,
    and navigating to https://evil.example/?data is an exfiltration route."""

    async def wrapper(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        state = {}
        scope = dict(scope, **{SCOPE_STATE_KEY: state})

        async def wrapped_send(event):
            if event["type"] == "http.response.start" and state.get("frames"):
                headers = list(event.get("headers") or [])
                headers.append(
                    (b"content-security-policy", APP_VIEW_PARENT_CSP.encode("latin-1"))
                )
                event = dict(event, headers=headers)
            await send(event)

        await app(scope, receive, wrapped_send)

    return wrapper


def add_panel_path(datasette, target_type, database="", table="", app_id=None):
    params = {"target": target_type} if target_type else {}
    if database:
        params["database"] = database
    if table:
        params["table"] = table
    if app_id:
        params["app"] = app_id
    return datasette.urls.path("/-/apps/panels/add") + "?" + urlencode(params)


def panel_action_links(datasette, actor, target_type, database="", table=""):
    """Entry for the homepage / database / table actions menus."""
    if not actor:
        return []
    return [
        {
            "href": add_panel_path(datasette, target_type, database, table),
            "label": "Add app panel",
            "description": "Pin a small app or chart to the top of this page",
        }
    ]


# --- Views ---------------------------------------------------------------


def _require_actor(request):
    if not request.actor:
        raise Forbidden("Panels require a signed-in actor")
    return request.actor


def _target_value(target_type, database="", table=""):
    return "/".join(
        [target_type, quote(database or "", safe=""), quote(table or "", safe="")]
    )


def _parse_target_value(value):
    parts = (value or "").split("/")
    parts += [""] * (3 - len(parts))
    return parts[0], unquote(parts[1]), unquote(parts[2])


async def _target_options(datasette, actor, selected):
    """Homepage plus every database and table the actor can see."""
    options = [
        {
            "value": _target_value("homepage"),
            "label": "Homepage",
            "group": "",
        }
    ]
    for name, db in datasette.databases.items():
        if name == "_internal" or not await can_view_target(
            datasette, actor, "database", name
        ):
            continue
        options.append(
            {
                "value": _target_value("database", name),
                "label": f"{name} (database page)",
                "group": name,
            }
        )
        for table in (await db.table_names())[:200]:
            if await datasette.allowed(
                action="view-table", resource=TableResource(name, table), actor=actor
            ):
                options.append(
                    {
                        "value": _target_value("table", name, table),
                        "label": table,
                        "group": name,
                    }
                )
    for option in options:
        option["selected"] = option["value"] == selected
    return options


async def _pinnable_apps(datasette, actor):
    apps = []
    for app in await Registry(datasette).list_apps(
        limit=200, actor_id=_actor_id(actor)
    ):
        if app["external"]:
            continue
        if await datasette.allowed(
            action="view-app", resource=AppResource(app["id"]), actor=actor
        ):
            apps.append(app)
    return apps


async def add_panel(datasette, request):
    actor = _require_actor(request)
    if request.method == "POST":
        post = await request.post_vars()
        target_type, database, table = _parse_target_value(post.get("target"))
        try:
            await create_panel(
                datasette,
                actor,
                post.get("app_id") or "",
                target_type,
                database,
                table,
                width=post.get("width"),
                height=post.get("height"),
                is_shared=post.get("is_shared") == "1",
                show_header={"1": True, "0": False}.get(post.get("show_header")),
                share_filters=post.get("share_filters") == "1",
            )
        except PanelError as e:
            raise Forbidden(str(e))
        return Response.redirect(target_path(datasette, target_type, database, table))

    target_type = request.args.get("target") or ""
    database = request.args.get("database") or ""
    table = request.args.get("table") or ""
    target_known = False
    if target_type:
        try:
            target_type, database, table = normalize_target(
                target_type, database, table
            )
        except PanelError as e:
            raise NotFound(str(e))
        if not await can_view_target(datasette, actor, target_type, database, table):
            raise NotFound("Page not found")
        target_known = True
    selected_app = request.args.get("app") or ""
    template_params = {}
    if target_known:
        template_params = {"panel_target": target_type}
        if database:
            template_params["panel_database"] = database
        if table:
            template_params["panel_table"] = table
    create_base = datasette.urls.path("/-/apps/create")
    templates = [
        {
            "template": template,
            "href": create_base
            + "?"
            + urlencode({"template": template.id, **template_params}),
        }
        for template in TEMPLATES
    ]
    return Response.html(
        await datasette.render_template(
            "panel_add.html",
            {
                "target_known": target_known,
                "target_label": (
                    target_label(target_type, database, table) if target_known else ""
                ),
                "target_path": (
                    target_path(datasette, target_type, database, table)
                    if target_known
                    else ""
                ),
                "target_options": await _target_options(
                    datasette,
                    actor,
                    (
                        _target_value(target_type, database, table)
                        if target_known
                        else _target_value("homepage")
                    ),
                ),
                "apps": await _pinnable_apps(datasette, actor),
                "selected_app": selected_app,
                "templates": templates,
                "can_create": await datasette.allowed(
                    action="create-app", resource=AppsResource(), actor=actor
                ),
                "can_share": await can_share_panels(datasette, actor),
                "widths": PANEL_WIDTHS,
                "default_width": 6,
                "default_height": DEFAULT_PANEL_HEIGHT,
                "min_height": PANEL_MIN_HEIGHT,
                "max_height": PANEL_MAX_HEIGHT,
            },
            request=request,
        )
    )


async def edit_panel(datasette, request):
    actor = _require_actor(request)
    panel = await get_panel(datasette, int(request.url_vars["panel_id"]))
    if panel is None or not await can_manage_panel(datasette, actor, panel):
        raise NotFound("Panel not found")
    app = await Registry(datasette).get_app(panel["app_id"])
    if app is None:
        raise NotFound("Panel not found")
    page = target_path(
        datasette, panel["target_type"], panel["database_name"], panel["table_name"]
    )
    share_allowed = await can_share_panels(datasette, actor)
    if request.method == "POST":
        post = await request.post_vars()
        if post.get("remove") == "1":
            await delete_panel(datasette, panel["id"])
            return Response.redirect(page)
        values = {
            "width": clean_width(post.get("width"), panel["width"]),
            "height": clean_height(post.get("height"), panel["height"]),
            "show_header": int(post.get("show_header") == "1"),
            "share_filters": int(
                post.get("share_filters") == "1" and panel["target_type"] == "table"
            ),
        }
        try:
            values["sort_order"] = int(post.get("sort_order"))
        except (TypeError, ValueError):
            pass
        if share_allowed:
            values["is_shared"] = int(post.get("is_shared") == "1")
        await update_panel(datasette, panel["id"], **values)
        return Response.redirect(page)
    return Response.html(
        await datasette.render_template(
            "panel_edit.html",
            {
                "panel": panel,
                "app": app,
                "target_label": target_label(
                    panel["target_type"], panel["database_name"], panel["table_name"]
                ),
                "target_path": page,
                "can_share": share_allowed,
                "widths": PANEL_WIDTHS,
                "min_height": PANEL_MIN_HEIGHT,
                "max_height": PANEL_MAX_HEIGHT,
            },
            request=request,
        )
    )


async def panel_templates_json(datasette, request):
    return Response.json({"templates": [template.as_dict() for template in TEMPLATES]})
