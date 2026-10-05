"""Announcements: an organiser's message to a contest or to one of its tasks,
held at the forge as a thread labelled `announcement` on the contest's or
the task's own repository.

An organiser managing the place posts, edits and closes one, each at the
forge as themselves, with their own credential, so the record says who
wrote it. There is no delete anywhere: a message people have read and acted
on is closed, and stays readable. An organiser observing the place reads
every one of its announcements, closed ones included (`manage`).

A signed-in person reads the open announcements of a contest they see,
together with those of each task released to them (`contest`), and of one
released task on its page (`task`), read live from the forge as the
platform on the request with nothing kept, since a contestant reaches no
repository at the forge. A task not released to them contributes nothing,
the same as a task that is not there.

An announcement made from a clarification's answer (`clarifications.
answer_publicly`) points back at the question with a line the platform adds
and reads back, which a person's own text can never carry: a reader learns
only that it answers a question, and an organiser which one.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from forge.domain import release as rules
from forge.domain.errors import InvalidMessage, NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId
from forge.domain.names import ScopeNames
from forge.domain.roles import Role, Scope, ScopeKind, contest_id_of, task_scope
from forge.domain.sessions import Session
from forge.domain.threads import Thread, ThreadKind, ThreadPlace
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import names, published, release
from forge.services.access import Organiser, require

log = get_logger(__name__)

TITLE_MOST = 200
BODY_MOST = 20_000
NO_SUCH_ANNOUNCEMENT = "There is no such announcement."
MARK = re.compile(r"<!--\s*unicon:.*?-->", re.DOTALL)
ANSWERS = re.compile(r"\n*<!-- unicon:answers u(\d+)#(\d+) -->\s*$")


@dataclass(frozen=True, slots=True)
class AnsweredQuestion:
    """The clarification an announcement answers: the asker's user id and the
    question's number in their workspace.
    """

    user_id: int
    number: int


@dataclass(frozen=True, slots=True)
class Announcement:
    """One announcement: where it is, by the names of its contest and task,
    its number there, what it says, when it was posted, whether it is
    closed, whether it answers a question, and, for an organiser, which.
    """

    where: ScopeNames
    number: int
    title: str
    body: str
    posted_at: datetime
    closed: bool
    answers_question: bool
    answers: AnsweredQuestion | None


@action
async def post(
    ctx: Context,
    organiser: Organiser,
    scope: Scope,
    *,
    title: str,
    body: str,
    answers: AnsweredQuestion | None = None,
) -> Announcement:
    """Post an announcement on the contest or task `scope` names, as the
    organiser. Needs the manager role there.
    """
    require(organiser, scope, Role.MANAGER)
    title, body = checked(title, body)
    if answers is not None:
        body += f"\n\n<!-- unicon:answers u{answers.user_id}#{answers.number} -->"
    where = await names.scope_names(ctx, scope)
    await ctx.let_go()
    thread = await ctx.forge.threads.post_thread(
        organiser.identity, place_of(scope), ThreadKind.ANNOUNCEMENT, title=title, body=body
    )
    log.info("announcements.posted", place=scope.path, number=thread.number, by=organiser.user.id)
    return announcement(thread, where, organiser=True)


@action
async def edit(
    ctx: Context, organiser: Organiser, scope: Scope, number: int, *, title: str, body: str
) -> Announcement:
    """Change an announcement's title and text, as the organiser, keeping
    what it answers. Needs the manager role at its place.
    """
    require(organiser, scope, Role.MANAGER)
    title, body = checked(title, body)
    found = await _found(ctx, organiser, scope, number)
    kept = ANSWERS.search(found.body)
    if kept is not None:
        body += f"\n\n<!-- unicon:answers u{kept.group(1)}#{kept.group(2)} -->"
    await ctx.forge.threads.edit_thread(organiser.identity, found.id, title=title, body=body)
    log.info("announcements.edited", place=scope.path, number=number, by=organiser.user.id)
    return await _shown(ctx, organiser, scope, number)


@action
async def close(ctx: Context, organiser: Organiser, scope: Scope, number: int) -> Announcement:
    """Close an announcement, as the organiser; it stays readable. Closing a
    closed one changes nothing. Needs the manager role at its place.
    """
    require(organiser, scope, Role.MANAGER)
    found = await _found(ctx, organiser, scope, number)
    if not found.closed:
        await ctx.forge.threads.close_thread(organiser.identity, found.id)
        log.info("announcements.closed", place=scope.path, number=number, by=organiser.user.id)
    return await _shown(ctx, organiser, scope, number)


@action
async def manage(ctx: Context, organiser: Organiser, scope: Scope) -> tuple[Announcement, ...]:
    """Every announcement of the contest or task, closed ones included,
    oldest first, read as the organiser. Needs the observer role there.
    """
    require(organiser, scope, Role.OBSERVER)
    where = await names.scope_names(ctx, scope)
    await ctx.let_go()
    threads = await ctx.forge.threads.list_threads(
        organiser.identity, place_of(scope), ThreadKind.ANNOUNCEMENT, comments=False
    )
    return tuple(announcement(thread, where, organiser=True) for thread in threads)


@action
async def contest(ctx: Context, session: Session, contest: ContestId) -> tuple[Announcement, ...]:
    """The open announcements of a contest the person sees and of each of its
    tasks released to them, oldest first within the contest's and then each
    task's in the contest's order. `NotFound` for a contest they may not see.
    """
    settings, _ = await release.seen(ctx, session, contest)
    tasks = [
        task
        for task in await published.tasks(ctx, contest, settings)
        if rules.visible(settings, task.definition, ctx.now)
    ]
    where = await names.places_named(ctx, [contest, *(task.id for task in tasks)])
    await ctx.let_go()
    found = list(await _open(ctx, contest, where[contest]))
    for task in tasks:
        found.extend(await _open(ctx, task.id, where[task.id]))
    return tuple(found)


@action
async def task(ctx: Context, session: Session, task: TaskId) -> tuple[Announcement, ...]:
    """The open announcements of a task released to the person, oldest first.
    `NotFound` for a task that is not, or in a contest they may not see.
    """
    contest = contest_id_of(task_scope(task))
    try:
        settings, _ = await release.seen(ctx, session, contest)
    except NotFound as exc:
        raise NotFound(published.NO_SUCH_TASK) from exc
    found = await published.task(ctx, task, settings)
    if found is None or not rules.visible(settings, found.definition, ctx.now):
        raise NotFound(published.NO_SUCH_TASK)
    where = await names.scope_names(ctx, task_scope(task))
    await ctx.let_go()
    return await _open(ctx, task, where)


def checked(title: str, body: str) -> tuple[str, str]:
    """The title and the text as they are kept: trimmed, with any line the
    platform reads back taken out, and each within its length. An empty or
    too long one is `InvalidMessage`, naming it.
    """
    title, body = _unmarked(title), _unmarked(body)
    if not title:
        raise InvalidMessage("A title is needed.", field="title")
    if len(title) > TITLE_MOST:
        raise InvalidMessage(f"A title is at most {TITLE_MOST} characters.", field="title")
    if not body:
        raise InvalidMessage("Some text is needed.", field="body")
    if len(body) > BODY_MOST:
        raise InvalidMessage(f"The text is at most {BODY_MOST} characters.", field="body")
    return title, body


def _unmarked(text: str) -> str:
    """The text with every marker taken out, again until none is left, so
    one hidden inside another cannot come out whole.
    """
    while True:
        bare = MARK.sub("", text)
        if bare == text:
            return bare.strip()
        text = bare


def place_of(scope: Scope) -> ThreadPlace:
    """The contest or task a scope names, as the place its threads live."""
    if scope.kind is ScopeKind.CONTEST:
        return ContestId(scope.path)
    if scope.kind is ScopeKind.TASK:
        return TaskId(scope.path)
    raise NotFound("An org has no announcements of its own.")


def announcement(thread: Thread, where: ScopeNames, *, organiser: bool) -> Announcement:
    """The thread as an announcement, its pointer back to a question read and
    taken out of its text, and named only to an organiser.
    """
    answers = ANSWERS.search(thread.body)
    return Announcement(
        where=where,
        number=thread.number,
        title=thread.title,
        body=ANSWERS.sub("", thread.body).rstrip(),
        posted_at=thread.created_at,
        closed=thread.closed,
        answers_question=answers is not None,
        answers=AnsweredQuestion(int(answers.group(1)), int(answers.group(2)))
        if answers is not None and organiser
        else None,
    )


async def _open(ctx: Context, place: ThreadPlace, where: ScopeNames) -> tuple[Announcement, ...]:
    threads = await ctx.forge.threads.list_threads(
        PLATFORM, place, ThreadKind.ANNOUNCEMENT, comments=False
    )
    return tuple(
        announcement(thread, where, organiser=False) for thread in threads if not thread.closed
    )


async def _found(ctx: Context, organiser: Organiser, scope: Scope, number: int) -> Thread:
    """The announcement numbered `number` at the place, read as the
    organiser. `NotFound` for none, or a thread of another kind.
    """
    await ctx.let_go()
    try:
        found = await ctx.forge.threads.read_thread(
            organiser.identity, ctx.forge.threads.thread_of(place_of(scope), number)
        )
    except NotFound:
        raise NotFound(NO_SUCH_ANNOUNCEMENT) from None
    if found.kind is not ThreadKind.ANNOUNCEMENT:
        raise NotFound(NO_SUCH_ANNOUNCEMENT)
    return found


async def _shown(ctx: Context, organiser: Organiser, scope: Scope, number: int) -> Announcement:
    found = await _found(ctx, organiser, scope, number)
    return announcement(found, await names.scope_names(ctx, scope), organiser=True)
