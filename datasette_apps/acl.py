"""datasette-acl integration for per-app sharing.

datasette-acl and datasette-acl-share (the share dialog UI) are hard
dependencies; acl grants are the source of truth for per-app access.

The mapping between the legacy model and acl grants:

    app owner (actor_id)    -> Manager role grant on the app
    is_private = 0          -> Viewer role grant for the ``authenticated``
                               public audience ("anyone signed in")
    is_private = 1          -> no audience grant

``is_private`` stays as the UI toggle; flipping it grants/revokes the
``authenticated`` audience. The share dialog can layer further per-actor
grants on top.
"""

from __future__ import annotations

from datasette_acl.grants import Principal, grant as _grant, revoke as _revoke
from datasette_acl.internal_migrations import (
    internal_migrations as _acl_internal_migrations,
)
from datasette_acl.roles import build_roles_registry, standard_roles
from sqlite_utils import Database as _SqliteUtilsDatabase

from datasette_acl_share import datasette_share_assets

APP_RESOURCE_TYPE = "app"
APPS_PARENT = "apps"
# "Anyone signed in" maps to acl's first-class ``authenticated`` audience.
GENERAL_PRINCIPAL = Principal.authenticated()

_MIGRATION_TABLE = "_datasette_apps_acl_migration"
_MIGRATION_KEY = "grants-backfill-v1"


def app_acl_roles():
    """Viewer / Editor / Manager roles for the ``app`` resource type.

    Manager is the single ``manage=True`` role; ``delete-app`` and
    ``manage-app-access`` appear in no other role, which is what authorizes
    re-sharing via the dialog.
    """
    return standard_roles(
        APP_RESOURCE_TYPE,
        view="view-app",
        edit="edit-app",
        manage=["delete-app", "manage-app-access"],
        descriptions={
            "Viewer": "Can view the app",
            "Editor": "Can view and edit the app",
            "Manager": "Full control, including sharing and deletion",
        },
    )


def _acl_ready(datasette):
    # grant(role=...) resolves names against the registry that acl's own
    # startup hook builds; before startup seeding must no-op.
    return getattr(datasette, "_acl_roles_registry", None) is not None


async def seed_owner_manager_grant(datasette, app_id, owner_actor_id):
    """Grant the app creator the Manager role. No-op for anonymous creators."""
    if not owner_actor_id or not _acl_ready(datasette):
        return
    await _grant(
        datasette,
        APP_RESOURCE_TYPE,
        APPS_PARENT,
        str(app_id),
        principal=Principal.actor(str(owner_actor_id)),
        role="Manager",
        by_actor=str(owner_actor_id),
    )


async def sync_general_access_grant(datasette, app_id, is_private, by_actor=None):
    """Mirror the is_private toggle onto the ``authenticated`` audience grant."""
    if not _acl_ready(datasette):
        return
    by_actor = str(by_actor) if by_actor else None
    if is_private:
        await _revoke(
            datasette,
            APP_RESOURCE_TYPE,
            APPS_PARENT,
            str(app_id),
            principal=GENERAL_PRINCIPAL,
            by_actor=by_actor,
        )
    else:
        await _grant(
            datasette,
            APP_RESOURCE_TYPE,
            APPS_PARENT,
            str(app_id),
            principal=GENERAL_PRINCIPAL,
            role="Viewer",
            by_actor=by_actor,
        )


async def _ensure_acl_tables(db):
    """Apply acl's schema migrations if its tables don't exist yet.

    This plugin's startup hook may run before datasette-acl's (cross-plugin
    hook ordering is not guaranteed), in which case the grants backfill would
    find no acl tables. acl's migrations are append-only and idempotent
    (sqlite-migrate tracks applied ones), so applying them here is safe —
    acl's own startup re-applying them later is a no-op.
    """
    present = bool(
        (
            await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'acl_resources'"
            )
        ).rows
    )
    if present:
        return

    def apply_migrations(connection):
        _acl_internal_migrations.apply(_SqliteUtilsDatabase(connection))

    await db.execute_write_fn(apply_migrations)


async def _ensure_app_roles_registry(datasette):
    """Make sure acl's roles registry knows the ``app`` roles.

    The backfill runs from this plugin's ``startup`` hook and the relative
    ordering of two plugins' startup hooks is not guaranteed; if ours runs
    first the registry is missing and grant(role=...) would raise. Rebuilding
    is cheap and idempotent.
    """
    registry = getattr(datasette, "_acl_roles_registry", None)
    if not registry or APP_RESOURCE_TYPE not in registry:
        datasette._acl_roles_registry = await build_roles_registry(datasette)
    return APP_RESOURCE_TYPE in (getattr(datasette, "_acl_roles_registry", None) or {})


async def backfill_acl_grants(datasette, *, force=False):
    """One-time backfill of pre-acl apps into acl grants.

    For every live stored app: owner -> Manager grant, and is_private=0 ->
    ``authenticated`` Viewer grant. External apps are skipped (no owner; their
    visibility stays config-driven). Doubly idempotent: a marker row
    short-circuits reruns, and grant() only inserts missing actions.
    """
    stats = {"owners": 0, "audiences": 0, "skipped": False}
    db = datasette.get_internal_database()
    await _ensure_acl_tables(db)
    if not await _ensure_app_roles_registry(datasette):
        stats["skipped"] = True
        return stats
    await db.execute_write(
        f"CREATE TABLE IF NOT EXISTS {_MIGRATION_TABLE} "
        "(key TEXT PRIMARY KEY, migrated_at TEXT NOT NULL)"
    )
    if not force:
        done = (
            await db.execute(
                f"SELECT 1 FROM {_MIGRATION_TABLE} WHERE key = ?", [_MIGRATION_KEY]
            )
        ).rows
        if done:
            stats["skipped"] = True
            return stats
    apps = (
        await db.execute(
            "SELECT id, actor_id, is_private FROM apps "
            "WHERE external = 0 AND deleted_at IS NULL"
        )
    ).rows
    for row in apps:
        if row["actor_id"]:
            await _grant(
                datasette,
                APP_RESOURCE_TYPE,
                APPS_PARENT,
                str(row["id"]),
                principal=Principal.actor(str(row["actor_id"])),
                role="Manager",
                by_actor=str(row["actor_id"]),
            )
            stats["owners"] += 1
        if not row["is_private"]:
            await _grant(
                datasette,
                APP_RESOURCE_TYPE,
                APPS_PARENT,
                str(row["id"]),
                principal=GENERAL_PRINCIPAL,
                role="Viewer",
                by_actor=None,
            )
            stats["audiences"] += 1
    await db.execute_write(
        f"INSERT OR IGNORE INTO {_MIGRATION_TABLE} (key, migrated_at) "
        "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
        [_MIGRATION_KEY],
    )
    return stats
