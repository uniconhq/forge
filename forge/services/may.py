"""What a person may do at an org, a contest or a task, for a page to show
its buttons by. Each flag asks what its action's own check asks: the same
role through `holds`, at the same scope. The actions still check; a flag
only decides what a page shows.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from forge.domain.definitions import CONTEST_ADMIN_KEYS, TASK_ADMIN_KEYS
from forge.domain.roles import Role, RoleGrant, Scope, ScopeKind, holds
from forge.services import roles


@dataclass(frozen=True, slots=True)
class OrgMay:
    """At an org: change what it says about itself (`orgs.update`), make a
    contest in it (`contests.create`), and read, change and give admin to
    its organisers (`roles.holders`, `roles.grant` and `roles.revoke`).
    """

    edit: bool
    create_contest: bool
    see_people: bool
    manage_people: bool
    give_admin: bool


@dataclass(frozen=True, slots=True)
class PlaceMay:
    """At a contest or a task: change it (`files.write`, `task_save.save`
    and every other manager's action there), change its admin-only
    settings, and read, change and give admin to its organisers.
    `locked_keys` are the settings file's top keys a save of theirs may not
    change: the admin-only ones, unless they are an admin.
    """

    manage: bool
    edit_admin_settings: bool
    locked_keys: tuple[str, ...]
    see_people: bool
    manage_people: bool
    give_admin: bool


def at_org(grants: Iterable[RoleGrant], scope: Scope) -> OrgMay:
    held = tuple(grants)
    return OrgMay(
        edit=holds(held, scope, Role.ADMIN),
        create_contest=holds(held, scope, Role.MANAGER),
        see_people=holds(held, scope, Role.OBSERVER),
        manage_people=roles.may_change(held, scope, Role.MANAGER),
        give_admin=roles.may_change(held, scope, Role.ADMIN),
    )


def at_place(grants: Iterable[RoleGrant], scope: Scope) -> PlaceMay:
    """At a contest or a task."""
    held = tuple(grants)
    admin = holds(held, scope, Role.ADMIN)
    keys = CONTEST_ADMIN_KEYS if scope.kind is ScopeKind.CONTEST else TASK_ADMIN_KEYS
    return PlaceMay(
        manage=holds(held, scope, Role.MANAGER),
        edit_admin_settings=admin,
        locked_keys=() if admin else keys,
        see_people=holds(held, scope, Role.OBSERVER),
        manage_people=roles.may_change(held, scope, Role.MANAGER),
        give_admin=roles.may_change(held, scope, Role.ADMIN),
    )
