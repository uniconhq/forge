"""Making a contest is a request a manager of the org makes and the poller
runs in two recorded steps: the place with a starter `contest.yaml` that is
valid as written, titled by its name when no title is given, and its roles
and protection. A failure at either step is
named and a rerun does not repeat the one before; the status is for anyone
observing the org; the list is read as the organiser; and the nightly pass
puts back a contest's detached roles.
"""

import logging
from typing import Any

import pytest

from forge.domain.definitions import parse_contest
from forge.domain.errors import Conflict, Forbidden, InvalidName, Unavailable
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import ContestId
from forge.domain.roles import Role, Scope
from forge.runtime.setup import Setup
from forge.services import contests, provisioning
from forge.testing import logged, tick
from tests.services.conftest import ACME, SPRING, Acme, organiser


async def test_a_manager_asks_and_the_poller_makes_the_contest_with_a_valid_starter(
    setup: Setup, acme: Acme
) -> None:
    record = await contests.create(setup, acme.ada, ACME, "spring", title="Spring 2026")

    assert (record.kind, record.target_id, record.status) == ("contest", "acme/spring", "pending")
    assert acme.fake.calls == []
    await tick(setup, "provisioning")

    done = await contests.status(setup, acme.ada, SPRING)
    assert done is not None
    assert (done.status, done.last_step, done.attempts) == ("ready", "roles", 1)
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
    await tick(setup, "provisioning")

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
        assert await provisioning.record_of(ctx, "contest", "acme/spring") is None


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
    with pytest.raises(Conflict, match="being made"):
        await contests.create(setup, acme.ada, ACME, "spring")
    await tick(setup, "provisioning")
    with pytest.raises(Conflict, match="already exists"):
        await contests.create(setup, acme.ada, ACME, "spring")


async def test_a_contest_made_outside_the_platform_is_refused_not_adopted(
    setup: Setup, acme: Acme
) -> None:
    await acme.fake.content.create_contest(ACME, "spring", {"contest.yaml": b"theirs"})
    await contests.create(setup, acme.ada, ACME, "spring")
    await tick(setup, "provisioning")

    failed = await contests.status(setup, acme.ada, SPRING)
    assert failed is not None
    assert (failed.status, failed.last_step, failed.failed_step) == ("failed", None, "repo")
    assert failed.steps == ("repo", "roles")
    assert failed.error == "the forge already holds something by this name"


@pytest.mark.parametrize(("step", "operation"), [("repo", "create_contest"), ("roles", "secure")])
async def test_a_failure_is_named_and_a_rerun_starts_after_the_last_step_done(
    setup: Setup, acme: Acme, monkeypatch: pytest.MonkeyPatch, step: str, operation: str
) -> None:
    original = getattr(acme.fake.content, operation)

    async def broken(*args: Any, **kwargs: Any) -> Any:
        await original(*args, **kwargs)
        raise Unavailable("the forge went away after doing it")

    monkeypatch.setattr(acme.fake.content, operation, broken)
    await contests.create(setup, acme.ada, ACME, "spring")
    await tick(setup, "provisioning")

    failed = await contests.status(setup, acme.ada, SPRING)
    assert failed is not None
    assert failed.status == "failed"
    assert (failed.failed_step, failed.error) == (step, "the forge or the CI did not answer")

    monkeypatch.setattr(acme.fake.content, operation, original)
    acme.fake.reset_calls()
    await tick(setup, "provisioning")

    done = await contests.status(setup, acme.ada, SPRING)
    assert done is not None
    assert (done.status, done.attempts) == ("ready", 2)
    later = [call.operation for call in acme.fake.calls]
    assert later == (["create_contest", "secure"] if step == "repo" else ["secure"])


async def test_the_status_is_for_anyone_observing_the_org(setup: Setup, acme: Acme) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "autumn"), Role.ADMIN)
    bob = await organiser(setup, acme.fake, 8, Scope("acme", "autumn"))
    assert await contests.status(setup, acme.ada, SPRING) is None
    await contests.create(setup, acme.ada, ACME, "spring")

    with pytest.raises(Forbidden, match="observer role at acme"):
        await contests.status(setup, bob, SPRING)
    await acme.fake.orgs.grant_role(8, Scope("acme"), Role.OBSERVER)
    observer = await organiser(setup, acme.fake, 8, Scope("acme"))
    pending = await contests.status(setup, observer, SPRING)
    assert pending is not None
    assert pending.status == "pending"


async def test_the_contests_of_an_org_are_listed_as_the_organiser(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await contests.create(setup, acme.ada, ACME, "autumn")
    await tick(setup, "provisioning")

    listed = await contests.list(setup, acme.ada, ACME)

    assert listed == (ContestId("acme/autumn"), SPRING)
    (call,) = acme.fake.calls_to("list_contests")
    assert call.identity == acme.ada.identity


async def test_the_nightly_pass_reattaches_a_detached_contest_role(
    setup: Setup, acme: Acme, spring: ContestId, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    repo = acme.fake.state.repos[("acme", "spring.contest")]
    repo.teams.clear()

    await tick(setup, "drift.nightly")

    assert repo.teams == {Scope("acme", "spring")}
    (restored,) = logged(caplog, "drift.content_restored")
    assert (restored["place"], restored["put_back"]) == ("acme/spring", 3)
    (summary,) = logged(caplog, "drift.nightly")
    assert summary["content_restored"] == 3
