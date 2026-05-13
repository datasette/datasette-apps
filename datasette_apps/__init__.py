from datasette import hookimpl

from .permissions import app_permission_sql, register_app_actions
from .registry import Registry
from .views import (
    app_json,
    apps_index,
    capability_request,
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
        (r"^/-/apps/(?P<id>[^/]+)/edit$", edit_app),
        (r"^/-/apps/(?P<id>[^/]+)/pin$", pin_app),
        (r"^/-/apps/(?P<id>[^/]+)/unpin$", unpin_app),
        (r"^/-/apps/(?P<id>[^/]+)/launch$", launch_app),
        (
            r"^/-/apps/(?P<id>[^/]+)/capabilities/(?P<capability>[^/]+)$",
            capability_request,
        ),
        (r"^/-/apps/(?P<id>[^/]+)$", view_app),
    ]


@hookimpl
def register_actions(datasette):
    return register_app_actions()


@hookimpl
def permission_resources_sql(datasette, actor, action):
    return app_permission_sql(actor, action)


@hookimpl
async def startup(datasette):
    await Registry(datasette).ensure_tables()


@hookimpl
def top_homepage(datasette, request):
    return top_homepage_html(datasette, request)
