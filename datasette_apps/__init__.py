from datasette import hookimpl
from datasette.jump import JumpSQL

from .permissions import app_permission_sql, register_app_actions
from .registry import Registry
from .views import (
    app_json,
    app_query,
    app_revision,
    apps_index,
    create_app,
    edit_app,
    launch_app,
    pin_app,
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
        (r"^/-/apps/(?P<id>[^/]+)\.json$", app_json),
        (r"^/-/apps/(?P<id>[^/]+)/revisions/(?P<version>\d+)$", app_revision),
        (r"^/-/apps/(?P<id>[^/]+)/edit$", edit_app),
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
            """,
            params=app_params,
        )

    return inner


@hookimpl
async def startup(datasette):
    await Registry(datasette).ensure_tables()


@hookimpl
def top_homepage(datasette, request):
    return top_homepage_html(datasette, request)


@hookimpl
def extra_css_urls():
    return ["/-/static-plugins/datasette-apps/datasette-apps.css"]


@hookimpl
def menu_links(datasette, actor, request):
    if not actor:
        return []
    return [{"href": datasette.urls.path("/-/apps"), "label": "Apps"}]
