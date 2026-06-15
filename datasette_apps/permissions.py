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
        Action(
            name="apps-set-csp",
            description="Set arbitrary CSP origins on Datasette apps",
            resource_class=AppsResource,
        ),
    ]


# Resources where the actor holds an acl grant for the action being checked.
# UNIONed into restriction_sql below so datasette-acl grants pass the
# owner-only / private restriction filter that would otherwise exclude them.
# Mirrors the principal matching in datasette-acl's own permission hook
# (public audiences keyed by principal_type, direct actor grants, live group
# membership). The :action and :actor_id params are populated by datasette
# core for every PermissionSQL.
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
        acl.principal_type = 'everyone'
        OR (:actor_id IS NOT NULL
            AND acl.principal_type = 'actor' AND acl.actor_id = :actor_id)
        OR (:actor_id IS NOT NULL AND acl.principal_type = 'authenticated')
        OR (:actor_id IS NULL AND acl.principal_type = 'anonymous')
        OR (acl.principal_type = 'group' AND acl.group_id IN (
            SELECT ag.group_id
            FROM acl_actor_groups ag
            JOIN acl_groups ig ON ag.group_id = ig.id
            WHERE :actor_id IS NOT NULL
              AND ag.actor_id = :actor_id
              AND ig.deleted IS NULL
        ))
      )
      AND (acl.group_id IS NULL OR g.deleted IS NULL)
"""


def _with_acl_grants(restriction_sql):
    return f"{restriction_sql}\nUNION\n{_ACL_GRANTS_RESTRICTION_SQL}"


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
        restriction_sql=_with_acl_grants(restriction_sql),
        params={
            "actor_id": actor_id,
            "owner_reason": action_reasons[action],
        },
    )
