"""Workflows through the Forgejo implementation against a real Forgejo, made
by the people who own them. The forge lets nobody but the platform create a
repository, so a workflow is made as the platform: a person's own under
their name, which they then own, and an org's in the org, where the org's
role teams reach it. The person who asked writes its first commit, names its
versions and reads it; its visibility and who it is shared with change as
the platform once the forge says the person may write it.
"""

import contextlib
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from forge.domain.errors import Conflict, Forbidden, NotFound
from forge.domain.identity import AsUser
from forge.domain.ids import OrgId
from forge.domain.roles import Role, Scope
from forge.domain.workflows import Visibility
from forge.forges.forgejo import ForgejoForge
from tests.live.conftest import LIVE, as_person, delete_org, delete_user, make_user

pytestmark = LIVE

DEFINITION = {"workflow.yaml": b"steps: []\n"}
TOPIC = "unicon-workflow"


@pytest.fixture(scope="module")
def org(admin: httpx.Client, stamp: str) -> Iterator[str]:
    name = f"live-flows-{stamp}"
    yield name
    delete_org(admin, name)


@pytest.fixture(scope="module")
def people(admin: httpx.Client, stamp: str) -> Iterator[dict[str, dict[str, Any]]]:
    """An ordinary person, the org's manager and observer, and a stranger to
    both.
    """
    made = {
        role: make_user(admin, f"live-{role}-{stamp}")
        for role in ("author", "manager", "observer", "stranger")
    }
    yield made
    for person in made.values():
        delete_user(admin, person["login"])


@pytest.fixture(scope="module")
def acting(admin: httpx.Client, people: dict[str, dict[str, Any]]) -> dict[str, AsUser]:
    return {role: as_person(admin, person) for role, person in people.items()}


async def _org_with_roles(forge: ForgejoForge, org: str, people: dict[str, dict[str, Any]]) -> None:
    with contextlib.suppress(Conflict):
        await forge.orgs.create_org(OrgId(org), description="Live workflows")
    await forge.orgs.create_roles(OrgId(org))
    await forge.orgs.grant_role(int(people["manager"]["id"]), Scope(org), Role.MANAGER)
    await forge.orgs.grant_role(int(people["observer"]["id"]), Scope(org), Role.OBSERVER)


def _repo(admin: httpx.Client, owner: str, name: str) -> dict[str, Any]:
    found = admin.get(f"/api/v1/repos/{owner}/{name}.workflow")
    assert found.status_code == 200, found.text
    record: dict[str, Any] = found.json()
    return record


def _made_as_asked(admin: httpx.Client, owner: str, name: str, author: str) -> None:
    """The repository carries the workflow mark, its `main` is protected,
    which Forgejo keeps from every force-push, and its first commit is the
    person's.
    """
    path = f"/api/v1/repos/{owner}/{name}.workflow"
    assert TOPIC in admin.get(f"{path}/topics").json()["topics"]
    protection = admin.get(f"{path}/branch_protections/main")
    assert protection.status_code == 200, protection.text
    (first,) = admin.get(f"{path}/commits", params={"sha": "main"}).json()
    assert first["author"]["login"] == author


async def _unreadable(forge: ForgejoForge, as_: AsUser, workflow: Any) -> None:
    with pytest.raises((Forbidden, NotFound)):
        await forge.workflows.read_workflow_file(as_, workflow, "v1", "workflow.yaml")


async def test_a_person_makes_their_own_workflow_names_a_version_and_shares_it(
    forge: ForgejoForge,
    admin: httpx.Client,
    people: dict[str, dict[str, Any]],
    acting: dict[str, AsUser],
) -> None:
    author, stranger = people["author"]["login"], acting["stranger"]
    workflow = await forge.workflows.create_workflow(
        acting["author"], author, "own", DEFINITION, Visibility.PRIVATE
    )
    await forge.workflows.create_workflow_version(acting["author"], workflow, "v1")

    read = await forge.workflows.read_workflow_file(
        acting["author"], workflow, "v1", "workflow.yaml"
    )
    assert read.content == DEFINITION["workflow.yaml"]
    assert _repo(admin, author, "own")["owner"]["login"] == author
    _made_as_asked(admin, author, "own", author)
    owned = await forge.workflows.workflows_owned_by(int(people["author"]["id"]))
    assert [(entry.id, entry.visibility) for entry in owned] == [(workflow, Visibility.PRIVATE)]
    await _unreadable(forge, stranger, workflow)

    await forge.workflows.share_workflow(acting["author"], workflow, int(people["stranger"]["id"]))
    shared = await forge.workflows.read_workflow_file(stranger, workflow, "v1", "workflow.yaml")
    assert shared.content == DEFINITION["workflow.yaml"]
    (entry,) = await forge.workflows.workflows_owned_by(int(people["author"]["id"]))
    assert entry.visibility is Visibility.SHARED
    with pytest.raises(Forbidden):
        await forge.workflows.set_workflow_visibility(stranger, workflow, Visibility.PUBLIC)
    await forge.workflows.unshare_workflow(
        acting["author"], workflow, int(people["stranger"]["id"])
    )
    await _unreadable(forge, stranger, workflow)

    await forge.workflows.set_workflow_visibility(acting["author"], workflow, Visibility.PUBLIC)
    assert _repo(admin, author, "own")["private"] is False
    found = await forge.workflows.search_public_workflows("own")
    assert workflow in [entry.id for entry in found]


async def test_an_org_manager_makes_a_workflow_its_observer_reads_and_a_stranger_cannot(
    forge: ForgejoForge,
    admin: httpx.Client,
    org: str,
    people: dict[str, dict[str, Any]],
    acting: dict[str, AsUser],
) -> None:
    await _org_with_roles(forge, org, people)
    manager, observer, stranger = acting["manager"], acting["observer"], acting["stranger"]

    workflow = await forge.workflows.create_workflow(
        manager, org, "grading", DEFINITION, Visibility.PRIVATE
    )
    await forge.workflows.create_workflow_version(manager, workflow, "v1")

    assert _repo(admin, org, "grading")["owner"]["login"] == org
    _made_as_asked(admin, org, "grading", people["manager"]["login"])
    read = await forge.workflows.read_workflow_file(observer, workflow, "v1", "workflow.yaml")
    assert read.content == DEFINITION["workflow.yaml"]
    await _unreadable(forge, stranger, workflow)
    with pytest.raises(Forbidden):
        await forge.workflows.create_workflow_version(observer, workflow, "v2")
    with pytest.raises(Forbidden):
        await forge.workflows.share_workflow(observer, workflow, int(people["stranger"]["id"]))

    await forge.workflows.share_workflow(manager, workflow, int(people["stranger"]["id"]))
    shared = await forge.workflows.read_workflow_file(stranger, workflow, "v1", "workflow.yaml")
    assert shared.content == DEFINITION["workflow.yaml"]
    await forge.workflows.unshare_workflow(manager, workflow, int(people["stranger"]["id"]))
    await _unreadable(forge, stranger, workflow)

    await forge.workflows.set_workflow_visibility(manager, workflow, Visibility.PUBLIC)
    public = await forge.workflows.read_workflow_file(stranger, workflow, "v1", "workflow.yaml")
    assert public.content == DEFINITION["workflow.yaml"]
    await forge.workflows.set_workflow_visibility(manager, workflow, Visibility.PRIVATE)
    await _unreadable(forge, stranger, workflow)


async def test_a_person_copies_a_public_workflow_into_their_own_name(
    forge: ForgejoForge,
    admin: httpx.Client,
    org: str,
    people: dict[str, dict[str, Any]],
    acting: dict[str, AsUser],
) -> None:
    await _org_with_roles(forge, org, people)
    source = await forge.workflows.create_workflow(
        acting["manager"], org, "shared", DEFINITION, Visibility.PUBLIC
    )
    await forge.workflows.create_workflow_version(acting["manager"], source, "v1")
    stranger = people["stranger"]["login"]

    copied = await forge.workflows.copy_workflow(acting["stranger"], source, "v1", stranger, "kept")

    read = await forge.workflows.read_workflow_file(
        acting["stranger"], copied, "main", "workflow.yaml"
    )
    assert read.content == DEFINITION["workflow.yaml"]
    assert _repo(admin, stranger, "kept")["private"] is True
    _made_as_asked(admin, stranger, "kept", stranger)
    await forge.workflows.star_workflow(acting["stranger"], source)
    found = await forge.workflows.search_public_workflows("shared")
    assert next(entry for entry in found if entry.id == source).stars == 1


async def test_a_person_edits_the_draft_with_its_token_and_versions_a_commit_of_it(
    forge: ForgejoForge,
    admin: httpx.Client,
    people: dict[str, dict[str, Any]],
    acting: dict[str, AsUser],
) -> None:
    author = people["author"]["login"]
    workflow = await forge.workflows.create_workflow(
        acting["author"], author, "drafted", DEFINITION, Visibility.PRIVATE
    )

    head, draft = await forge.workflows.read_workflow_draft(
        acting["author"], workflow, "workflow.yaml"
    )
    assert draft.content == DEFINITION["workflow.yaml"]
    written = await forge.workflows.write_workflow_file(
        acting["author"],
        workflow,
        "workflow.yaml",
        b"steps: [one]\n",
        expected=draft.token,
        message="Edit workflow.yaml",
    )
    assert written.content == b"steps: [one]\n"
    assert written.token != draft.token
    with pytest.raises(Conflict):
        await forge.workflows.write_workflow_file(
            acting["author"],
            workflow,
            "workflow.yaml",
            b"steps: [two]\n",
            expected=draft.token,
            message="Edit workflow.yaml",
        )
    with pytest.raises((Forbidden, NotFound)):
        await forge.workflows.write_workflow_file(
            acting["stranger"],
            workflow,
            "workflow.yaml",
            b"steps: [two]\n",
            expected=written.token,
            message="Edit workflow.yaml",
        )

    await forge.workflows.create_workflow_version(acting["author"], workflow, "v1", at=head)
    first = await forge.workflows.read_workflow_file(
        acting["author"], workflow, "v1", "workflow.yaml"
    )
    assert first.content == DEFINITION["workflow.yaml"]
    commits = admin.get(f"/api/v1/repos/{author}/drafted.workflow/commits", params={"sha": "main"})
    assert commits.json()[0]["author"]["login"] == author


async def test_who_reads_a_workflow_is_described_to_its_readers_alone(
    forge: ForgejoForge,
    people: dict[str, dict[str, Any]],
    acting: dict[str, AsUser],
) -> None:
    author, stranger = people["author"]["login"], people["stranger"]
    workflow = await forge.workflows.create_workflow(
        acting["author"], author, "described", DEFINITION, Visibility.PRIVATE
    )
    await forge.workflows.create_workflow_version(acting["author"], workflow, "v1")

    described = await forge.workflows.describe_workflow(acting["author"], workflow)
    assert (described.visibility, described.versions) == (Visibility.PRIVATE, ("v1",))
    with pytest.raises(NotFound):
        await forge.workflows.describe_workflow(acting["stranger"], workflow)
    readable = await forge.workflows.workflows_readable_by(acting["stranger"])
    assert workflow not in [entry.id for entry in readable]

    await forge.workflows.share_workflow(acting["author"], workflow, int(stranger["id"]))
    (reader,) = await forge.workflows.workflow_readers(acting["author"], workflow)
    assert (reader.id, reader.username) == (int(stranger["id"]), stranger["login"])
    with pytest.raises(Forbidden):
        await forge.workflows.workflow_readers(acting["stranger"], workflow)
    shared = await forge.workflows.describe_workflow(acting["stranger"], workflow)
    assert shared.visibility is Visibility.SHARED
    readable = await forge.workflows.workflows_readable_by(acting["stranger"])
    assert workflow in [entry.id for entry in readable]
