from __future__ import annotations

from datasette.permissions import Action, PermissionSQL, Resource

from .acl import APPS_PARENT


class AppsResource(Resource):
    name = "apps"
    parent_class = None

    def __init__(self):
        super().__init__(parent=APPS_PARENT, child=None)

    @classmethod
    async def resources_sql(cls, datasette, actor=None):
        return f"SELECT '{APPS_PARENT}' AS parent, NULL AS child"


class AppResource(Resource):
    name = "app"
    parent_class = AppsResource

    def __init__(self, parent=None, child=None):
        # Accept both the ergonomic AppResource(app_id) and datasette-acl's
        # positional build_resource convention AppResource("apps", app_id).
        if child is None:
            child = parent
        super().__init__(
            parent=APPS_PARENT, child=str(child) if child is not None else None
        )

    @classmethod
    async def resources_sql(cls, datasette, actor=None):
        return f"SELECT '{APPS_PARENT}' AS parent, id AS child FROM apps WHERE deleted_at IS NULL"


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
            also_requires="view-app",
        ),
        Action(
            name="delete-app",
            description="Delete a Datasette app",
            resource_class=AppResource,
            also_requires="view-app",
        ),
        Action(
            name="manage-app-access",
            description="Manage Datasette app access",
            resource_class=AppResource,
            also_requires="view-app",
        ),
    ]


# Resources where the actor holds an acl grant for the action being checked.
# UNIONed into restriction_sql below so datasette-acl grants pass the
# owner-only / private restriction filter that would otherwise exclude them.
# Mirrors the principal matching in datasette-acl's own permission hook
# (wildcards, direct actor grants, live group membership). The :action and
# :actor_id params are populated by datasette core for every PermissionSQL.
_ACL_GRANTS_RESTRICTION_SQL = """
    SELECT ar.parent AS parent,
           ar.child AS child
    FROM acl
    JOIN acl_actions ON acl.action_id = acl_actions.id
    JOIN acl_resources ar ON acl.resource_id = ar.id
    LEFT JOIN acl_groups g ON acl.group_id = g.id
    WHERE acl_actions.name = :action
      AND ar.resource_type = 'app'
      AND ar.child IN (SELECT id FROM apps WHERE deleted_at IS NULL)
      AND (
        acl.actor_id = '*'
        OR (:actor_id IS NOT NULL AND acl.actor_id = :actor_id)
        OR (:actor_id IS NOT NULL AND acl.actor_id = '_signed_in')
        OR (:actor_id IS NULL AND acl.actor_id = '_anonymous')
        OR acl.group_id IN (
            SELECT ag.group_id
            FROM acl_actor_groups ag
            JOIN acl_groups ig ON ag.group_id = ig.id
            WHERE :actor_id IS NOT NULL
              AND ag.actor_id = :actor_id
              AND ig.deleted IS NULL
        )
      )
      AND (acl.group_id IS NULL OR g.deleted IS NULL)
"""


def _with_acl_grants(restriction_sql):
    return f"{restriction_sql}\nUNION\n{_ACL_GRANTS_RESTRICTION_SQL}"


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
        restriction_sql=_with_acl_grants(restriction_sql),
        params={
            "actor_id": actor_id,
            "owner_reason": owner_actions[action],
        },
    )
