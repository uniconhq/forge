"""The names people give orgs, contests and tasks, and the ids they are
filed under. A thing is named once, when it is asked for: `reserve_org`,
`reserve_contest` and `reserve_task` make its key, write its row and hand
back its id, and the name is taken from then on, among the orgs or among
the contests or tasks of the same parent. Everything else the platform
keeps refers to the id, which never changes; the name is read here only to
find an id from the names in an address, and to show a person what they
are looking at.

`scope_at` is the one way in from an address: it turns `acme`,
`acme/spring` or `acme/spring/sum` into the scope of keys a role is checked
at, labelled with those names for the messages a person reads, or refuses
with `NotFound` in the same words whichever part is not there, so an
address tells nobody whether a hidden contest or an unreleased task by that
name exists. `deepest` names the first missing part, for the organiser's
check, which says it only to someone holding the role above. `names_of`,
`name_of` and `scope_names` go the other way.
"""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forge.db.tables import Name
from forge.domain.errors import Conflict, NotFound
from forge.domain.ids import ContestId, OrgId, TaskId, WorkflowId
from forge.domain.names import Named, ScopeNames
from forge.domain.roles import (
    PRIMITIVE_OWNER,
    HeldRole,
    RoleGrant,
    Scope,
    ScopeKind,
    scope_of_place,
)
from forge.domain.workflow_definition import WorkflowRef
from forge.runtime.actions import action
from forge.runtime.context import Context

NO_SUCH_CONTEST = "There is no such contest."
NO_SUCH_TASK = "There is no such task."

ORG = ScopeKind.ORG.value
CONTEST = ScopeKind.CONTEST.value
TASK = ScopeKind.TASK.value
NO_PARENT = ""


async def reserve_org(ctx: Context, name: str) -> OrgId:
    """A new org's id, its name taken from now on. `Conflict` when another
    org has it.
    """
    return OrgId(await _reserve(ctx, ORG, NO_PARENT, name))


async def reserve_contest(ctx: Context, org: OrgId, name: str) -> ContestId:
    """A new contest's id in the org, its name taken there from now on.
    `Conflict` when another contest of the org has it.
    """
    return ContestId(await _reserve(ctx, CONTEST, org, name))


async def reserve_task(ctx: Context, contest: ContestId, name: str) -> TaskId:
    """A new task's id in the contest, its name taken there from now on.
    `Conflict` when another task of the contest has it.
    """
    return TaskId(await _reserve(ctx, TASK, contest, name))


async def org_id(ctx: Context, name: str) -> OrgId | None:
    """The id of the org of that name, or none."""
    found = await _find(ctx, ORG, NO_PARENT, name)
    return OrgId(found) if found is not None else None


async def contest_id(ctx: Context, org: OrgId, name: str) -> ContestId | None:
    """The id of the org's contest of that name, or none."""
    found = await _find(ctx, CONTEST, org, name)
    return ContestId(found) if found is not None else None


async def task_id(ctx: Context, contest: ContestId, name: str) -> TaskId | None:
    """The id of the contest's task of that name, or none."""
    found = await _find(ctx, TASK, contest, name)
    return TaskId(found) if found is not None else None


async def task_ids(ctx: Context, contest: ContestId, names: Iterable[str]) -> dict[str, TaskId]:
    """The id of each of the contest's tasks among `names` that has one, by
    name, in one read.
    """
    wanted = set(names)
    if not wanted:
        return {}
    rows = await ctx.db.execute(
        select(Name.id, Name.name).where(
            Name.kind == TASK, Name.parent == contest, Name.name.in_(sorted(wanted))
        )
    )
    return {row.name: TaskId(row.id) for row in rows}


@action
async def scope_at(
    ctx: Context, org: str, contest: str | None = None, task: str | None = None
) -> Scope:
    """The scope an address names, by the names in it, labelled with them.
    `NotFound` when any part is not there, in the words a contest or task
    that is there but hidden from the caller gets, so the two read alike.
    """
    found, missing = await deepest(ctx, org, contest, task)
    if missing is not None:
        raise NotFound(NO_SUCH_TASK if task is not None else NO_SUCH_CONTEST)
    assert found is not None
    return found


async def deepest(
    ctx: Context, org: str, contest: str | None = None, task: str | None = None
) -> tuple[Scope | None, str | None]:
    """The deepest scope the names in an address reach, labelled with them,
    and what the first missing part is, `contest acme/autumn` for one, or
    none when every part is there.
    """
    org_key = await org_id(ctx, org)
    if org_key is None:
        return None, f"org {org}"
    found = Scope(org_key, label=org)
    if contest is None:
        return found, None
    contest_key = await contest_id(ctx, org_key, contest)
    if contest_key is None:
        return found, f"contest {org}/{contest}"
    contest_part = scope_of_place(contest_key).contest
    found = Scope(org_key, contest_part, label=f"{org}/{contest}")
    if task is None:
        return found, None
    task_key = await task_id(ctx, contest_key, task)
    if task_key is None:
        return found, f"task {org}/{contest}/{task}"
    return Scope(
        org_key, contest_part, scope_of_place(task_key).task, label=f"{org}/{contest}/{task}"
    ), None


async def names_of(ctx: Context, ids: Iterable[str]) -> dict[str, str]:
    """The name of each org, contest and task among the ids that has one."""
    wanted = set(ids)
    if not wanted:
        return {}
    rows = await ctx.db.execute(select(Name.id, Name.name).where(Name.id.in_(wanted)))
    return {row.id: row.name for row in rows}


async def named(ctx: Context, ids: Iterable[str]) -> tuple[Named, ...]:
    """Each id with its name, ordered by name. One with no name was not made
    by the platform and is left out.
    """
    wanted = tuple(ids)
    known = await names_of(ctx, wanted)
    found = (Named(id_, known[id_]) for id_ in wanted if id_ in known)
    return tuple(sorted(found, key=lambda each: each.name))


async def name_of(ctx: Context, id_: str) -> str:
    """The name of one org, contest or task. `NotFound` when it has none."""
    found = (await names_of(ctx, [id_])).get(id_)
    if found is None:
        raise NotFound("There is no such org, contest or task.")
    return found


async def scope_names(ctx: Context, scope: Scope) -> ScopeNames:
    """The names of everything the scope reaches. `NotFound` when any part
    has none.
    """
    found = (await scopes_named(ctx, [scope])).get(scope)
    if found is None:
        raise NotFound("There is no such org, contest or task.")
    return found


async def places_named(ctx: Context, places: Iterable[str]) -> dict[str, ScopeNames]:
    """The names of everything each contest or task id reaches, in one read,
    for each of them that has every name.
    """
    scopes = {place: scope_of_place(place) for place in places}
    named = await scopes_named(ctx, scopes.values())
    return {place: named[scope] for place, scope in scopes.items() if scope in named}


async def held_roles(ctx: Context, grants: Iterable[RoleGrant]) -> tuple[HeldRole, ...]:
    """Each grant with the names of its scope. A grant at a scope without
    names, which the platform did not make, is left out.
    """
    held = tuple(grants)
    found = await scopes_named(ctx, [grant.scope for grant in held])
    return tuple(
        HeldRole(grant.scope, grant.role, found[grant.scope])
        for grant in held
        if grant.scope in found
    )


async def scopes_named(ctx: Context, scopes: Iterable[Scope]) -> dict[Scope, ScopeNames]:
    """The names of everything each scope reaches, in one read, for each of
    them that has every name.
    """
    wanted = tuple(scopes)
    parts = {part for scope in wanted for part in _lineage_ids(scope)}
    known = await names_of(ctx, parts)
    found: dict[Scope, ScopeNames] = {}
    for scope in wanted:
        ids = _lineage_ids(scope)
        if all(part in known for part in ids):
            labels = [known[part] for part in ids]
            found[scope] = ScopeNames(*labels)
    return found


def _lineage_ids(scope: Scope) -> list[str]:
    """The ids of the org, the contest and the task a scope reaches, as far
    down as it does.
    """
    ids = [scope.org]
    if scope.contest is not None:
        ids.append(f"{scope.org}/{scope.contest}")
        if scope.task is not None:
            ids.append(f"{scope.org}/{scope.contest}/{scope.task}")
    return ids


async def labelled(ctx: Context, scope: Scope) -> Scope:
    """The scope with its names as its label, for a message a person reads."""
    if scope.label is not None:
        return scope
    try:
        names = await scope_names(ctx, scope)
    except NotFound:
        return scope
    return Scope(scope.org, scope.contest, scope.task, label=names.path)


async def workflow_id(ctx: Context, ref: WorkflowRef) -> WorkflowId:
    """The id of the workflow a reference names, `<owner>/<name>`: the
    platform's own org as it is, an org by the key its name is filed under,
    and anyone else as the person of that username, whose workflows the
    forge keeps under it.
    """
    return await workflow_place(ctx, ref.owner, ref.name)


async def workflow_place(ctx: Context, owner: str, name: str) -> WorkflowId:
    """The id of the workflow `<owner>/<name>`, read as `workflow_id` reads
    a reference's.
    """
    if owner == PRIMITIVE_OWNER:
        return WorkflowId(f"{owner}/{name}")
    org = await org_id(ctx, owner)
    return WorkflowId(f"{org if org is not None else owner}/{name}")


async def _reserve(ctx: Context, kind: str, parent: str, name: str) -> str:
    if await _find(ctx, kind, parent, name) is not None:
        raise Conflict(f"The name {name!r} is taken.")
    key = ctx.make_key(name)
    id_ = key if parent == NO_PARENT else f"{parent}/{key}"
    async with ctx.db.begin_nested():
        ctx.db.add(Name(id=id_, kind=kind, parent=parent, name=name))
        try:
            await ctx.db.flush()
        except IntegrityError as exc:
            raise Conflict(f"The name {name!r} is taken.") from exc
    return id_


async def _find(ctx: Context, kind: str, parent: str, name: str) -> str | None:
    found = await ctx.db.execute(
        select(Name.id).where(Name.kind == kind, Name.parent == parent, Name.name == name)
    )
    return found.scalar_one_or_none()
