"""Editing a workflow after it is made: its draft read with the token a
save carries, a save as the person with the conflict check, a version made
only of a draft that checks and never changed after, a definition checked
on its own, its three visibilities with the list of readers, a read at a
version as a named user, a copy at a version, a combination of several,
the workflows a person may read, and the newest version a task's notice
names.
"""

import pytest

from forge.domain.errors import Conflict, Forbidden, InvalidName, NotFound, Rejected
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import WorkflowId
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.domain.workflow_definition import parse_workflow, parse_workflow_ref
from forge.domain.workflows import Visibility
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import primitives, workflows
from forge.testing import CLASSIC
from tests.services.conftest import Acme, signed_in

BROKEN_REFERENCE = CLASSIC.replace(
    b"binary: ${{ steps.compile.binary }}", b"binary: ${{ steps.compiler.binary }}"
).decode()


async def _own(setup: Setup, acme: Acme) -> Session:
    """bob, signed in, with his own workflow bob/tuned."""
    bob = await signed_in(setup, acme.fake, 8)
    await workflows.create(setup, bob, "bob", "tuned")
    return bob


async def test_the_owner_reads_the_draft_saves_it_and_versions_it(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)

    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.summary.editable is True
    assert page.summary.visibility is Visibility.PRIVATE
    assert page.summary.versions == ()
    assert page.draft is not None
    edited = page.draft.text.replace("fold: max, better: lower", "fold: sum, better: lower", 1)
    saved = await workflows.save(setup, bob, "bob", "tuned", edited, page.draft.token)

    assert saved.text == edited
    assert saved.token != page.draft.token
    repo = acme.fake.state.repos[("bob", "tuned.workflow")]
    assert repo.files["workflow.yaml"] == edited.encode()
    assert repo.history[-1].author_id == 8
    assert await workflows.create_version(setup, bob, "bob", "tuned", "v1") == "v1"
    later = edited.replace("fold: sum", "fold: mean", 1)
    await workflows.save(setup, bob, "bob", "tuned", later, saved.token)

    assert await workflows.read_version(setup, bob, "bob/tuned@v1") == edited
    assert (await workflows.view(setup, bob, "bob", "tuned")).summary.versions == ("v1",)


async def test_a_save_with_a_stale_token_is_a_conflict(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)
    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.draft is not None
    await workflows.save(setup, bob, "bob", "tuned", page.draft.text + "\n", page.draft.token)

    with pytest.raises(Conflict, match="has changed since you read it"):
        await workflows.save(setup, bob, "bob", "tuned", page.draft.text, page.draft.token)


async def test_a_draft_with_problems_saves_and_its_version_is_refused_at_each_path(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.draft is not None

    await workflows.save(setup, bob, "bob", "tuned", BROKEN_REFERENCE, page.draft.token)
    with pytest.raises(InvalidDefinition) as refused:
        await workflows.create_version(setup, bob, "bob", "tuned", "v1")

    assert refused.value.errors == [
        {
            "path": "steps[1].with.binary",
            "message": "compiler is not a step before this one.",
        }
    ]
    assert acme.fake.state.repos[("bob", "tuned.workflow")].versions == {}


async def test_a_version_of_a_draft_that_does_not_parse_is_refused(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.draft is not None
    await workflows.save(setup, bob, "bob", "tuned", "test: {}\nsteps: []\n", page.draft.token)

    with pytest.raises(InvalidDefinition) as refused:
        await workflows.create_version(setup, bob, "bob", "tuned", "v1")

    assert {error["path"] for error in refused.value.errors} == {"test", "steps"}


@pytest.mark.parametrize("version", ["", "v 1", "-v1", "published/1", "v" * 41])
async def test_a_version_name_that_breaks_the_rules_is_refused(
    setup: Setup, acme: Acme, version: str
) -> None:
    bob = await _own(setup, acme)

    with pytest.raises(InvalidName):
        await workflows.create_version(setup, bob, "bob", "tuned", version)


async def test_a_version_name_taken_is_a_conflict(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)
    await workflows.create_version(setup, bob, "bob", "tuned", "v1")

    with pytest.raises(Conflict, match="has a version v1 already"):
        await workflows.create_version(setup, bob, "bob", "tuned", "v1")


async def test_someone_else_may_not_save_or_version_a_workflow(setup: Setup, acme: Acme) -> None:
    await _own(setup, acme)
    ada = await signed_in(setup, acme.fake, 7)

    with pytest.raises(Forbidden):
        await workflows.save(setup, ada, "bob", "tuned", "x", None)
    with pytest.raises(Forbidden):
        await workflows.create_version(setup, ada, "bob", "tuned", "v1")
    with pytest.raises(NotFound, match="There is no workflow bob/tuned you may read"):
        await workflows.view(setup, ada, "bob", "tuned")


async def test_a_draft_over_the_size_is_refused(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)

    with pytest.raises(Rejected, match="at most 256 KB"):
        await workflows.save(setup, bob, "bob", "tuned", "#" * (256 * 1024 + 1), None)


# Checking a definition on its own


async def test_a_valid_definition_checks_with_no_problem(setup: Setup, acme: Acme) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    assert await workflows.check(setup, bob, CLASSIC.decode()) == ()


async def test_a_bad_step_reference_is_named_at_its_path(setup: Setup, acme: Acme) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    found = await workflows.check(setup, bob, BROKEN_REFERENCE)

    assert found == (
        {"path": "steps[1].with.binary", "message": "compiler is not a step before this one."},
    )
    assert acme.fake.calls_to("write_workflow_file") == []


async def test_a_step_that_uses_a_workflow_is_not_a_primitive(setup: Setup, acme: Acme) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    uses_workflow = CLASSIC.replace(b"unicon/diff-check@v2", b"unicon/classic@v2").decode()

    found = await workflows.check(setup, bob, uses_workflow)

    assert found == (
        {
            "path": "steps[2].use",
            "message": "unicon/classic@v2 is not a primitive: a step uses a primitive, never a "
            "workflow.",
        },
    )


async def test_a_definition_that_does_not_parse_is_answered_with_its_paths(
    setup: Setup, acme: Acme
) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    found = await workflows.check(setup, bob, "test: {input: file}\nsteps: []\nname: x\n")

    assert {problem["path"] for problem in found} == {"steps", "name"}


# Who reads a workflow


@pytest.mark.parametrize(
    ("visibility", "owner_reads", "shared_reads", "stranger_reads"),
    [
        (Visibility.PRIVATE, True, False, False),
        (Visibility.SHARED, True, True, False),
        (Visibility.PUBLIC, True, True, True),
    ],
)
async def test_a_named_user_reads_a_version_by_the_workflows_visibility(
    setup: Setup,
    acme: Acme,
    visibility: Visibility,
    owner_reads: bool,
    shared_reads: bool,
    stranger_reads: bool,
) -> None:
    bob = await _own(setup, acme)
    acme.fake.add_user(31, "cat")
    acme.fake.add_user(32, "dan")
    cat = await signed_in(setup, acme.fake, 31)
    dan = await signed_in(setup, acme.fake, 32)
    await workflows.create_version(setup, bob, "bob", "tuned", "v1")
    await workflows.set_visibility(setup, bob, "bob", "tuned", visibility)
    if visibility is Visibility.SHARED:
        await workflows.share(setup, bob, "bob", "tuned", "cat")

    for session, expected in ((bob, owner_reads), (cat, shared_reads), (dan, stranger_reads)):
        try:
            await workflows.read_version(setup, session, "bob/tuned@v1")
        except NotFound:
            read = False
        else:
            read = True
        assert read is expected
    assert (await workflows.view(setup, bob, "bob", "tuned")).summary.visibility is visibility


async def test_a_reader_removed_loses_the_workflow(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)
    acme.fake.add_user(31, "cat")
    cat = await signed_in(setup, acme.fake, 31)
    await workflows.create_version(setup, bob, "bob", "tuned", "v1")
    await workflows.set_visibility(setup, bob, "bob", "tuned", Visibility.SHARED)
    shared = await workflows.share(setup, bob, "bob", "tuned", "cat")
    assert shared.id == 31
    assert [
        reader.username for reader in (await workflows.view(setup, bob, "bob", "tuned")).readers
    ] == ["cat"]
    await workflows.read_version(setup, cat, "bob/tuned@v1")

    await workflows.unshare(setup, bob, "bob", "tuned", "cat")

    with pytest.raises(NotFound):
        await workflows.read_version(setup, cat, "bob/tuned@v1")
    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.readers == ()
    assert page.summary.visibility is Visibility.PRIVATE


@pytest.mark.parametrize("visibility", [Visibility.PRIVATE, Visibility.PUBLIC])
async def test_private_and_public_empty_the_list_of_readers(
    setup: Setup, acme: Acme, visibility: Visibility
) -> None:
    bob = await _own(setup, acme)
    acme.fake.add_user(31, "cat")
    await workflows.share(setup, bob, "bob", "tuned", "cat")

    await workflows.set_visibility(setup, bob, "bob", "tuned", visibility)

    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.readers == ()
    assert page.summary.visibility is visibility


async def test_a_public_workflow_takes_no_named_reader(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)
    acme.fake.add_user(31, "cat")
    await workflows.set_visibility(setup, bob, "bob", "tuned", Visibility.PUBLIC)

    with pytest.raises(Conflict, match="is public"):
        await workflows.share(setup, bob, "bob", "tuned", "cat")


async def test_sharing_with_someone_who_is_not_there_is_not_found(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)

    with pytest.raises(NotFound, match="There is no user nobody"):
        await workflows.share(setup, bob, "bob", "tuned", "nobody")


async def test_only_the_owner_changes_who_reads_it(setup: Setup, acme: Acme) -> None:
    await _own(setup, acme)
    ada = await signed_in(setup, acme.fake, 7)

    with pytest.raises(Forbidden):
        await workflows.set_visibility(setup, ada, "bob", "tuned", Visibility.PUBLIC)
    with pytest.raises(Forbidden):
        await workflows.share(setup, ada, "bob", "tuned", "ada")


async def test_an_orgs_manager_edits_its_workflow_and_its_observer_only_reads_it(
    setup: Setup, acme: Acme
) -> None:
    ada = await signed_in(setup, acme.fake, 7)
    await workflows.create(setup, ada, "acme", "tuned")
    await acme.fake.orgs.grant_role(8, Scope("acme"), Role.OBSERVER)
    bob = await signed_in(setup, acme.fake, 8)

    managed = await workflows.view(setup, ada, "acme", "tuned")
    observed = await workflows.view(setup, bob, "acme", "tuned")

    assert managed.summary.editable is True
    assert managed.draft is not None
    assert observed.summary.editable is False
    assert observed.draft is None
    with pytest.raises(Forbidden, match="manager role at acme"):
        await workflows.save(setup, bob, "acme", "tuned", "x", managed.draft.token)


# Copying and combining


async def test_a_copy_is_the_sources_file_at_a_version_private_and_marked(
    setup: Setup, acme: Acme
) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    made = await workflows.copy(setup, bob, "unicon/classic@v2", "bob", "mine")

    assert made.id == WorkflowId("bob/mine")
    repo = acme.fake.state.repos[("bob", "mine.workflow")]
    assert repo.private is True
    assert repo.marked == "workflow"
    assert repo.files == {"workflow.yaml": CLASSIC}
    assert repo.history[-1].author_id == 8
    page = await workflows.view(setup, bob, "bob", "mine")
    assert page.draft is not None
    await workflows.save(setup, bob, "bob", "mine", "test: {}\n", page.draft.token)
    source = acme.fake.state.repos[("unicon", "classic.workflow")]
    assert source.files == {"workflow.yaml": CLASSIC}


async def test_a_copy_takes_the_version_named_not_the_draft(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)
    await workflows.create_version(setup, bob, "bob", "tuned", "v1")
    first = await workflows.read_version(setup, bob, "bob/tuned@v1")
    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.draft is not None
    await workflows.save(setup, bob, "bob", "tuned", first + "# later\n", page.draft.token)

    await workflows.copy(setup, bob, "bob/tuned@v1", "bob", "again")

    assert acme.fake.state.repos[("bob", "again.workflow")].files["workflow.yaml"] == (
        first.encode()
    )


async def test_a_copy_of_a_workflow_one_may_not_read_is_not_found(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)
    await workflows.create_version(setup, bob, "bob", "tuned", "v1")
    ada = await signed_in(setup, acme.fake, 7)

    with pytest.raises(NotFound, match="There is no workflow bob/tuned@v1 you may read"):
        await workflows.copy(setup, ada, "bob/tuned@v1", "ada", "taken")
    assert ("ada", "taken.workflow") not in acme.fake.state.repos


async def test_two_workflows_combine_into_one_that_checks_on_its_own(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    await workflows.create_version(setup, bob, "bob", "tuned", "v1")

    made = await workflows.combine(setup, bob, ["unicon/classic@v2", "bob/tuned@v1"], "bob", "both")

    assert made.owner == "bob"
    repo = acme.fake.state.repos[("bob", "both.workflow")]
    assert repo.private is True
    combined = parse_workflow(repo.files["workflow.yaml"])
    assert [step.id for step in combined.steps] == [
        "compile",
        "compile-2",
        "run",
        "check",
        "run-2",
        "check-2",
    ]
    assert await workflows.check(setup, bob, repo.files["workflow.yaml"].decode()) == ()


async def test_combine_takes_two_workflows_or_more(setup: Setup, acme: Acme) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    with pytest.raises(Rejected, match="two workflows or more"):
        await workflows.combine(setup, bob, ["unicon/classic@v2"], "bob", "one")


# Listing and the newest version


async def test_a_person_lists_what_they_may_read_and_what_they_may_edit(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    ada = await signed_in(setup, acme.fake, 7)
    await workflows.create(setup, ada, "ada", "hers")
    await workflows.create(setup, ada, "ada", "shared")
    await workflows.share(setup, ada, "ada", "shared", "bob")
    await workflows.create(setup, ada, "acme", "orgs")

    listed = await workflows.listing(setup, bob)

    assert [(item.owner, item.name, item.editable) for item in listed] == [
        ("ada", "shared", False),
        ("bob", "tuned", True),
        ("unicon", "classic", False),
    ]
    assert listed[0].visibility is Visibility.SHARED
    assert listed[2].versions == ("v2",)
    by_ada = await workflows.listing(setup, ada)
    assert [(item.owner, item.name, item.editable) for item in by_ada] == [
        ("acme", "orgs", True),
        ("ada", "hers", True),
        ("ada", "shared", True),
        ("unicon", "classic", False),
    ]


async def test_the_newest_version_is_named_once_it_comes_after_the_one_pinned(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    await workflows.create_version(setup, bob, "bob", "tuned", "v1")
    as_bob = AsUser(8, acme.fake.mint(8))
    pinned = parse_workflow_ref("bob/tuned@v1")

    async with setup.unit_of_work() as ctx:
        assert await workflows.newer_version(ctx, as_bob, pinned) is None
    await workflows.create_version(setup, bob, "bob", "tuned", "v2")
    await workflows.create_version(setup, bob, "bob", "tuned", "v10")
    async with setup.unit_of_work() as ctx:
        assert await workflows.newer_version(ctx, as_bob, pinned) == "v10"
        assert (
            await workflows.newer_version(ctx, as_bob, parse_workflow_ref("bob/tuned@v10")) is None
        )
        stranger = AsUser(7, acme.fake.mint(7))
        assert await workflows.newer_version(ctx, stranger, pinned) is None


async def test_a_version_is_of_the_save_the_person_made_or_not_made(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.draft is not None
    mine = await workflows.save(
        setup, bob, "bob", "tuned", page.draft.text + "\n", page.draft.token
    )
    await workflows.save(setup, bob, "bob", "tuned", page.draft.text + "\n\n", mine.token)

    with pytest.raises(Conflict, match="has been saved since you saved it"):
        await workflows.create_version(setup, bob, "bob", "tuned", "v1", mine.token)
    assert acme.fake.state.repos[("bob", "tuned.workflow")].versions == {}


async def test_the_list_leaves_other_peoples_public_workflows_to_the_marketplace(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    ada = await signed_in(setup, acme.fake, 7)
    await workflows.create(setup, ada, "ada", "open")
    await workflows.set_visibility(setup, ada, "ada", "open", Visibility.PUBLIC)

    listed = await workflows.listing(setup, bob)

    assert [(item.owner, item.name) for item in listed] == [
        ("bob", "tuned"),
        ("unicon", "classic"),
    ]
    assert await workflows.read_version(setup, bob, "unicon/classic@v2")


async def test_a_primitive_version_with_no_declaration_is_listed_with_why(
    setup: Setup, acme: Acme
) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    repo = acme.fake.state.repos[("unicon", "compile.primitive")]
    acme.fake.state.commit(repo, {"primitive.yaml": None}, "empty", None)
    acme.fake.state.create_version(PLATFORM, repo, "v3")

    listed = await primitives.listing(setup, bob)

    empty = next(found for found in listed if found.ref == "unicon/compile@v3")
    assert (empty.declaration, empty.problem) == (
        None,
        "unicon/compile@v3 has no declaration to read.",
    )
    assert any(found.ref == "unicon/compile@v2" and found.declaration for found in listed)
