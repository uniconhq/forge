"""Making a workflow is open to anyone signed in, under their own name or
under an org where they hold the manager role or above. The platform makes
the place and the person writes its first commit, a `workflow.yaml` named
`<owner>/<name>` that parses; the workflow is private. An observer of the
org, a person with no role there, a role held only at one of its contests,
an org that is not there and another person's name are all refused the same
way, before the forge is asked; a name that breaks the rules is refused, a
name the owner has already is `Conflict`, and a forge failure is told in
fixed words.
"""

import logging
from dataclasses import replace

import pytest

from forge.domain.errors import Conflict, Forbidden, InvalidName, Unavailable
from forge.domain.identity import AsUser
from forge.domain.ids import ContestId, WorkflowId
from forge.domain.roles import Role, Scope
from forge.domain.workflow_definition import parse_workflow
from forge.runtime.setup import Setup
from forge.services import workflows
from forge.services.workflows import NewWorkflow
from forge.testing import CLASSIC, logged
from tests.services.conftest import Acme, signed_in


async def test_a_person_makes_a_workflow_under_their_own_name(setup: Setup, acme: Acme) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    made = await workflows.create(setup, bob, "bob", "tuned")

    assert made == NewWorkflow(WorkflowId("bob/tuned"), "bob", "tuned")
    repo = acme.fake.state.repos[("bob", "tuned.workflow")]
    assert repo.private is True
    assert repo.readers == set()
    assert [change.author_id for change in repo.history] == [8]
    assert parse_workflow(repo.files["workflow.yaml"]).name == "bob/tuned"
    (call,) = acme.fake.calls_to("create_workflow")
    assert isinstance(call.identity, AsUser)
    assert call.identity.user_id == 8


async def test_the_starter_workflow_has_the_classic_steps_under_its_own_name(
    setup: Setup, acme: Acme
) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    await workflows.create(setup, bob, "bob", "tuned")

    repo = acme.fake.state.repos[("bob", "tuned.workflow")]
    starter = parse_workflow(repo.files["workflow.yaml"])
    classic = parse_workflow(CLASSIC)
    assert starter.ref.version == "v1"
    assert starter.copied_from is None
    assert (starter.inputs, starter.steps, starter.outputs) == (
        classic.inputs,
        classic.steps,
        classic.outputs,
    )


async def test_ones_own_name_is_matched_whatever_its_case(setup: Setup, acme: Acme) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    made = await workflows.create(setup, bob, "Bob", "tuned")

    assert made.owner == "bob"
    assert ("bob", "tuned.workflow") in acme.fake.state.repos


async def test_a_person_who_renamed_themselves_makes_it_under_the_name_they_have_now(
    setup: Setup, acme: Acme
) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    acme.fake.state.users[8] = replace(acme.fake.state.users[8], username="robert")
    acme.fake.add_user(31, "bob")

    with pytest.raises(Forbidden):
        await workflows.create(setup, bob, "bob", "tuned")
    made = await workflows.create(setup, bob, "robert", "tuned")

    assert made.owner == "robert"
    assert ("robert", "tuned.workflow") in acme.fake.state.repos
    assert ("bob", "tuned.workflow") not in acme.fake.state.repos


async def test_a_username_that_cannot_name_a_workflow_is_refused(setup: Setup, acme: Acme) -> None:
    acme.fake.add_user(30, "ken.l")
    ken = await signed_in(setup, acme.fake, 30)

    with pytest.raises(InvalidName, match="cannot name a workflow"):
        await workflows.create(setup, ken, "ken.l", "tuned")
    assert acme.fake.calls_to("create_workflow") == []


@pytest.mark.parametrize("role", [Role.MANAGER, Role.ADMIN])
async def test_a_manager_or_admin_of_the_org_makes_one_in_the_org(
    setup: Setup, acme: Acme, role: Role
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme"), role)
    bob = await signed_in(setup, acme.fake, 8)

    made = await workflows.create(setup, bob, "acme", "tuned")

    assert made == NewWorkflow(WorkflowId("acme/tuned"), "acme", "tuned")
    repo = acme.fake.state.repos[("acme", "tuned.workflow")]
    assert repo.private is True
    assert [change.author_id for change in repo.history] == [8]
    assert parse_workflow(repo.files["workflow.yaml"]).name == "acme/tuned"


async def test_the_orgs_first_admin_makes_one_in_the_org(setup: Setup, acme: Acme) -> None:
    ada = await signed_in(setup, acme.fake, 7)

    made = await workflows.create(setup, ada, "acme", "tuned")

    assert made.id == WorkflowId("acme/tuned")


async def test_an_observer_of_the_org_is_refused(setup: Setup, acme: Acme) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme"), Role.OBSERVER)
    bob = await signed_in(setup, acme.fake, 8)

    with pytest.raises(Forbidden, match="manager role at acme"):
        await workflows.create(setup, bob, "acme", "tuned")
    assert acme.fake.calls_to("create_workflow") == []


async def test_a_manager_of_one_contest_is_refused_at_the_org(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring"), Role.MANAGER)
    bob = await signed_in(setup, acme.fake, 8)

    with pytest.raises(Forbidden, match="manager role at acme"):
        await workflows.create(setup, bob, "acme", "tuned")
    assert acme.fake.calls_to("create_workflow") == []


@pytest.mark.parametrize("owner", ["acme", "nowhere", "ada"])
async def test_an_org_without_a_role_one_not_there_and_another_person_are_refused_alike(
    setup: Setup, acme: Acme, owner: str
) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    with pytest.raises(Forbidden) as refused:
        await workflows.create(setup, bob, owner, "tuned")

    assert refused.value.detail == f"This needs the manager role at {owner}."
    assert acme.fake.calls_to("create_workflow") == []


@pytest.mark.parametrize("name", ["Tuned", "tuned!", "-tuned", "", "t" * 41])
async def test_a_name_that_breaks_the_rules_is_refused(setup: Setup, acme: Acme, name: str) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    with pytest.raises(InvalidName):
        await workflows.create(setup, bob, "bob", name)
    assert acme.fake.calls_to("create_workflow") == []


async def test_a_name_the_owner_has_already_is_a_conflict(setup: Setup, acme: Acme) -> None:
    ada = await signed_in(setup, acme.fake, 7)
    await workflows.create(setup, ada, "acme", "tuned")

    with pytest.raises(Conflict, match="There is a workflow acme/tuned already"):
        await workflows.create(setup, ada, "acme", "tuned")


async def test_the_same_name_under_another_owner_is_another_workflow(
    setup: Setup, acme: Acme
) -> None:
    ada = await signed_in(setup, acme.fake, 7)
    await workflows.create(setup, ada, "acme", "tuned")

    made = await workflows.create(setup, ada, "ada", "tuned")

    assert made.id == WorkflowId("ada/tuned")


async def test_a_forge_failure_is_told_in_fixed_words_and_logged(
    setup: Setup, acme: Acme, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def gone(*args: object, **kwargs: object) -> WorkflowId:
        raise Unavailable("forgejo at 10.0.0.3 went away")

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(acme.fake.workflows, "create_workflow", gone)
    bob = await signed_in(setup, acme.fake, 8)

    with pytest.raises(Unavailable) as failed:
        await workflows.create(setup, bob, "bob", "tuned")

    assert "10.0.0.3" not in failed.value.detail
    (step,) = logged(caplog, "workflows.step_failed")
    assert step["owner"] == "bob"
    assert logged(caplog, "workflows.created") == []


async def test_a_workflow_made_is_logged(
    setup: Setup, acme: Acme, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    bob = await signed_in(setup, acme.fake, 8)

    await workflows.create(setup, bob, "bob", "tuned")

    (made,) = logged(caplog, "workflows.created")
    assert made["workflow"] == "bob/tuned"
    assert made["user_id"] == 8
