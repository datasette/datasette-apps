"""Make apps taggable with # in datasette-agent chat messages."""

from urllib.parse import quote

from .agent_tools import search_allowed_apps
from .permissions import AppResource
from .registry import Registry

REFERENCE_INSTRUCTIONS = (
    "A tagged app's id is the app_id for the app_view, app_str_replace, "
    "app_insert, app_edit, app_render and app_debug tools, which require "
    "edit permission for that app. External apps cannot be edited."
)


def _app_reference_url(datasette, ref):
    # Has no database access, so it can't tell stored apps from external
    # ones - the launch route redirects to the right place for both, and
    # forwards full=1 for stored apps only.
    return datasette.urls.path(f"/-/apps/{quote(ref.id, safe=':')}/launch?full=1")


async def _app_reference_context(datasette, actor, ref):
    # References come from the client, so the id can't be trusted
    app = await Registry(datasette).get_app(ref.id)
    if app is None or not await datasette.allowed(
        action="view-app", resource=AppResource(ref.id), actor=actor
    ):
        return None
    lines = [f"URL: {datasette.urls.path(app['path'])}"]
    if app["external"]:
        lines.append("External app registered by a plugin; it cannot be edited.")
    else:
        can_edit = await datasette.allowed(
            action="edit-app", resource=AppResource(ref.id), actor=actor
        )
        lines.append(f"Current version: {app['current_version']}")
        lines.append(
            "The user can edit this app."
            if can_edit
            else "The user can view this app but not edit it, so its source "
            "is not available to the app tools."
        )
    return "\n".join(lines)


def get_app_reference_types(ReferenceType, Reference):
    async def search(datasette, actor, q, limit):
        rows = await search_allowed_apps(
            datasette, actor, "view-app", query=q, limit=limit, stored_only=False
        )
        return [
            Reference(
                type="app",
                id=row["id"],
                name=row["name"],
                description=row["description"] or "",
            )
            for row in rows
        ]

    return [
        ReferenceType(
            type="app",
            label="app",
            search=search,
            url=_app_reference_url,
            instructions=REFERENCE_INSTRUCTIONS,
            context=_app_reference_context,
        )
    ]
