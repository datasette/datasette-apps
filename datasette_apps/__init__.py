from datasette import hookimpl

from .registry import Registry
from .views import app_json, apps_index, create_app, edit_app, launch_app, view_app


__all__ = ["Registry"]


@hookimpl
def register_routes():
    return [
        (r"^/-/apps$", apps_index),
        (r"^/-/apps/create$", create_app),
        (r"^/-/apps/(?P<id>[^/]+)\.json$", app_json),
        (r"^/-/apps/(?P<id>[^/]+)/edit$", edit_app),
        (r"^/-/apps/(?P<id>[^/]+)/launch$", launch_app),
        (r"^/-/apps/(?P<id>[^/]+)$", view_app),
    ]
