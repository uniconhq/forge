"""Every main request path runs on a pool of one connection. A request that
asked for a second connection while it held the first would wait on the
pool for a second and fail, so these tests fail on any path that nests a
checkout, which under load locks the whole platform up: sign-in, the guard
with and without a refresh, `/me`, the list of contests, a contest's home,
a task's page, an upload's slot, door and completion, a submit and the
start of its grading after the commit, reading it back, the organiser's
guard and their list of gradings, and a credential the forge refuses.
Each request is the guard's `identity.current` and then one action, the way
the backend calls them. A first upload slot holds no connection at all
while the forge makes the person's place, which takes it seconds.
"""

import hashlib
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from forge.domain.errors import SessionExpired
from forge.domain.keys import key_from_name
from forge.domain.roles import Role
from forge.domain.sessions import Session
from forge.domain.submissions import SubmittedInput
from forge.forges.fake import FakeForge
from forge.runtime.setup import Setup
from forge.services import (
    access,
    contest_home,
    gradings,
    identity,
    landing,
    sign_in,
    submissions,
    uploads,
)
from forge.settings import Settings
from forge.testing import CALLBACK_PATH, FakeClock
from tests.services.conftest import SPRING, Acme, Entered

SOURCE = b"print(1)\n"


@pytest.fixture
async def one(settings: Settings, fake: FakeForge, clock: FakeClock) -> AsyncIterator[Setup]:
    """A setup over the same database and forge whose pool holds exactly one
    connection and waits one second for it.
    """
    built = Setup.build(
        settings.model_copy(
            update={
                "database_pool_size": 1,
                "database_pool_overflow": 0,
                "database_pool_wait": timedelta(seconds=1),
            }
        ),
        callback_path=CALLBACK_PATH,
        forge=fake,
        clock=clock,
        keys=key_from_name,
    )
    try:
        yield built
    finally:
        await built.stop()


async def test_signing_in_and_reading_who_one_is(one: Setup, fake: FakeForge) -> None:
    started = sign_in.start("/", setup=one)
    query = parse_qs(urlsplit(fake.consent_redirect(started.url)).query)

    session, _ = await sign_in.complete(
        one,
        code=query["code"][0],
        state=query["state"][0],
        attempt=started.attempt,
        ip=None,
        user_agent=None,
    )

    checked = await identity.current(session.id, setup=one)
    me = await identity.whoami(one, checked)
    assert me.user.id == 7
    assert me.degraded is False


async def test_a_contestants_whole_path_to_a_graded_submission(
    one: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    clock.advance(timedelta(minutes=57))

    async def guard() -> Session:
        return await identity.current(entered.session.id, setup=one)

    listed = await contest_home.contests(one, await guard())
    assert [summary.contest for summary in listed] == [SPRING]
    assert await landing.contests(one)
    home = await contest_home.home(one, await guard(), SPRING)
    assert [entry.task for entry in home.tasks] == [entered.task]
    page = await contest_home.task(one, await guard(), entered.task)
    assert page.task == entered.task
    assert acme.fake.refreshes == 1

    slot = await uploads.slot(
        one,
        await guard(),
        entered.task,
        input="submission",
        filename="main.py",
        size=len(SOURCE),
        sha256=hashlib.sha256(SOURCE).hexdigest(),
    )
    door = await uploads.door(one, await guard(), slot.id, length=len(SOURCE))
    acme.fake.uploads.send(door.path, door.authorization, SOURCE)
    await uploads.complete(one, await guard(), entered.task, slot.id)

    made = await submissions.submit(
        one,
        await guard(),
        entered.task,
        {"submission": SubmittedInput(uploads=(slot.id,), language="python")},
        idempotency_key="one-connection",
    )
    [read] = await submissions.mine(one, await guard(), entered.task)
    assert read.number == made.number
    assert acme.fake.calls_to("start_run")


async def test_an_organiser_reads_the_gradings_of_a_task(
    one: Setup, acme: Acme, entered: Entered
) -> None:
    session = await identity.current((await _ada(one, acme)).id, setup=one)
    manager = await access.organiser_at(one, session, Role.MANAGER, "acme", "spring", "sum")

    assert await gradings.list(one, manager, entered.task) is not None


async def test_a_credential_the_forge_refuses_ends_the_session_on_one_connection(
    one: Setup, acme: Acme
) -> None:
    session = await _ada(one, acme)
    acme.fake.state.revoke_credentials(7)

    with pytest.raises(SessionExpired):
        await identity.whoami(one, await identity.current(session.id, setup=one))
    with pytest.raises(SessionExpired):
        await identity.current(session.id, setup=one)


async def _ada(one: Setup, acme: Acme) -> Session:
    started = sign_in.start("/", setup=one)
    query = parse_qs(urlsplit(acme.fake.consent_redirect(started.url)).query)
    session, _ = await sign_in.complete(
        one,
        code=query["code"][0],
        state=query["state"][0],
        attempt=started.attempt,
        ip=None,
        user_agent=None,
    )
    return session


async def test_the_pool_of_one_refuses_a_second_connection_while_one_is_held(one: Setup) -> None:
    async with one.unit_of_work() as outer:
        await outer.db.execute(text("select 1"))
        with pytest.raises(sqlalchemy.exc.TimeoutError):
            async with one.unit_of_work() as inner:
                await inner.db.execute(text("select 1"))


async def test_a_first_slot_holds_no_connection_while_the_forge_makes_the_place(
    one: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = acme.fake.workspaces.open_submission_place

    async def needing_a_connection(*args: Any, **kwargs: Any) -> None:
        async with one.unit_of_work() as other:
            await other.db.execute(text("select 1"))
        await real(*args, **kwargs)

    monkeypatch.setattr(acme.fake.workspaces, "open_submission_place", needing_a_connection)
    session = await identity.current(entered.session.id, setup=one)

    slot = await uploads.slot(
        one,
        session,
        entered.task,
        input="submission",
        filename="main.py",
        size=len(SOURCE),
        sha256=hashlib.sha256(SOURCE).hexdigest(),
    )

    assert not slot.ready
