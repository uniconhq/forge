"""An entrant's workspace at the forge, made when it is first needed.
Approval makes nothing at the forge; publishing a task makes nothing for
anyone; an entrant's first submit to a task makes its place to submit it,
which its members write. A removed entrant keeps what was made without
access, and one removed before submitting has nothing made.
"""

import uuid

import pytest

from forge.domain.errors import NotApproved
from forge.domain.ids import TaskId
from forge.domain.names import EntrantOwner
from forge.domain.roles import Role, Scope
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import entrants, submissions
from forge.services.access import Organiser
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    make_task,
    organiser,
    publish,
    signed_in,
    upload,
    write_contest,
)

SOURCE = b"print(sum(map(int, input().split())))\n"


@pytest.fixture
async def manager(setup: Setup, acme: Acme, spring: str) -> Organiser:
    await write_contest(acme.fake, RUNNING.format(visibility="everyone"))
    acme.fake.add_user(20, "cyd")
    return await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)


@pytest.fixture
async def two_tasks(setup: Setup, acme: Acme, sum_task: TaskId, manager: Organiser) -> list[TaskId]:
    """sum and diff, both published."""
    diff = await make_task(setup, acme, "diff")
    await write_contest(acme.fake, RUNNING.format(visibility="everyone") + "  - id: diff\n")
    await publish(setup, acme, sum_task)
    await publish(setup, acme, diff)
    acme.fake.reset_calls()
    return [sum_task, diff]


async def _approve(setup: Setup, acme: Acme, manager: Organiser, user_id: int) -> uuid.UUID:
    session = await signed_in(setup, acme.fake, user_id)
    entrant = await entrants.register(setup, session, SPRING)
    await entrants.approve(setup, manager, SPRING, entrant.id)
    return entrant.id


def _place(entrant: uuid.UUID, task: str) -> str:
    return f"spring.{task}.{EntrantOwner(entrant).segment}.sub"


def _made_for(acme: Acme, entrant: uuid.UUID) -> list[str]:
    return [repo for (_, repo) in acme.fake.state.repos if EntrantOwner(entrant).segment in repo]


async def _submit(setup: Setup, acme: Acme, user_id: int, task: TaskId) -> None:
    session = await signed_in(setup, acme.fake, user_id)
    made = await upload(setup, acme.fake, session, task, SOURCE)
    await submissions.submit(
        setup,
        session,
        task,
        {
            "submission": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key="key-0001-aaaa",
    )


def _writers(acme: Acme, repo: str) -> set[int]:
    return acme.fake.state.repos[("acme", repo)].writers


async def test_approval_and_publication_make_nothing_at_the_forge(
    setup: Setup, acme: Acme, manager: Organiser, two_tasks: list[TaskId]
) -> None:
    bob = await _approve(setup, acme, manager, 8)
    late = await make_task(setup, acme, "late")
    await publish(setup, acme, late)

    assert _made_for(acme, bob) == []
    assert acme.fake.calls_to("open_workspace") == []
    assert acme.fake.calls_to("open_submission_place") == []


async def test_a_first_submit_makes_the_place_to_submit_that_task_alone(
    setup: Setup, acme: Acme, manager: Organiser, two_tasks: list[TaskId]
) -> None:
    sum_task, _ = two_tasks
    bob = await _approve(setup, acme, manager, 8)

    await _submit(setup, acme, 8, sum_task)

    assert _writers(acme, _place(bob, "sum")) == {8}
    assert ("acme", _place(bob, "diff")) not in acme.fake.state.repos


async def test_a_removed_entrant_keeps_what_was_made_without_access(
    setup: Setup, acme: Acme, manager: Organiser, two_tasks: list[TaskId]
) -> None:
    sum_task, diff = two_tasks
    bob = await _approve(setup, acme, manager, 8)
    await _submit(setup, acme, 8, sum_task)

    await entrants.remove(setup, manager, SPRING, bob)

    assert 8 not in _writers(acme, _place(bob, "sum"))
    assert acme.fake.state.repos[("acme", _place(bob, "sum"))].versions
    session = await signed_in(setup, acme.fake, 8)
    with pytest.raises(NotApproved):
        await submissions.submit(setup, session, diff, {}, idempotency_key="key-0002-bbbb")
    assert ("acme", _place(bob, "diff")) not in acme.fake.state.repos


async def test_an_entrant_removed_before_submitting_has_nothing_to_close(
    setup: Setup, acme: Acme, manager: Organiser, two_tasks: list[TaskId]
) -> None:
    bob = await _approve(setup, acme, manager, 8)

    removed = await entrants.remove(setup, manager, SPRING, bob)

    assert removed.status.value == "removed"
    assert _made_for(acme, bob) == []
