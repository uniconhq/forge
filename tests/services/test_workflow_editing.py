"""Editing a workflow after it is made: its draft read with the token a
save carries, a save as the person with the conflict check, a version made
only of a draft that checks and never changed after, a definition checked
on its own, its three visibilities with the list of readers, a read at a
version as a named user, a copy at a version, the workflows a person may
read, the newest version a task's notice names, and a version checked
against the files of its own commit.
"""

from pathlib import Path

import pytest

from forge.domain.errors import Conflict, Forbidden, InvalidName, NotFound, Rejected
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import WorkflowId
from forge.domain.program_manifest import MAX_MANIFEST, TOO_LARGE
from forge.domain.roles import Role, Scope
from forge.domain.run_plan import RETIRED
from forge.domain.sessions import Session
from forge.domain.workflow_definition import WORKFLOW_FILE, parse_workflow_ref
from forge.domain.workflows import Visibility
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import primitives, workflows
from forge.testing import CLASSIC, PRIMITIVES, RUN_WORKFLOWS
from tests.conftest import sibling
from tests.services.conftest import Acme, signed_in

WORKFLOW = CLASSIC[WORKFLOW_FILE]
"""The built-in workflow's `workflow.yaml`."""
BROKEN_REFERENCE = WORKFLOW.replace(
    b"output: ${{ steps.solution.output }}", b"output: ${{ steps.solver.output }}"
).decode()


async def _own(setup: Setup, acme: Acme) -> Session:
    """bob, signed in, with his own workflow bob/tuned."""
    bob = await signed_in(setup, acme.fake, 8)
    await workflows.create(setup, bob, "bob", "tuned")
    repo = acme.fake.state.repos[("bob", "tuned.workflow")]
    acme.fake.state.commit(repo, CLASSIC, "Write workflow steps", 8)
    return bob


async def test_the_owner_reads_the_draft_saves_it_and_versions_it(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)

    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.summary.editable is True
    assert page.summary.visibility is Visibility.PRIVATE
    assert page.summary.versions == ()
    assert page.draft is not None
    edited = page.draft.text.replace("time_limit: 3\n", "time_limit: 4\n", 1)
    assert edited != page.draft.text
    saved = await workflows.save(setup, bob, "bob", "tuned", edited, page.draft.token)

    assert saved.text == edited
    assert saved.token != page.draft.token
    repo = acme.fake.state.repos[("bob", "tuned.workflow")]
    assert repo.files["workflow.yaml"] == edited.encode()
    assert repo.history[-1].author_id == 8
    assert await workflows.create_version(setup, bob, "bob", "tuned", "v1") == "v1"
    later = edited.replace("time_limit: 4", "time_limit: 5", 1)
    await workflows.save(setup, bob, "bob", "tuned", later, saved.token)

    assert await workflows.read_version(setup, bob, "bob/tuned@v1") == edited
    assert (await workflows.view(setup, bob, "bob", "tuned")).summary.versions == ("v1",)


async def test_a_save_with_a_stale_token_is_a_conflict(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)
    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.draft is not None
    await workflows.save(setup, bob, "bob", "tuned", page.draft.text + "\n", page.draft.token)

    with pytest.raises(Conflict, match="has changed since you opened it"):
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
            "path": "steps[1].with.files.output",
            "message": "There is no step called solver before this one.",
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


@pytest.mark.parametrize(
    "version",
    ["", "v 1", "-v1", "published/1", "v" * 41, "v0", "v01", "V2", "2", "v2.1", "beta"],
)
async def test_a_version_name_that_breaks_the_rules_is_refused(
    setup: Setup, acme: Acme, version: str
) -> None:
    bob = await _own(setup, acme)

    with pytest.raises(InvalidName) as refused:
        await workflows.create_version(setup, bob, "bob", "tuned", version)

    assert str(refused.value) == (
        f"{version!r} cannot be used as a version. Name a version v and a whole number from "
        "1, with no leading zero, such as v1, v2 or v10."
    )
    assert acme.fake.state.repos[("bob", "tuned.workflow")].versions == {}


@pytest.mark.parametrize("version", ["v1", "v9", "v10", "v123"])
async def test_a_version_is_v_and_a_whole_number(setup: Setup, acme: Acme, version: str) -> None:
    bob = await _own(setup, acme)

    assert await workflows.create_version(setup, bob, "bob", "tuned", version) == version


async def test_a_version_name_taken_is_a_conflict(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)
    await workflows.create_version(setup, bob, "bob", "tuned", "v1")

    with pytest.raises(Conflict, match="already has a version v1"):
        await workflows.create_version(setup, bob, "bob", "tuned", "v1")


async def test_someone_else_may_not_save_or_version_a_workflow(setup: Setup, acme: Acme) -> None:
    await _own(setup, acme)
    ada = await signed_in(setup, acme.fake, 7)

    with pytest.raises(Forbidden):
        await workflows.save(setup, ada, "bob", "tuned", "x", None)
    with pytest.raises(Forbidden):
        await workflows.create_version(setup, ada, "bob", "tuned", "v1")
    with pytest.raises(NotFound, match="There is no workflow bob/tuned that you can read"):
        await workflows.view(setup, ada, "bob", "tuned")


async def test_a_draft_over_the_size_is_refused(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)

    with pytest.raises(Rejected, match="at most 256 KB"):
        await workflows.save(setup, bob, "bob", "tuned", "#" * (256 * 1024 + 1), None)


# Checking a definition on its own


async def test_a_valid_definition_checks_with_no_problem(setup: Setup, acme: Acme) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    assert await workflows.check(setup, bob, WORKFLOW.decode()) == ()


async def test_a_bad_step_reference_is_named_at_its_path(setup: Setup, acme: Acme) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    found = await workflows.check(setup, bob, BROKEN_REFERENCE)

    assert found == (
        {
            "path": "steps[1].with.files.output",
            "message": "There is no step called solver before this one.",
        },
    )
    assert acme.fake.calls_to("write_workflow_file") == []


async def test_a_unit_on_an_input_that_is_no_number_is_refused_by_a_check_and_a_version(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.draft is not None
    stray = WORKFLOW.replace(b"title: Language}", b"title: Language, unit: words}")
    assert stray != WORKFLOW
    refusal = {
        "path": "inputs.language.unit",
        "message": "Only a number takes a unit, and language is one of a list. Remove the "
        "unit, or make language a number.",
    }

    found = await workflows.check(setup, bob, stray.decode())
    await workflows.save(setup, bob, "bob", "tuned", stray.decode(), page.draft.token)
    with pytest.raises(InvalidDefinition) as refused:
        await workflows.create_version(setup, bob, "bob", "tuned", "v1")

    assert found == (refusal,)
    assert refused.value.errors == [refusal]


async def test_a_unit_on_a_reported_text_is_refused_by_a_check_and_a_version(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.draft is not None
    stray = WORKFLOW.replace(b"title: Compile log}", b"title: Compile log, unit: lines}")
    assert stray != WORKFLOW
    refusal = {
        "path": "outputs.log.unit",
        "message": "Only a number takes a unit, and log is text. Remove the unit.",
    }

    found = await workflows.check(setup, bob, stray.decode())
    await workflows.save(setup, bob, "bob", "tuned", stray.decode(), page.draft.token)
    with pytest.raises(InvalidDefinition) as refused:
        await workflows.create_version(setup, bob, "bob", "tuned", "v1")

    assert found == (refusal,)
    assert refused.value.errors == [refusal]


async def test_a_definition_that_does_not_parse_is_answered_with_its_paths(
    setup: Setup, acme: Acme
) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    found = await workflows.check(setup, bob, "test: {input: file}\nsteps: []\nname: x\n")

    assert {problem["path"] for problem in found} == {"test", "steps", "name"}


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

    with pytest.raises(NotFound, match="There is no user called nobody"):
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


# Copying


async def test_a_copy_is_the_sources_file_at_a_version_private_and_marked(
    setup: Setup, acme: Acme
) -> None:
    bob = await signed_in(setup, acme.fake, 8)

    made = await workflows.copy(setup, bob, "unicon/classic@v4", "bob", "mine")

    assert made.id == WorkflowId("bob/mine")
    repo = acme.fake.state.repos[("bob", "mine.workflow")]
    assert repo.private is True
    assert repo.marked == "workflow"
    assert repo.files == CLASSIC
    assert repo.history[-1].author_id == 8
    page = await workflows.view(setup, bob, "bob", "mine")
    assert page.draft is not None
    await workflows.save(setup, bob, "bob", "mine", "test: {}\n", page.draft.token)
    source = acme.fake.state.repos[("unicon", "classic.workflow")]
    assert source.files == CLASSIC


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

    with pytest.raises(NotFound, match="There is no workflow bob/tuned@v1 that you can read"):
        await workflows.copy(setup, ada, "bob/tuned@v1", "ada", "taken")
    assert ("ada", "taken.workflow") not in acme.fake.state.repos


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
    assert listed[2].versions == ("v4",)
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


async def test_the_larger_number_is_newer(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)
    for version in ("v9", "v10"):
        await workflows.create_version(setup, bob, "bob", "tuned", version)
    as_bob = AsUser(8, acme.fake.mint(8))

    async with setup.unit_of_work() as ctx:
        nine = parse_workflow_ref("bob/tuned@v9")
        assert await workflows.newer_version(ctx, as_bob, nine) == "v10"


async def test_a_tag_under_another_name_is_read_but_never_offered_as_newer(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    await workflows.create_version(setup, bob, "bob", "tuned", "v1")
    repo = acme.fake.state.repos[("bob", "tuned.workflow")]
    acme.fake.state.create_version(PLATFORM, repo, "beta")
    as_bob = AsUser(8, acme.fake.mint(8))

    read = await workflows.read_version(setup, bob, "bob/tuned@beta")
    (listed,) = [item for item in await workflows.listing(setup, bob) if item.name == "tuned"]
    async with setup.unit_of_work() as ctx:
        on_v1 = await workflows.newer_version(ctx, as_bob, parse_workflow_ref("bob/tuned@v1"))
        on_beta = await workflows.newer_version(ctx, as_bob, parse_workflow_ref("bob/tuned@beta"))

    assert read == repo.files["workflow.yaml"].decode()
    assert listed.versions == ("beta", "v1")
    assert on_v1 is None
    assert on_beta is None


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

    with pytest.raises(Conflict, match="has been saved again since your save"):
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
    assert await workflows.read_version(setup, bob, "unicon/classic@v4")


async def test_a_primitive_version_with_no_declaration_is_listed_with_why(
    setup: Setup, acme: Acme
) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    repo = acme.fake.state.repos[("unicon", "exec.primitive")]
    acme.fake.state.commit(repo, {"primitive.yaml": None}, "empty", None)
    acme.fake.state.create_version(PLATFORM, repo, "v3")

    listed = await primitives.listing(setup, bob)

    empty = next(found for found in listed if found.ref == "unicon/exec@v3")
    assert (empty.declaration, empty.problem) == (
        None,
        "Unicon cannot read the settings of unicon/exec@v3.",
    )
    assert any(found.ref == "unicon/exec@v2" and found.declaration for found in listed)


async def test_a_retired_primitive_version_is_listed_with_why_and_refused_by_a_check(
    setup: Setup, acme: Acme
) -> None:
    """A forge that was never reset still holds compile and exec@v1: the
    palette lists each with no declaration and the words a step naming it
    is refused with, which a check of a workflow using either gives."""
    bob = await signed_in(setup, acme.fake, 8)
    exec_v2 = PRIMITIVES["unicon/exec@v2"]
    acme.fake.primitives.add("compile", {"v3": exec_v2})
    acme.fake.primitives.add("exec", {"v1": exec_v2})

    listed = await primitives.listing(setup, bob)

    for ref in ("unicon/compile@v3", "unicon/exec@v1"):
        found = next(each for each in listed if each.ref == ref)
        assert (found.declaration, found.problem) == (None, RETIRED)
    assert RETIRED == ("unicon/compile and unicon/exec@v1 are retired; use unicon/exec@v2.")
    for use in ("unicon/compile@v3", "unicon/exec@v1"):
        retired = WORKFLOW.replace(b"use: unicon/exec@v2", f"use: {use}".encode(), 1)
        assert await workflows.check(setup, bob, retired.decode()) == (
            {"path": "steps[0].use", "message": RETIRED},
        )


async def test_a_capitalised_username_lists_and_opens_its_workflows_in_lower_case(
    setup: Setup, acme: Acme
) -> None:
    acme.fake.add_user(33, "Bob2")
    bob = await signed_in(setup, acme.fake, 33)
    await workflows.create(setup, bob, "Bob2", "tuned")
    acme.fake.state.commit(
        acme.fake.state.repos[("Bob2", "tuned.workflow")], CLASSIC, "Write workflow steps", 33
    )
    await workflows.create_version(setup, bob, "bob2", "tuned", "v1")

    (mine,) = [item for item in await workflows.listing(setup, bob) if item.editable]

    assert (mine.owner, mine.name) == ("bob2", "tuned")
    assert (await workflows.view(setup, bob, mine.owner, mine.name)).summary.editable
    made = await workflows.copy(setup, bob, f"{mine.owner}/tuned@v1", "bob2", "both")
    assert made.owner == "bob2"


async def test_a_repository_without_the_workflow_mark_is_no_workflow(
    setup: Setup, acme: Acme
) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    acme.fake.state.create_repo(PLATFORM, "bob", "loose.workflow", {"workflow.yaml": WORKFLOW})

    with pytest.raises(NotFound):
        await workflows.view(setup, bob, "bob", "loose")
    assert "loose" not in [item.name for item in await workflows.listing(setup, bob)]
    with pytest.raises(NotFound):
        await workflows.save(setup, bob, "bob", "loose", WORKFLOW.decode(), None)
    with pytest.raises(NotFound):
        await workflows.copy(setup, bob, "bob/loose@v1", "bob", "copied")


async def test_a_create_of_a_name_left_unmarked_finishes_it(setup: Setup, acme: Acme) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    acme.fake.state.create_repo(PLATFORM, "bob", "loose.workflow", {"workflow.yaml": WORKFLOW})

    with pytest.raises(Conflict):
        await workflows.create(setup, bob, "bob", "loose")

    assert (await workflows.view(setup, bob, "bob", "loose")).summary.editable
    assert acme.fake.state.repo("bob", "loose.workflow").rewrites_refused


def _held(folder: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


@pytest.mark.parametrize("ref", list(RUN_WORKFLOWS))
async def test_a_built_in_run_workflow_versions_with_its_own_files(
    setup: Setup, acme: Acme, ref: str
) -> None:
    bob = await _own(setup, acme)
    repo = acme.fake.state.repos[("bob", "tuned.workflow")]
    acme.fake.state.commit(repo, _held(sibling("deploy", RUN_WORKFLOWS[ref])), "copy", 8)

    assert await workflows.create_version(setup, bob, "bob", "tuned", "v1") == "v1"
    reads = [*acme.fake.calls_to("workflow_files"), *acme.fake.calls_to("read_workflow_at")]
    assert reads
    for read in reads:
        assert isinstance(read.identity, AsUser) and read.identity.user_id == 8
        assert read.arguments["at"] == repo.versions["v1"]


async def test_a_manifest_over_the_largest_size_is_refused_unread(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)
    repo = acme.fake.state.repos[("bob", "tuned.workflow")]
    held = _held(sibling("deploy", RUN_WORKFLOWS["unicon/classic@v4"]))
    held["diff/program.yaml"] = b"#" * (MAX_MANIFEST + 1)
    acme.fake.state.commit(repo, held, "copy", 8)

    with pytest.raises(InvalidDefinition) as refused:
        await workflows.create_version(setup, bob, "bob", "tuned", "v1")

    assert refused.value.errors == [
        {"path": "steps[1].with.manifest", "message": f"diff/program.yaml: {TOO_LARGE}"}
    ]
    assert acme.fake.calls_to("read_workflow_at") == []


async def test_a_run_workflows_path_its_commit_lacks_is_refused_by_a_version_alone(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    text = (sibling("deploy", RUN_WORKFLOWS["unicon/classic@v4"]) / "workflow.yaml").read_bytes()
    repo = acme.fake.state.repos[("bob", "tuned.workflow")]
    acme.fake.state.commit(
        repo, {"workflow.yaml": text, "diff/diff.py": None, "diff/program.yaml": None}, "copy", 8
    )

    assert await workflows.check(setup, bob, text.decode()) == ()
    with pytest.raises(InvalidDefinition) as refused:
        await workflows.create_version(setup, bob, "bob", "tuned", "v1")

    assert refused.value.errors == [
        {"path": "steps[1].with.source", "message": "The workflow has no folder diff/."},
        {
            "path": "steps[1].with.manifest",
            "message": "The workflow has no file diff/program.yaml.",
        },
    ]


async def test_a_manifest_listed_but_not_read_as_a_file_is_refused_at_its_port(
    setup: Setup, acme: Acme, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As a symbolic link the host listed anyway would be."""
    bob = await _own(setup, acme)
    repo = acme.fake.state.repos[("bob", "tuned.workflow")]
    held = _held(sibling("deploy", RUN_WORKFLOWS["unicon/classic@v4"]))
    acme.fake.state.commit(repo, held, "copy", 8)

    async def unreadable(*_: object) -> None:
        raise NotFound("diff/program.yaml is not a file")

    monkeypatch.setattr(acme.fake.workflows, "read_workflow_at", unreadable)
    with pytest.raises(InvalidDefinition) as refused:
        await workflows.create_version(setup, bob, "bob", "tuned", "v1")

    assert refused.value.errors == [
        {
            "path": "steps[1].with.manifest",
            "message": "The workflow has no file diff/program.yaml.",
        }
    ]
