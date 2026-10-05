"""Making places ahead: approving someone makes their place at every task the
contest has published, a task's first publication makes it for everyone
approved, both once the request has committed and in turns of their own,
none for a contest that is over, and someone removed while their place is
made has the access taken back. A forge that fails stops the rest, which the
contestant's first upload makes.
"""

import asyncio
import hashlib
from datetime import timedelta
from typing import Any

import pytest

from forge.domain.clock import FakeClock
from forge.domain.errors import Unavailable
from forge.domain.ids import TaskId
from forge.domain.names import UserOwner
from forge.domain.roles import Role, Scope
from forge.forges.fake import FakeForge
from forge.forges.ids import parse_task, parse_workspace
from forge.runtime.setup import Setup
from forge.services import contestants, places, uploads
from forge.settings import Settings
from forge.testing import APP_URL, FORGE_URL, logged
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    make_task,
    organiser,
    publish,
    signed_in,
    write_contest,
)

SOURCE = b"print(1)\n"


@pytest.fixture
def settings(migrated_database_url: str) -> Settings:
    return Settings.for_tests(
        database_url=migrated_database_url,
        public_url=APP_URL,
        forge_public_url=FORGE_URL,
        places_ahead=True,
    )


async def _approve(setup: Setup, acme: Acme, user_id: int) -> None:
    person = await signed_in(setup, acme.fake, user_id)
    await contestants.register(setup, person, SPRING)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await contestants.approve(setup, manager, SPRING, user_id)


def _writers(fake: FakeForge, task: TaskId, user_id: int) -> set[int] | None:
    """Who may write the user's place at the task, or none when it is not made."""
    ref = parse_workspace(fake.workspaces.workspace_of(SPRING, UserOwner(user_id)))
    repo = fake.state.repos.get((ref.org, ref.submission_repo(parse_task(task).task)))
    return None if repo is None else set(repo.writers)


def _made(fake: FakeForge) -> list[str]:
    return [
        call.arguments["task"]
        for call in fake.state.calls
        if call.operation == "open_submission_place"
    ]


async def test_approving_someone_makes_their_place_at_every_published_task(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await write_contest(acme.fake, RUNNING.format(visibility="public"))
    await publish(setup, acme, sum_task)
    await setup.settle()
    unsaved = await make_task(setup, acme, "product")

    await _approve(setup, acme, 8)
    await setup.settle()

    assert _writers(acme.fake, sum_task, 8) == {8}
    assert _writers(acme.fake, unsaved, 8) is None


async def test_a_tasks_first_publication_makes_its_place_for_everyone_approved(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await write_contest(acme.fake, RUNNING.format(visibility="public"))
    acme.fake.add_user(20, "cyd")
    await _approve(setup, acme, 8)
    await _approve(setup, acme, 20)
    await setup.settle()
    assert _made(acme.fake) == []

    await publish(setup, acme, sum_task)
    await setup.settle()
    await publish(setup, acme, sum_task, b"# again\n")
    await setup.settle()

    assert _writers(acme.fake, sum_task, 8) == {8}
    assert _writers(acme.fake, sum_task, 20) == {20}
    assert _made(acme.fake) == [sum_task, sum_task]


async def test_a_contest_that_is_over_gets_no_places_ahead(
    setup: Setup, acme: Acme, sum_task: TaskId, clock: FakeClock
) -> None:
    await write_contest(acme.fake, RUNNING.format(visibility="public"))
    await publish(setup, acme, sum_task)
    await setup.settle()
    person = await signed_in(setup, acme.fake, 8)
    await contestants.register(setup, person, SPRING)
    clock.advance(timedelta(hours=4))
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)

    await contestants.approve(setup, manager, SPRING, 8)
    await setup.settle()

    assert _made(acme.fake) == []


async def test_someone_removed_while_their_place_is_made_ahead_has_the_access_taken_back(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    await write_contest(acme.fake, RUNNING.format(visibility="public"))
    await publish(setup, acme, sum_task)
    await setup.settle()
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    real = acme.fake.workspaces.open_submission_place

    async def made_then_removed(*args: Any, **kwargs: Any) -> None:
        await real(*args, **kwargs)
        await contestants.remove(setup, manager, SPRING, 8)

    monkeypatch.setattr(acme.fake.workspaces, "open_submission_place", made_then_removed)

    await _approve(setup, acme, 8)
    await setup.settle()

    assert _writers(acme.fake, sum_task, 8) == set()


async def test_a_forge_that_fails_stops_the_rest_and_the_first_upload_makes_the_place(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await write_contest(acme.fake, RUNNING.format(visibility="public"))
    await publish(setup, acme, sum_task)
    await setup.settle()
    real = acme.fake.workspaces.open_submission_place

    async def down(*args: Any, **kwargs: Any) -> None:
        raise Unavailable("the forge is down")

    monkeypatch.setattr(acme.fake.workspaces, "open_submission_place", down)
    await _approve(setup, acme, 8)
    await setup.settle()
    assert _writers(acme.fake, sum_task, 8) is None
    assert logged(caplog, "places.ahead_stopped")

    monkeypatch.setattr(acme.fake.workspaces, "open_submission_place", real)
    bob = await signed_in(setup, acme.fake, 8)
    await uploads.slot(
        setup,
        bob,
        sum_task,
        input="submission",
        filename="main.py",
        size=len(SOURCE),
        sha256=hashlib.sha256(SOURCE).hexdigest(),
    )

    assert _writers(acme.fake, sum_task, 8) == {8}


async def test_places_ahead_take_a_few_turns_of_their_own() -> None:
    turns = places.Turns(places.AHEAD_AT_ONCE)
    making = most = 0

    async def make() -> None:
        nonlocal making, most
        async with turns():
            making += 1
            most = max(most, making)
            await asyncio.sleep(0.01)
            making -= 1

    await asyncio.gather(*(make() for _ in range(10)))

    assert most == places.AHEAD_AT_ONCE
