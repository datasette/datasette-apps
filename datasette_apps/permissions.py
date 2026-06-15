from __future__ import annotations

from datasette.permissions import Action, PermissionSQL, Resource


class AppsResource(Resource):
    name = "apps"
    parent_class = None

    def __init__(self):
        super().__init__(parent="apps", child=None)

    @classmethod
    async def resources_sql(cls, datasette, actor=None):
        return "SELECT 'apps' AS parent, NULL AS child"


class AppResource(Resource):
    name = "app"
    parent_class = AppsResource

    def __init__(self, app_id):
        super().__init__(parent="apps", child=app_id)

    @classmethod
    async def resources_sql(cls, datasette, actor=None):
        return "SELECT 'apps' AS parent, id AS child FROM apps WHERE deleted_at IS NULL"


def register_app_actions():
    return [
        Action(
            name="create-app",
            description="Create Datasette apps",
            resource_class=AppsResource,
        ),
        Action(
            name="view-app",
            description="View a Datasette app",
            resource_class=AppResource,
        ),
        Action(
            name="edit-app",
            description="Edit a Datasette app",
            resource_class=AppResource,
        ),
        Action(
            name="delete-app",
            description="Delete a Datasette app",
            resource_class=AppResource,
        ),
        Action(
            name="manage-app-access",
            description="Manage Datasette app access",
            resource_class=AppResource,
        ),
        Action(
            name="apps-set-csp",
            description="Set arbitrary CSP origins on Datasette apps",
            resource_class=AppsResource,
        ),
    ]


def app_permission_sql(actor, action):
    # Owners can do anything to their stored apps.
    actor_id = actor.get("id") if actor else None
    if action not in {"view-app", "edit-app", "delete-app", "manage-app-access"}:
        return None

    action_reasons = {
        "view-app": "Owner can view app",
        "edit-app": "Owner can edit app",
        "delete-app": "Owner can delete app",
        "manage-app-access": "Owner can manage app access",
    }
    sql = """
    SELECT 'apps' AS parent,
           id AS child,
           1 AS allow,
           :owner_reason AS reason
    FROM apps
    WHERE actor_id = :actor_id
      AND :actor_id IS NOT NULL
      AND external = 0 -- It's a stored HTML app, not an external app
      AND deleted_at IS NULL
    """
    # restriction_sql defines the resources that configured Datasette grants
    # are allowed to apply to. The privacy rule is the same for every app
    # action: a private app is only in that set for its owner.
    #
    # The only action-specific difference is that view-app can include
    # non-private external apps, while edit/delete/manage only apply to stored
    # apps.
    stored_apps_only = "\n          AND external = 0" if action != "view-app" else ""
    restriction_sql = f"""
    SELECT 'apps' AS parent,
           id AS child
    FROM apps
    WHERE deleted_at IS NULL{stored_apps_only}
      AND (is_private = 0
           OR (
               actor_id = :actor_id
               AND :actor_id IS NOT NULL
               AND external = 0
           ))
    """
    return PermissionSQL(
        source="datasette-apps",
        sql=sql,
        restriction_sql=restriction_sql,
        params={
            "actor_id": actor_id,
            "owner_reason": action_reasons[action],
        },
    )
