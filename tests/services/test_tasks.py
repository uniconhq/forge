"""Making a task is a request a manager of its contest makes, done before it
answers: the place with the starter `task.yaml`, `statement.md`, an empty
`public/` and one test, `tests/main/1/`, then the roles of the contest and
the task with its protection and its publications reserved, then its
activation at the CI as the org's account, then its entry at the end of the
`tasks` list in `contest.yaml`, written as the platform: `{id}` in a draft
contest, and in a published one with `release_at` and `closes` at the
contest's end, so work in progress shows nothing. Nothing is published; a
task the list holds already is left as it is, and a `contest.yaml` that does
not read is left alone; a step that fails fails the request, removes what
the earlier steps made and leaves the name free, and so does a commit that
fails after every step, which takes the entry out of `contest.yaml` too
unless an organiser changed the file since; a removal that fails is logged
and the step's own error is raised; the list is read as the organiser.
"""

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.content import Edit
from forge.domain.definitions import parse_contest, parse_task
from forge.domain.errors import Forbidden, NotFound, Rejected, Unavailable
from forge.domain.identity import PLATFORM, AsOrgAccount, User
from forge.domain.ids import ContestId, OrgId, TaskId
from forge.domain.names import Named
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import making, names, org_accounts, publications, tasks
from forge.services.access import Organiser
from forge.testing import logged
from tests.services.conftest import (
    SPRING,
    SUM,
    Acme,
    forge_state,
    make_task,
    organiser,
    publish,
    write_contest,
)

STEPS = [
    ("content", "create_task"),
    ("content", "secure"),
    ("grading", "activate"),
    ("content", "read_file"),
    ("content", "write_file"),
]
REMOVALS = {"write_file", "deactivate", "delete_place"}

CONTEST_HEAD = (
    "name: Spring 2026\nstart: 2026-10-01T10:00:00Z\nend: 2026-10-01T15:00:00Z\n"
    "state: draft\nvisibility: signed-in\n"
)


async def test_a_contest_manager_makes_the_task_with_its_files(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring"), Role.MANAGER)
    bob = await organiser(setup, acme.fake, 8, Scope("acme", "spring"), Role.MANAGER)
    contest_before = acme.fake.state.repos[("acme", "spring.contest")].files["contest.yaml"]
    acme.fake.reset_calls()

    made = await tasks.create(setup, bob, spring, "sum", title="Sum of Two")

    assert made == Named(SUM, "sum")
    (activated,) = acme.fake.calls_to("activate")
    assert isinstance(activated.identity, AsOrgAccount)
    assert activated.identity.org == "acme"
    assert [call.operation for call in acme.fake.calls if call.identity == PLATFORM] == [
        "exists",
        "create_task",
        "secure",
        "read_file",
        "write_file",
    ]
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    assert sorted(repo.files) == [
        "public/.gitkeep",
        "statement.md",
        "task.yaml",
        "tests/main/1/answer",
        "tests/main/1/input",
    ]
    assert (repo.files["tests/main/1/input"], repo.files["tests/main/1/answer"]) == (
        b"1 2\n",
        b"3\n",
    )
    starter = parse_task(repo.files["task.yaml"])
    assert (starter.name, str(starter.workflow)) == ("Sum of Two", "unicon/classic@v2")
    assert repo.teams == {Scope("acme", "spring"), Scope("acme", "spring", "sum")}
    assert repo.reserved == {"published/"}
    assert repo.rewrites_refused is True
    assert repo.versions == {}
    assert acme.fake.state.repos[("acme", "spring.contest")].files["contest.yaml"] == (
        contest_before + b"tasks:\n  - id: sum\n"
    )
    found = await acme.fake.content.read_file(bob.identity, SUM, "statement.md")
    assert found.content.startswith(b"Write the statement")


async def test_each_new_task_goes_at_the_end_of_the_list_and_a_listed_one_is_left_alone(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await write_contest(
        acme.fake,
        CONTEST_HEAD + "tasks:\n  # Ordered by difficulty.\n  - id: product\n    worth: 50\n",
    )

    for name in ("product", "sum", "power"):
        await tasks.create(setup, acme.ada, spring, name)

    written = acme.fake.state.repos[("acme", "spring.contest")].files["contest.yaml"]
    contest = parse_contest(written)
    assert [(entry.id, entry.worth, entry.release_at) for entry in contest.tasks] == [
        ("product", 50, None),
        ("sum", None, None),
        ("power", None, None),
    ]
    assert [contest.label_of(name) for name in ("product", "sum", "power")] == ["A", "B", "C"]
    assert b"# Ordered by difficulty." in written


async def test_a_task_made_in_a_published_contest_opens_and_closes_at_its_end(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await write_contest(acme.fake, CONTEST_HEAD.replace("state: draft", "state: published"))

    await tasks.create(setup, acme.ada, spring, "sum")

    written = acme.fake.state.repos[("acme", "spring.contest")].files["contest.yaml"]
    assert written.endswith(
        b"tasks:\n  - id: sum\n    release_at: 2026-10-01T15:00:00Z\n"
        b"    closes: 2026-10-01T15:00:00Z\n"
    )
    (entry,) = parse_contest(written).tasks
    assert entry.release_at == entry.closes == parse_contest(written).end
    assert (entry.worth, entry.due) == (None, None)


async def test_a_contest_yaml_that_does_not_read_is_left_alone_and_the_task_is_made(
    setup: Setup, acme: Acme, spring: ContestId, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await write_contest(acme.fake, "name: [\n")

    await tasks.create(setup, acme.ada, spring, "sum")

    assert acme.fake.state.repos[("acme", "spring.contest")].files["contest.yaml"] == b"name: [\n"
    (skipped,) = logged(caplog, "tasks.not_listed")
    assert skipped["task"] == "acme/spring/sum"


async def test_a_task_needs_the_manager_role_at_its_contest(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring"), Role.OBSERVER)
    bob = await organiser(setup, acme.fake, 8, Scope("acme", "spring"))

    with pytest.raises(Forbidden, match="manager role at acme/spring"):
        await tasks.create(setup, bob, spring, "sum")
    assert acme.fake.calls_to("exists") == []


async def test_a_task_in_a_contest_that_is_not_there_is_refused(setup: Setup, acme: Acme) -> None:
    with pytest.raises(NotFound, match="no contest acme/winter"):
        await tasks.create(setup, acme.ada, ContestId("acme/winter"), "sum")
    assert acme.fake.calls_to("create_task") == []


async def test_a_malformed_contest_names_nothing(setup: Setup, acme: Acme) -> None:
    with pytest.raises(NotFound):
        await tasks.create(setup, acme.ada, ContestId("acme"), "sum")


async def test_a_failure_fails_the_request_and_leaves_the_name_free(
    setup: Setup, acme: Acme, spring: ContestId, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*args: Any, **kwargs: Any) -> None:
        raise Unavailable("the CI went away")

    monkeypatch.setattr(acme.fake.grading, "activate", down)

    with pytest.raises(Unavailable):
        await tasks.create(setup, acme.ada, spring, "sum")

    async with setup.unit_of_work() as ctx:
        assert await names.task_id(ctx, spring, "sum") is None
    contest = acme.fake.state.repos[("acme", "spring.contest")].files["contest.yaml"]
    assert b"id: sum" not in contest


@pytest.mark.parametrize(("area", "operation"), STEPS)
async def test_a_failure_at_any_step_removes_what_the_earlier_ones_made_and_the_next_try_works(
    setup: Setup,
    acme: Acme,
    spring: ContestId,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    area: str,
    operation: str,
) -> None:
    caplog.set_level(logging.INFO)
    target = getattr(acme.fake, area)
    original = getattr(target, operation)
    before = forge_state(acme.fake)

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the forge went away")

    monkeypatch.setattr(target, operation, broken)
    with pytest.raises(Unavailable) as failed:
        await tasks.create(setup, acme.ada, spring, "sum")

    assert failed.value.detail == making.NO_ANSWER
    assert forge_state(acme.fake) == before
    async with setup.unit_of_work() as ctx:
        assert await names.task_id(ctx, spring, "sum") is None
    assert logged(caplog, "tasks.undo_left") == []
    assert len(logged(caplog, "tasks.undone")) == 1

    monkeypatch.setattr(target, operation, original)
    made = await tasks.create(setup, acme.ada, spring, "sum")

    assert made == Named(SUM, "sum")
    assert SUM in acme.fake.state.activated


async def test_a_commit_that_fails_after_every_step_removes_the_task_and_its_entry(
    setup: Setup, acme: Acme, spring: ContestId, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = forge_state(acme.fake)

    async def refuse(self: AsyncSession) -> None:
        raise RuntimeError("the database went away at commit")

    monkeypatch.setattr(AsyncSession, "commit", refuse)
    with pytest.raises(RuntimeError, match="went away at commit"):
        await tasks.create(setup, acme.ada, spring, "sum")
    monkeypatch.undo()

    assert [call.operation for call in acme.fake.calls if call.operation in REMOVALS] == [
        "write_file",
        "write_file",
        "deactivate",
        "delete_place",
    ]
    assert forge_state(acme.fake) == before
    history = acme.fake.state.repos[("acme", "spring.contest")].history
    assert [change.message for change in history[-2:]] == [
        "Add sum to the contest's tasks",
        "Take sum out of the contest's tasks",
    ]


async def test_an_entry_an_organiser_changed_since_is_left_and_logged(
    setup: Setup,
    acme: Acme,
    spring: ContestId,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original = acme.fake.content.write_file

    async def written_then_edited(*args: Any, **kwargs: Any) -> Any:
        version = await original(*args, **kwargs)
        monkeypatch.setattr(acme.fake.content, "write_file", original)
        await write_contest(acme.fake, CONTEST_HEAD + "tasks: []\n# mine\n")
        return version

    async def refuse(self: AsyncSession) -> None:
        raise RuntimeError("the database went away at commit")

    monkeypatch.setattr(acme.fake.content, "write_file", written_then_edited)
    monkeypatch.setattr(AsyncSession, "commit", refuse)
    with pytest.raises(RuntimeError, match="went away at commit"):
        await tasks.create(setup, acme.ada, spring, "sum")
    monkeypatch.undo()

    contest = acme.fake.state.repos[("acme", "spring.contest")].files["contest.yaml"]
    assert contest.endswith(b"# mine\n")
    (left,) = logged(caplog, "tasks.undo_left")
    assert (left["kind"], left["key"], left["error"]) == ("contest_entry", SPRING, "Conflict")
    assert ("acme", "spring.sum.task") not in acme.fake.state.repos
    assert SUM not in acme.fake.state.activated


async def test_a_removal_that_fails_is_logged_and_the_rest_still_run(
    setup: Setup,
    acme: Acme,
    spring: ContestId,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise Rejected("the forge said no")

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the CI went away")

    monkeypatch.setattr(acme.fake.content, "read_file", refused)
    monkeypatch.setattr(acme.fake.grading, "deactivate", down)
    with pytest.raises(Rejected) as failed:
        await tasks.create(setup, acme.ada, spring, "sum")

    assert failed.value.detail == making.SAID[Rejected]
    (left,) = logged(caplog, "tasks.undo_left")
    assert (left["kind"], left["key"], left["task"], left["error"]) == (
        "activation",
        SUM,
        SUM,
        "Unavailable",
    )
    assert ("acme", "spring.sum.task") not in acme.fake.state.repos


async def test_a_task_made_in_full_removes_nothing(
    setup: Setup, acme: Acme, spring: ContestId, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    await tasks.create(setup, acme.ada, spring, "sum")

    assert [
        call for call in acme.fake.calls if call.operation in {"deactivate", "delete_place"}
    ] == []
    assert logged(caplog, "tasks.undone") == []
    assert SUM in acme.fake.state.activated


async def test_the_tasks_of_a_contest_are_listed_as_the_organiser(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await tasks.create(setup, acme.ada, SPRING, "product")

    listed = await tasks.list(setup, acme.ada, SPRING)

    assert listed == (Named("acme/spring/product", "product"), Named(SUM, "sum"))
    (call,) = acme.fake.calls_to("list_tasks")
    assert call.identity == acme.ada.identity


async def test_a_new_task_is_a_draft_with_nothing_wrong_and_no_publication(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    state = await tasks.state(setup, acme.ada, sum_task)

    assert (state.draft, state.errors, state.latest) == (True, (), None)
    assert state.head == acme.fake.state.repos[("acme", "spring.sum.task")].head


async def test_a_task_whose_activation_the_ci_refuses_signs_the_org_account_in_again(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    async with setup.unit_of_work() as ctx:
        lost = await org_accounts.identity(ctx, OrgId("acme"))
    acme.fake.state.revoked_ci_tokens.add(lost.ci_token)

    await tasks.create(setup, acme.ada, spring, "sum")

    assert len(acme.fake.calls_to("activate")) == 2
    assert len(acme.fake.calls_to("mint_ci_token")) == 1


STANDING = CONTEST_HEAD + (
    "tasks:\n"
    "  - id: max\n"
    "    worth: 50\n"
    "    release_at: 2026-10-01T11:00:00Z\n"
    "    closes: 2026-10-01T14:00:00Z\n"
    "  - id: sum\n"
    "    due: 2026-10-01T13:00:00Z\n"
)


async def test_the_contests_tasks_stand_in_its_order_with_drafts_and_timelines(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    max_task = await make_task(setup, acme, "max")
    await make_task(setup, acme, "extra")
    await publish(setup, acme, sum_task)
    first = await publish(setup, acme, max_task)
    head = await acme.fake.content.list_files(PLATFORM, max_task)
    broken = await publications.save(
        setup, acme.ada, max_task, {"task.yaml": Edit(b"name: [\n", head.tokens["task.yaml"])}
    )
    assert isinstance(broken, publications.Draft)
    await write_contest(acme.fake, STANDING)
    observer = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.OBSERVER)

    standing = await tasks.standing(setup, observer, SPRING)

    # The task the contest does not list is not on it.
    assert [(line.label, line.task) for line in standing] == [
        ("A", Named(max_task, "max")),
        ("B", Named(SUM, "sum")),
    ]
    on_max, on_sum = standing
    assert on_max.state.latest is not None and on_max.state.latest.number == first.number
    assert on_max.state.draft and on_max.state.errors
    assert on_max.timeline == tasks.Timeline(
        worth=50,
        release_at=datetime(2026, 10, 1, 11, tzinfo=UTC),
        due=None,
        late_per_day=None,
        closes=datetime(2026, 10, 1, 14, tzinfo=UTC),
    )
    assert on_sum.state.latest is not None
    assert (on_sum.state.draft, on_sum.state.errors) == (False, ())
    assert on_sum.timeline == tasks.Timeline(
        worth=100,
        release_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
        due=datetime(2026, 10, 1, 13, tzinfo=UTC),
        late_per_day=1,
        closes=datetime(2026, 10, 1, 15, tzinfo=UTC),
    )
    with pytest.raises(Forbidden):
        await tasks.standing(setup, _held_at_task(acme), SPRING)


async def test_settings_that_do_not_read_are_told_with_their_errors(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    observer = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.OBSERVER)
    await write_contest(acme.fake, "name: Spring\nstart: soon\n")

    with pytest.raises(InvalidDefinition) as invalid:
        await tasks.standing(setup, observer, SPRING)

    assert invalid.value.detail.startswith("contest.yaml ")
    assert {problem["path"] for problem in invalid.value.errors} >= {"start"}
    with pytest.raises(NotFound):
        await tasks.standing(setup, observer, ContestId("acme/autumn"))


def _held_at_task(acme: Acme) -> Organiser:
    scope = Scope("acme", "spring", "sum")
    return Organiser(
        user=User(id=9, username="eve"),
        grants=(RoleGrant(scope, Role.OBSERVER),),
        scope=scope,
        role=Role.OBSERVER,
        identity=acme.ada.identity,
    )
