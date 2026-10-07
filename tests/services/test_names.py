"""Orgs, contests and tasks are filed under keys, and their names are labels.
Most tests file things under their names, so their ids read as names; these
make random keys, as a deployment does, so a name and the key it is filed
under can be told apart. A name freed and taken again is a new thing, an
address resolves to keys or names what is missing, and a workflow name that
has come to mean another workflow is refused at the save.
"""

import pytest
from sqlalchemy import update

from forge.db.tables import Name
from forge.domain.content import Edit
from forge.domain.errors import Forbidden, NotFound
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import OrgId, TaskId
from forge.domain.names import Named, ScopeNames
from forge.domain.roles import Role, Scope, contest_id_of, task_id_of
from forge.domain.workflow_definition import parse_workflow_ref
from forge.domain.workflows import Visibility
from forge.forges.fake import FakeForge
from forge.runtime.setup import Setup
from forge.services import contests, identity, names, orgs, publications, roles, tasks
from forge.services.publications import Draft, Published
from forge.testing import CLASSIC
from tests.services.conftest import Acme, organiser, signed_in

KEY_LENGTH = 26


async def _org(setup: Setup, fake: FakeForge) -> Scope:
    await orgs.create_by_operator(setup, "acme", description="Acme", admin_username="ada")
    return await names.scope_at(setup, "acme")


async def _contest_and_task(setup: Setup, fake: FakeForge) -> tuple[Scope, Scope]:
    org = await _org(setup, fake)
    ada = await organiser(setup, fake, 7, org, Role.ADMIN)
    await contests.create(setup, ada, OrgId(org.org), "spring")
    contest = await names.scope_at(setup, "acme", "spring")
    await tasks.create(setup, ada, contest_id_of(contest), "sum")
    return contest, await names.scope_at(setup, "acme", "spring", "sum")


async def test_an_org_a_contest_and_a_task_are_filed_under_keys_and_named_by_labels(
    setup_with_random_keys: Setup, fake: FakeForge
) -> None:
    setup = setup_with_random_keys
    contest, task = await _contest_and_task(setup, fake)

    assert task.label == "acme/spring/sum"
    assert all(len(part or "") == KEY_LENGTH for part in (task.org, task.contest, task.task))
    assert "acme" not in fake.state.orgs
    assert task.org in fake.state.orgs
    assert (task.org, f"{task.contest}.{task.task}.task") in fake.state.repos
    ada = await organiser(setup, fake, 7, contest, Role.OBSERVER)
    assert await contests.list(setup, ada, OrgId(task.org)) == (
        Named(contest_id_of(contest), "spring"),
    )
    assert await tasks.list(setup, ada, contest_id_of(contest)) == (Named(task_id_of(task), "sum"),)
    listed = fake.state.repos[(task.org, f"{task.contest}.contest")].files["contest.yaml"]
    assert b"id: sum" in listed


async def test_the_names_of_a_role_and_of_where_it_is_held_are_shown(
    setup_with_random_keys: Setup, fake: FakeForge
) -> None:
    setup = setup_with_random_keys
    contest, _ = await _contest_and_task(setup, fake)
    ada = await organiser(setup, fake, 7, contest, Role.OBSERVER)

    (held,) = await roles.holders(setup, ada, contest)
    me = await identity.whoami(setup, await signed_in(setup, fake, 7))

    assert (held.at, held.at_names) == (Scope(contest.org), ScopeNames("acme"))
    assert [(role.names, role.role) for role in me.roles] == [(ScopeNames("acme"), Role.ADMIN)]


async def test_a_name_freed_and_taken_again_names_a_new_contest(
    setup_with_random_keys: Setup, fake: FakeForge
) -> None:
    setup = setup_with_random_keys
    org = await _org(setup, fake)
    ada = await organiser(setup, fake, 7, org, Role.ADMIN)
    await contests.create(setup, ada, OrgId(org.org), "spring")
    first = await names.scope_at(setup, "acme", "spring")
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Name).where(Name.id == contest_id_of(first)).values(name="spring-2025")
        )

    await contests.create(setup, ada, OrgId(org.org), "spring")
    second = await names.scope_at(setup, "acme", "spring")

    assert second != first
    assert (await names.scope_at(setup, "acme", "spring-2025")) == first
    assert await fake.content.exists(contest_id_of(first))


@pytest.mark.parametrize(
    ("address", "missing"),
    [
        (("nowhere", "spring"), "There is no such contest."),
        (("acme", "autumn"), "There is no such contest."),
        (("nowhere", "spring", "sum"), "There is no such task."),
        (("acme", "autumn", "sum"), "There is no such task."),
        (("acme", "spring", "product"), "There is no such task."),
    ],
)
async def test_an_address_with_a_part_not_there_is_refused_in_the_words_a_hidden_one_gets(
    setup_with_random_keys: Setup, fake: FakeForge, address: tuple[str, ...], missing: str
) -> None:
    await _contest_and_task(setup_with_random_keys, fake)

    with pytest.raises(NotFound, match=missing):
        await names.scope_at(setup_with_random_keys, *address)


async def test_a_refusal_names_the_scope_by_its_names_not_its_keys(
    setup_with_random_keys: Setup, fake: FakeForge
) -> None:
    setup = setup_with_random_keys
    contest, _ = await _contest_and_task(setup, fake)

    with pytest.raises(Forbidden, match=r"observer role at acme/spring\.$"):
        await organiser(setup, fake, 8, contest, Role.OBSERVER)


async def _bob_workflow(fake: FakeForge) -> None:
    """bob's own public workflow `bob/sorting` at `v1`, the built-in one under
    his name.
    """
    bob = AsUser(8, fake.mint(8))
    made = await fake.workflows.create_workflow(
        bob,
        "bob",
        "sorting",
        {"workflow.yaml": CLASSIC},
        Visibility.PUBLIC,
    )
    await fake.workflows.create_workflow_version(bob, made, "v1")


async def _save_with(setup: Setup, acme: Acme, task: TaskId, workflow: bytes) -> object:
    current = await acme.fake.content.read_file(PLATFORM, task, "task.yaml")
    head = await acme.fake.content.list_files(PLATFORM, task)
    content = current.content.replace(b"workflow: unicon/classic@v2", b"workflow: " + workflow)
    if content == current.content:
        content += b"\n"
    return await publications.save(
        setup, acme.ada, task, {"task.yaml": Edit(content, head.tokens["task.yaml"])}
    )


async def test_a_workflow_name_that_comes_to_mean_another_workflow_is_refused_at_its_line(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _bob_workflow(acme.fake)
    first = await _save_with(setup, acme, sum_task, b"bob/sorting@v1")
    assert isinstance(first, Published)
    (publication,) = await acme.fake.workspaces.list_publications(sum_task)
    assert set(publication.workflows) == {"bob/sorting"}

    del acme.fake.state.repos[("bob", "sorting.workflow")]
    await _bob_workflow(acme.fake)
    again = await _save_with(setup, acme, sum_task, b"bob/sorting@v1")

    assert isinstance(again, Draft)
    (error,) = again.errors
    assert error["path"] == "workflow"
    assert "bob/sorting now names a different workflow" in error["message"]


async def test_the_same_workflow_saved_again_publishes(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _bob_workflow(acme.fake)
    assert isinstance(await _save_with(setup, acme, sum_task, b"bob/sorting@v1"), Published)

    again = await _save_with(setup, acme, sum_task, b"bob/sorting@v1")

    assert isinstance(again, Published)
    assert again.number == 2


async def test_a_workflow_named_by_its_org_is_read_from_the_org_key(
    setup_with_random_keys: Setup, fake: FakeForge
) -> None:
    setup = setup_with_random_keys
    org = await _org(setup, fake)
    made = await fake.workflows.create_workflow(
        PLATFORM, org.org, "sorting", {"workflow.yaml": CLASSIC}, Visibility.PUBLIC
    )

    async with setup.unit_of_work() as ctx:
        found = await names.workflow_id(ctx, parse_workflow_ref("acme/sorting@v1"))
        platform = await names.workflow_id(ctx, parse_workflow_ref("unicon/classic@v2"))
        person = await names.workflow_id(ctx, parse_workflow_ref("bob/mine@v1"))

    assert (found, platform, person) == (made, "unicon/classic", "bob/mine")
