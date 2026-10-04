"""Making a contest is a request a manager of the org makes, done before it
answers: the place with a starter `contest.yaml` that is valid as written,
titled by its name when no title is given, and its roles and protection. A
failure at either step fails the request, removes the contest if the first
step made it, and leaves the name free for the next try, and so does a
commit that fails after both; a removal that fails is logged and the step's
own error is raised; the list is read as the organiser.
"""

import logging
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.definitions import parse_contest
from forge.domain.errors import Conflict, Forbidden, InvalidName, Rejected, Unavailable
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import ContestId
from forge.domain.keys import random_key
from forge.domain.names import Named
from forge.domain.roles import Role, Scope
from forge.runtime.setup import Setup
from forge.services import contests, making, names
from forge.testing import logged
from tests.services.conftest import ACME, SPRING, Acme, forge_state, organiser


async def test_a_manager_makes_the_contest_with_a_valid_starter(setup: Setup, acme: Acme) -> None:
    made = await contests.create(setup, acme.ada, ACME, "spring", title="Spring 2026")

    assert made == Named(SPRING, "spring")
    assert [call.operation for call in acme.fake.calls] == ["create_contest", "secure"]
    assert {call.identity for call in acme.fake.calls} == {PLATFORM}
    starter = await acme.fake.content.read_file(PLATFORM, SPRING, "contest.yaml")
    settings = parse_contest(starter.content)
    assert (settings.name, settings.state.value) == ("Spring 2026", "draft")
    repo = acme.fake.state.repos[("acme", "spring.contest")]
    assert repo.teams == {Scope("acme", "spring")}
    assert repo.rewrites_refused is True
    assert repo.reserved == set()


@pytest.mark.parametrize("title", [None, "", "   "])
async def test_a_contest_asked_for_without_a_title_is_titled_by_its_name(
    setup: Setup, acme: Acme, title: str | None
) -> None:
    await contests.create(setup, acme.ada, ACME, "spring", title=title)

    starter = await acme.fake.content.read_file(PLATFORM, SPRING, "contest.yaml")
    assert parse_contest(starter.content).name == "spring"


async def test_a_contest_manager_reads_and_writes_it_and_a_stranger_does_not(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring"), Role.MANAGER)
    acme.fake.add_user(30, "eve")
    bob = await organiser(setup, acme.fake, 8, Scope("acme", "spring"), Role.MANAGER)
    eve_credential = acme.fake.mint(30)

    found = await acme.fake.content.read_file(bob.identity, spring, "contest.yaml")
    await acme.fake.content.write_file(
        bob.identity, spring, "notes.md", b"x", message="Notes", expected=None
    )

    assert found.content.startswith(b"# The contest's settings")
    with pytest.raises(Forbidden):
        await acme.fake.content.read_file(AsUser(30, eve_credential), spring, "contest.yaml")


async def test_a_contest_needs_the_manager_role_at_the_org(setup: Setup, acme: Acme) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme"), Role.OBSERVER)
    bob = await organiser(setup, acme.fake, 8, Scope("acme"))

    with pytest.raises(Forbidden, match="manager role at acme"):
        await contests.create(setup, bob, ACME, "spring")
    async with setup.unit_of_work() as ctx:
        assert await names.contest_id(ctx, ACME, "spring") is None


@pytest.mark.parametrize("name", ["Spring", "a" * 25, "sp.ring", ""])
async def test_a_name_that_breaks_the_rules_is_refused_before_anything_is_written(
    setup: Setup, acme: Acme, name: str
) -> None:
    with pytest.raises(InvalidName):
        await contests.create(setup, acme.ada, ACME, name)
    assert acme.fake.calls == []


async def test_a_second_request_for_the_same_contest_is_a_conflict(
    setup: Setup, acme: Acme
) -> None:
    await contests.create(setup, acme.ada, ACME, "spring")
    with pytest.raises(Conflict, match="'spring' is taken"):
        await contests.create(setup, acme.ada, ACME, "spring")
    with pytest.raises(Conflict, match="'spring' is taken"):
        await contests.create(setup, acme.ada, ACME, "spring")


async def test_a_contest_made_outside_the_platform_is_refused_not_adopted(
    setup: Setup, acme: Acme
) -> None:
    await acme.fake.content.create_contest(ACME, "spring", {"contest.yaml": b"theirs"})

    with pytest.raises(Conflict):
        await contests.create(setup, acme.ada, ACME, "spring")

    async with setup.unit_of_work() as ctx:
        assert await names.contest_id(ctx, ACME, "spring") is None
    assert acme.fake.state.repos[("acme", "spring.contest")].files == {"contest.yaml": b"theirs"}
    assert acme.fake.calls_to("delete_place") == []


@pytest.mark.parametrize("operation", ["create_contest", "secure"])
async def test_a_failure_fails_the_request_and_the_next_try_makes_the_contest(
    setup: Setup, acme: Acme, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    original = getattr(acme.fake.content, operation)

    async def broken(*args: Any, **kwargs: Any) -> Any:
        await original(*args, **kwargs)
        raise Unavailable("the forge went away after doing it")

    monkeypatch.setattr(acme.fake.content, operation, broken)
    with pytest.raises(Unavailable):
        await contests.create(setup, acme.ada, ACME, "spring")
    async with setup.unit_of_work() as ctx:
        assert await names.contest_id(ctx, ACME, "spring") is None

    monkeypatch.setattr(acme.fake.content, operation, original)
    monkeypatch.setattr(setup, "_keys", random_key)
    made = await contests.create(setup, acme.ada, ACME, "spring")

    assert made.name == "spring"
    assert made.id != SPRING
    assert await contests.list(setup, acme.ada, ACME) == (made,)


@pytest.mark.parametrize("operation", ["create_contest", "secure"])
async def test_a_failure_at_either_step_removes_what_the_first_made_and_the_next_try_works(
    setup: Setup,
    acme: Acme,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    caplog.set_level(logging.INFO)
    original = getattr(acme.fake.content, operation)
    before = forge_state(acme.fake)

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the forge went away")

    monkeypatch.setattr(acme.fake.content, operation, broken)
    with pytest.raises(Unavailable) as failed:
        await contests.create(setup, acme.ada, ACME, "spring")

    assert failed.value.detail == making.NO_ANSWER
    assert forge_state(acme.fake) == before
    removed = acme.fake.calls_to("delete_place")
    # A failed create may have made the place without its starter files, so
    # it is removed too, which changes nothing when there is none.
    assert [call.arguments for call in removed] == [{"place": SPRING}]
    assert len(logged(caplog, "contests.undone")) == len(removed)

    monkeypatch.setattr(acme.fake.content, operation, original)
    made = await contests.create(setup, acme.ada, ACME, "spring")

    assert made == Named(SPRING, "spring")


async def test_a_contest_that_cannot_be_removed_is_logged_and_the_steps_error_is_raised(
    setup: Setup,
    acme: Acme,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise Rejected("the forge said no")

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the forge went away")

    monkeypatch.setattr(acme.fake.content, "secure", refused)
    monkeypatch.setattr(acme.fake.content, "delete_place", down)
    with pytest.raises(Rejected) as failed:
        await contests.create(setup, acme.ada, ACME, "spring")

    assert failed.value.detail == making.SAID[Rejected]
    (left,) = logged(caplog, "contests.undo_left")
    assert (left["kind"], left["key"], left["contest"]) == ("contest", SPRING, SPRING)
    async with setup.unit_of_work() as ctx:
        assert await names.contest_id(ctx, ACME, "spring") is None


async def test_a_commit_that_fails_after_both_steps_removes_the_contest(
    setup: Setup, acme: Acme, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = forge_state(acme.fake)

    async def refuse(self: AsyncSession) -> None:
        raise RuntimeError("the database went away at commit")

    monkeypatch.setattr(AsyncSession, "commit", refuse)
    with pytest.raises(RuntimeError, match="went away at commit"):
        await contests.create(setup, acme.ada, ACME, "spring")
    monkeypatch.undo()

    assert acme.fake.calls_to("secure")
    assert forge_state(acme.fake) == before


async def test_a_contest_made_in_full_removes_nothing(
    setup: Setup, acme: Acme, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    await contests.create(setup, acme.ada, ACME, "spring")

    assert acme.fake.calls_to("delete_place") == []
    assert logged(caplog, "contests.undone") == []


async def test_the_contests_of_an_org_are_listed_as_the_organiser(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await contests.create(setup, acme.ada, ACME, "autumn")

    listed = await contests.list(setup, acme.ada, ACME)

    assert listed == (Named("acme/autumn", "autumn"), Named(SPRING, "spring"))
    (call,) = acme.fake.calls_to("list_contests")
    assert call.identity == acme.ada.identity
