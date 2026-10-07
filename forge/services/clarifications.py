"""Clarifications: a contestant's question and the organisers' answer,
private to the asker and the organisers, held at the forge as a thread
labelled `clarification` on the desk of the workspace the contestant works
in, their own or their team's, which is what keeps everyone else out of it;
a team's members all read and follow up its questions, and a question is
named by its desk: the asker's user id, or `team.<id>` for a team's. Nothing about one is stored in
the database: the thread is the record.

- `ask`: an approved contestant asks, as themselves; their desk is made by
  their first question, as the platform, with them, or every member of their
  team, a reader on it, which at
  the forge lets them post and comment and close their own question but not
  touch an organiser's reply or a label. The platform labels the question.
  A question may name a task released to them, kept as a marker in its body
  that is checked against the contest whenever it is read, since the asker
  can edit their own question at the forge.
- `mine`: the questions on the contestant's desk, their team's included,
  with every message under them.
- `follow_up`: the contestant comments again, as themselves; on an answered
  question the platform then takes the mark off and opens it, so the
  follow-up lands back with the organisers instead of starting a thread with
  none of the history.
- `inbox`: every question still open across an org, in one search at the
  forge as the organiser, for anyone holding a role anywhere in the org, each
  shown only where they observe its contest.
- `of_contest`: every question of one contest, answered ones included, for
  an organiser observing it.
- `reply`: an organiser's comment, which leaves the question open.
- `mark` and `unmark`: label it answered and close it, with or without a
  reply, or take that back; each is one change, and doing either twice
  changes nothing, so a retry is always safe and a reply is never posted
  twice.
- `answer_publicly`: an organiser turns an answer into an announcement on the
  contest, or on the task the question names, pointing back at the question,
  which stays private.

Each of the organiser's changes needs the manager role at the contest, and
is made as the organiser, so the record says who answered.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from forge.db.tables import Contestant
from forge.domain import release as rules
from forge.domain.errors import (
    Forbidden,
    NotApproved,
    NotFound,
    SessionExpired,
    TeamChanged,
)
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import ContestId, OrgId, TaskId, ThreadId, WorkspaceId
from forge.domain.names import ScopeNames, TeamOwner, UserOwner, WorkspaceOwner
from forge.domain.registration import Status
from forge.domain.roles import Role, contest_scope, holds, task_scope
from forge.domain.sessions import Session
from forge.domain.threads import Thread, ThreadKind
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import announcements, names, published, release, sessions, teams
from forge.services.access import Organiser, require
from forge.services.announcements import Announcement, AnsweredQuestion

log = get_logger(__name__)

NO_SUCH_QUESTION = "There is no such question."
ABOUT = re.compile(r"\n*<!-- unicon:task (\S+) -->\s*$")


@dataclass(frozen=True, slots=True)
class Message:
    """One message under a question: whether the asker wrote it, what it
    says, and when.
    """

    from_asker: bool
    body: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Clarification:
    """One question: its contest by name, whose desk it is in, the asker's
    user id or `team.<id>` for a team's, its number there, the task it
    names, what it asks, when, whether it is answered and whether it is
    open, and every message under it, oldest first.
    """

    contest: ScopeNames
    asker: str
    number: int
    task: str | None
    title: str
    body: str
    asked_at: datetime
    answered: bool
    closed: bool
    messages: tuple[Message, ...]


@action
async def ask(
    ctx: Context,
    session: Session,
    contest: ContestId,
    *,
    title: str,
    body: str,
    task: str | None = None,
) -> Clarification:
    """Ask a question in the contest, as the contestant. `NotApproved` for
    anyone who is not its approved contestant, and `NotFound` for a contest
    they may not see or a task that is not released to them.
    """
    settings, person = await release.seen(ctx, session, contest)
    if person.row is None or person.row.status != Status.APPROVED:
        raise NotApproved("Only an approved contestant of the contest asks it questions.")
    title, body = announcements.checked(title, body)
    about: TaskId | None = None
    if task is not None:
        about = await names.task_id(ctx, contest, task)
        found = await published.task(ctx, about, settings) if about is not None else None
        if found is None or not rules.visible(settings, found.name, ctx.now):
            raise NotFound(published.NO_SUCH_TASK)
        body += f"\n\n<!-- unicon:task {about} -->"
    where = await names.scope_names(ctx, contest_scope(contest))
    as_ = AsUser(session.user_id, await sessions.credential_for(ctx, session.id))
    standing = await teams.standing(ctx, contest, session.user_id)
    await ctx.let_go()
    workspace = await ctx.forge.workspaces.open_workspace(contest, standing.owner, standing.members)
    if isinstance(standing.owner, TeamOwner):
        current = await teams.settle(ctx, standing.owner.team_id, workspace, standing.members)
        if session.user_id not in current:
            raise TeamChanged("Your team changed while your question was asked; ask again.")
    thread = await ctx.forge.threads.post_thread(
        as_, workspace, ThreadKind.CLARIFICATION, title=title, body=body
    )
    log.info("clarifications.asked", contest=contest, number=thread.number, user_id=as_.user_id)
    return (await _shown(ctx, [thread], {contest: where}))[0]


@action
async def mine(ctx: Context, session: Session, contest: ContestId) -> tuple[Clarification, ...]:
    """The person's own questions in the contest, oldest first; none for
    someone who never registered for it or never asked.
    """
    row = await _row(ctx, contest, session.user_id)
    if row is None:
        return ()
    where = await names.scope_names(ctx, contest_scope(contest))
    as_ = AsUser(session.user_id, await sessions.credential_for(ctx, session.id))
    standing = await teams.standing(ctx, contest, session.user_id)
    await ctx.let_go()
    workspace = ctx.forge.workspaces.workspace_of(contest, standing.owner)
    try:
        threads = await ctx.forge.threads.list_threads(as_, workspace, ThreadKind.CLARIFICATION)
    except NotFound:
        return ()
    return await _shown(ctx, threads, {contest: where})


@action
async def follow_up(
    ctx: Context, session: Session, contest: ContestId, number: int, *, body: str
) -> Clarification:
    """Comment on one's own question, as the contestant, opening it again
    when it was answered. `NotApproved` for anyone who is not the contest's
    approved contestant, and `NotFound` for a question that is not theirs.
    """
    row = await _row(ctx, contest, session.user_id)
    if row is None or row.status != Status.APPROVED:
        raise NotApproved("Only an approved contestant of the contest asks it questions.")
    _, body = announcements.checked("-", body)
    as_ = AsUser(session.user_id, await sessions.credential_for(ctx, session.id))
    standing = await teams.standing(ctx, contest, session.user_id)
    await ctx.let_go()
    workspace = ctx.forge.workspaces.workspace_of(contest, standing.owner)
    thread = await _found(ctx, as_, ctx.forge.threads.thread_of(workspace, number))
    await ctx.forge.threads.comment(as_, thread.id, body)
    if thread.answered or thread.closed:
        # The asker only reads their desk, so the label is the platform's to
        # take off.
        await ctx.forge.threads.unmark_answered(PLATFORM, thread.id)
        log.info("clarifications.reopened", contest=contest, number=number, user_id=as_.user_id)
    return await _again(ctx, as_, thread)


@action
async def inbox(ctx: Context, session: Session, org: str) -> tuple[Clarification, ...]:
    """Every question still open across the org that the person observes the
    contest of, oldest first, from one search at the forge as them.
    `Forbidden` for someone who holds no role anywhere in the org, which is
    also the answer for an org that is not there.
    """
    found = await names.org_id(ctx, org)
    credential = await sessions.credential_for(ctx, session.id)
    await ctx.let_go()
    as_ = AsUser(session.user_id, credential)
    try:
        grants = await ctx.forge.orgs.roles_of(as_)
    except Forbidden:
        sessions.revoke_at_end(ctx, session.id)
        raise SessionExpired("Sign in again.") from None
    if found is None or not any(grant.scope.org == found for grant in grants):
        raise Forbidden(f"This needs a role at {org}.")
    threads = [
        thread
        for thread, contest in _questions(
            ctx,
            await ctx.forge.threads.search_threads(
                as_, OrgId(found), ThreadKind.CLARIFICATION, comments=False
            ),
        )
        if holds(grants, contest_scope(contest), Role.OBSERVER)
    ]
    return await _shown(ctx, await _with_messages(ctx, as_, threads))


@action
async def of_contest(
    ctx: Context, organiser: Organiser, contest: ContestId
) -> tuple[Clarification, ...]:
    """Every question of the contest, answered ones included, oldest first,
    from one search at the forge as the organiser. Needs the observer role at
    the contest.
    """
    scope = contest_scope(contest)
    require(organiser, scope, Role.OBSERVER)
    await ctx.let_go()
    threads = [
        thread
        for thread, asked_in in _questions(
            ctx,
            await ctx.forge.threads.search_threads(
                organiser.identity,
                OrgId(scope.org),
                ThreadKind.CLARIFICATION,
                open_only=False,
                comments=False,
            ),
        )
        if asked_in == contest
    ]
    return await _shown(ctx, await _with_messages(ctx, organiser.identity, threads))


@action
async def reply(
    ctx: Context, organiser: Organiser, contest: ContestId, asker: str, number: int, *, body: str
) -> Clarification:
    """Comment on a question as the organiser, leaving it as open or
    answered as it was. Needs the manager role at the contest.
    """
    thread = await _managed(ctx, organiser, contest, asker, number)
    _, body = announcements.checked("-", body)
    await ctx.forge.threads.comment(organiser.identity, thread.id, body)
    log.info("clarifications.replied", contest=contest, number=number, by=organiser.user.id)
    return await _again(ctx, organiser.identity, thread)


@action
async def mark(
    ctx: Context, organiser: Organiser, contest: ContestId, asker: str, number: int
) -> Clarification:
    """Mark a question answered and close it, as the organiser; one already
    marked is left as it is. Needs the manager role at the contest.
    """
    thread = await _managed(ctx, organiser, contest, asker, number)
    if not (thread.answered and thread.closed):
        await ctx.forge.threads.mark_answered(organiser.identity, thread.id)
        log.info("clarifications.marked", contest=contest, number=number, by=organiser.user.id)
    return await _again(ctx, organiser.identity, thread)


@action
async def unmark(
    ctx: Context, organiser: Organiser, contest: ContestId, asker: str, number: int
) -> Clarification:
    """Take the answered mark off a question and open it again, as the
    organiser; one neither marked nor closed is left as it is. Needs the
    manager role at the contest.
    """
    thread = await _managed(ctx, organiser, contest, asker, number)
    if thread.answered or thread.closed:
        await ctx.forge.threads.unmark_answered(organiser.identity, thread.id)
        log.info("clarifications.unmarked", contest=contest, number=number, by=organiser.user.id)
    return await _again(ctx, organiser.identity, thread)


@action
async def answer_publicly(
    ctx: Context,
    organiser: Organiser,
    contest: ContestId,
    asker: str,
    number: int,
    *,
    title: str,
    body: str,
) -> Announcement:
    """Turn an answer into an announcement every contestant reads, on the
    task the question names or else on the contest, pointing back at the
    question, which stays where it is. Needs the manager role at that place.
    """
    thread = await _managed(ctx, organiser, contest, asker, number)
    about = _about(thread, contest)
    scope = task_scope(about) if about is not None else contest_scope(contest)
    return await announcements.post(
        ctx,
        organiser,
        scope,
        title=title,
        body=body,
        answers=AnsweredQuestion(_asker(_owner(asker)), number),
    )


async def _row(ctx: Context, contest: ContestId, user_id: int) -> Contestant | None:
    return (
        await ctx.db.execute(
            select(Contestant).where(
                Contestant.contest_id == contest, Contestant.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def _managed(
    ctx: Context, organiser: Organiser, contest: ContestId, asker: str, number: int
) -> Thread:
    """The question numbered `number` in the asker's workspace, read as the
    organiser, once they are found to manage the contest.
    """
    require(organiser, contest_scope(contest), Role.MANAGER)
    await ctx.let_go()
    workspace = ctx.forge.workspaces.workspace_of(contest, _owner(asker))
    return await _found(ctx, organiser.identity, ctx.forge.threads.thread_of(workspace, number))


def _owner(asker: str) -> WorkspaceOwner:
    """The desk a question's key names: a person's by their user id, or a
    team's by `team.<id>`. `NotFound` for anything else.
    """
    if asker.isascii() and asker.isdigit():
        return UserOwner(int(asker))
    if asker.startswith("team."):
        try:
            return TeamOwner(uuid.UUID(asker.removeprefix("team.")))
        except ValueError:
            pass
    raise NotFound(NO_SUCH_QUESTION)


def _asker(owner: WorkspaceOwner) -> str:
    return str(owner.user_id) if isinstance(owner, UserOwner) else f"team.{owner.team_id}"


async def _found(ctx: Context, as_: AsUser, thread: ThreadId) -> Thread:
    try:
        found = await ctx.forge.threads.read_thread(as_, thread)
    except NotFound:
        raise NotFound(NO_SUCH_QUESTION) from None
    if found.kind is not ThreadKind.CLARIFICATION:
        raise NotFound(NO_SUCH_QUESTION)
    return found


async def _again(ctx: Context, as_: AsUser, thread: Thread) -> Clarification:
    """The question as it now stands, read again as `as_`."""
    found = await _found(ctx, as_, thread.id)
    return (await _shown(ctx, [found]))[0]


def _contest_of(ctx: Context, thread: Thread) -> ContestId:
    return ctx.forge.workspaces.contest_of(WorkspaceId(thread.place))


def _questions(ctx: Context, threads: tuple[Thread, ...]) -> list[tuple[Thread, ContestId]]:
    """The threads that are questions on a desk, each with its contest. One
    labelled `clarification` anywhere else, by hand at the forge, is passed
    over rather than failing the whole list.
    """
    kept = []
    for thread in threads:
        try:
            kept.append((thread, _contest_of(ctx, thread)))
        except NotFound:
            continue
    return kept


async def _with_messages(ctx: Context, as_: AsUser, threads: list[Thread]) -> list[Thread]:
    """Each thread read again with its messages, once the search has been
    narrowed to the ones shown.
    """
    return [await ctx.forge.threads.read_thread(as_, thread.id) for thread in threads]


def _about(thread: Thread, contest: ContestId) -> TaskId | None:
    """The task a question names, from the marker the platform wrote into it,
    or none when it names none or names anything that is not a task of its
    own contest. The asker can edit their question at the forge, so the
    marker is checked whenever it is read, never trusted.
    """
    found = ABOUT.search(thread.body)
    if found is None:
        return None
    parts = found.group(1).split("/")
    if len(parts) != 3 or not all(parts) or f"{parts[0]}/{parts[1]}" != contest:
        return None
    return TaskId(found.group(1))


async def _shown(
    ctx: Context,
    threads: list[Thread] | tuple[Thread, ...],
    known: dict[ContestId, ScopeNames] | None = None,
) -> tuple[Clarification, ...]:
    """The threads as questions, with their contests' and tasks' names, read
    in one go for the ones not `known`.
    """
    asked = [
        (
            thread,
            _contest_of(ctx, thread),
            ctx.forge.workspaces.owner_of(WorkspaceId(thread.place)),
        )
        for thread in threads
    ]
    about = [(thread, contest, owner, _about(thread, contest)) for thread, contest, owner in asked]
    wanted = {contest for _, contest, _ in asked if contest not in (known or {})}
    tasks = {task for _, _, _, task in about if task is not None}
    places: dict[str, ScopeNames] = {
        str(contest): where for contest, where in (known or {}).items()
    }
    if wanted or tasks:
        places.update(await names.places_named(ctx, [*wanted, *tasks]))
    askers: dict[str, frozenset[int]] = {}
    for _, _, owner, _ in about:
        key = _asker(owner)
        if key not in askers:
            askers[key] = (
                frozenset({owner.user_id})
                if isinstance(owner, UserOwner)
                else await teams.everyone_in(ctx, owner.team_id)
            )
    shown = []
    for thread, contest, owner, task in about:
        asker = _asker(owner)
        task_names = places.get(task) if task is not None else None
        shown.append(
            Clarification(
                contest=places.get(contest) or ScopeNames(contest_scope(contest).org),
                asker=asker,
                number=thread.number,
                task=task_names.task if task_names is not None else None,
                title=thread.title,
                body=ABOUT.sub("", thread.body).rstrip(),
                asked_at=thread.created_at,
                answered=thread.answered,
                closed=thread.closed,
                messages=tuple(
                    Message(
                        from_asker=comment.author_id in askers[asker],
                        body=comment.body,
                        at=comment.at,
                    )
                    for comment in thread.comments
                ),
            )
        )
    return tuple(shown)
