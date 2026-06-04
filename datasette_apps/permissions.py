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
    ]


def app_permission_sql(actor, action):
    actor_id = actor.get("id") if actor else None
    if action == "create-app":
        return PermissionSQL(
            source="datasette-apps",
            sql="""
            SELECT 'apps' AS parent,
                   NULL AS child,
                   1 AS allow,
                   'Signed-in actors can create apps' AS reason
            WHERE :actor_id IS NOT NULL
            """,
            params={"actor_id": actor_id},
        )
    if action not in {"view-app", "edit-app", "delete-app", "manage-app-access"}:
        return None

    owner_actions = {
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
      AND external = 0
      AND deleted_at IS NULL
    """
    restriction_sql = """
    SELECT 'apps' AS parent,
           id AS child
    FROM apps
    WHERE actor_id = :actor_id
      AND :actor_id IS NOT NULL
      AND external = 0
      AND deleted_at IS NULL
    """
    if action == "view-app":
        restriction_sql = """
        SELECT 'apps' AS parent,
               id AS child
        FROM apps
        WHERE deleted_at IS NULL
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
            "owner_reason": owner_actions[action],
        },
    )
