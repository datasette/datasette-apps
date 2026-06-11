import re

from datasette import hookimpl
from datasette.jump import JumpSQL

from .acl import app_acl_roles, backfill_acl_grants, datasette_share_assets
from .csp import configured_csp_allowlist
from .permissions import app_permission_sql, register_app_actions
from .registry import Registry
from .views import (
    app_json,
    app_query,
    app_revision,
    apps_index,
    create_app,
    delete_app,
    edit_app,
    launch_app,
    pin_app,
    recent_stored_queries,
    top_homepage_html,
    unpin_app,
    view_app,
)

__all__ = ["Registry"]


@hookimpl
def register_routes():
    return [
        (r"^/-/apps$", apps_index),
        (r"^/-/apps/create$", create_app),
        (r"^/-/apps/recent-queries\.json$", recent_stored_queries),
        (r"^/-/apps/(?P<id>[^/]+)\.json$", app_json),
        (r"^/-/apps/(?P<id>[^/]+)/revisions/(?P<version>\d+)$", app_revision),
        (r"^/-/apps/(?P<id>[^/]+)/edit$", edit_app),
        (r"^/-/apps/(?P<id>[^/]+)/delete$", delete_app),
        (r"^/-/apps/(?P<id>[^/]+)/pin$", pin_app),
        (r"^/-/apps/(?P<id>[^/]+)/unpin$", unpin_app),
        (r"^/-/apps/(?P<id>[^/]+)/launch$", launch_app),
        (r"^/-/apps/(?P<id>[^/]+)/query$", app_query),
        (r"^/-/apps/(?P<id>[^/]+)$", view_app),
    ]


@hookimpl
def register_actions(datasette):
    return register_app_actions()


@hookimpl
def permission_resources_sql(datasette, actor, action):
    return app_permission_sql(actor, action)


@hookimpl
def datasette_acl_roles(datasette):
    """Viewer / Editor / Manager roles for the ``app`` resource type."""
    return app_acl_roles()


@hookimpl
def jump_items_sql(datasette, actor, request):
    async def inner():
        app_sql, app_params = await datasette.allowed_resources_sql(
            action="view-app", actor=actor
        )
        return JumpSQL(
            sql=f"""
            WITH allowed_apps AS (
                {app_sql}
            )
            SELECT
                'app' AS type,
                apps.name AS label,
                apps.description AS description,
                json_object(
                    'method', 'path',
                    'path', CASE
                        WHEN apps.external = 1
                        THEN '/-/apps/' || apps.id || '/launch'
                        ELSE apps.path
                    END
                ) AS url,
                'app' || apps.name || ' ' || apps.description || ' ' ||
                    apps.id || ' ' || apps.source AS search_text,
                NULL AS display_name
            FROM apps
            JOIN allowed_apps
                ON allowed_apps.parent = 'apps'
               AND allowed_apps.child = apps.id
            WHERE apps.deleted_at IS NULL
            """,
            params=app_params,
        )

    return inner


@hookimpl
async def startup(datasette):
    # Fail fast on invalid allowed_csp_origins plugin configuration
    configured_csp_allowlist(datasette)
    await Registry(datasette).ensure_tables()
    await backfill_acl_grants(datasette)


@hookimpl
def top_homepage(datasette, request):
    return top_homepage_html(datasette, request)


# The share dialog only appears on individual app pages, so its assets are
# loaded there and nowhere else (acl-share registers nothing site-wide).
_APP_PAGE_RE = re.compile(r"^/-/apps/(?!create$)[^/]+$")


def _is_app_page(request):
    return bool(request and _APP_PAGE_RE.match(request.path or ""))


@hookimpl
def extra_css_urls(datasette, request):
    urls = ["/-/static-plugins/datasette-apps/datasette-apps.css"]
    if _is_app_page(request):
        urls.extend(datasette_share_assets(datasette)["css"])
    return urls


@hookimpl
def extra_js_urls(datasette, request):
    if not _is_app_page(request):
        return []
    return datasette_share_assets(datasette)["js"]


@hookimpl
def menu_links(datasette, actor, request):
    if not actor:
        return []
    return [{"href": datasette.urls.path("/-/apps"), "label": "Apps"}]


@hookimpl(optionalhook=True)
def register_agent_tools(datasette):
    from datasette_agent.tools import AgentTool

    from .agent_tools import get_app_edit_tools

    return get_app_edit_tools(AgentTool, datasette)
