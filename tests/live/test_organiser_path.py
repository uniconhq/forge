"""The organiser's and the contestant's whole path against the real Forgejo
and Woodpecker, over a `Setup` built on `ForgejoForge` and the test Postgres.
A person asks for an org and the poller makes it, its service account and
that account's sign-in at the CI included; as the org's admin they ask for a
contest and a task and the poller makes those; they save the starter
`task.yaml` with one change and it is published, its plan committed in the
same commit, and the task is activated at the CI trusted for `volumes` and
nothing else with no webhook left on its repository; a bad save is a draft
with its errors and no new tag. Then the contest is published and running,
a second person registers and is approved, the poller opens their desk and a
place for the published task that they write, they read the contest's home
and the task's statement, a visitor reads the same statement, a task
published afterwards gets its place, and removing the contestant takes their
access away and keeps the repositories. Everything made at both services is
removed afterwards.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from forge.domain.errors import NotFound
from forge.domain.ids import ContestId, OrgName, TaskId
from forge.domain.registration import Status, WorkspaceState
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.forges.forgejo import ForgejoForge
from forge.runtime.setup import Setup
from forge.services import (
    access,
    contest_home,
    contestants,
    contests,
    files,
    landing,
    orgs,
    sessions,
    tasks,
)
from forge.services.provisioning import Record
from forge.services.publications import Draft, Published
from forge.settings import Settings
from forge.testing import APP_URL, CALLBACK_PATH, tick
from tests.live.conftest import (
    CI_ADMIN_TOKEN,
    CI_PUBLIC_URL,
    CI_URL,
    FORGE_PUBLIC_URL,
    LIVE,
    credential_of,
    delete_org,
    delete_user,
    forge_config,
    make_user,
    needs_ci,
)

pytestmark = [*LIVE, needs_ci]


@pytest.fixture
def ci() -> Iterator[httpx.Client]:
    assert CI_URL and CI_ADMIN_TOKEN
    with httpx.Client(
        base_url=CI_URL.rstrip("/"),
        headers={"Authorization": f"Bearer {CI_ADMIN_TOKEN}"},
        timeout=30,
    ) as client:
        yield client


@pytest.fixture
def org(admin: httpx.Client, ci: httpx.Client, stamp: str) -> Iterator[str]:
    """The org's name; everything the test makes under it, at the forge and at
    the CI, is removed afterwards.
    """
    name = f"live-path-{stamp}"
    yield name
    for task in ("sum", "later"):
        found = ci.get(f"/api/repos/lookup/{name}/spring.{task}.task")
        if found.status_code == 200:
            ci.delete(f"/api/repos/{found.json()['id']}", params={"remove": "true"})
    found = ci.get(f"/api/orgs/lookup/{name}")
    if found.status_code == 200 and found.json().get("id"):
        ci.delete(f"/api/orgs/{found.json()['id']}")
    ci.delete(f"/api/users/unicon-ci-{name}")
    delete_org(admin, name)
    delete_user(admin, f"unicon-ci-{name}")


@pytest.fixture
def person(admin: httpx.Client, stamp: str) -> Iterator[dict[str, Any]]:
    made = make_user(admin, f"live-organiser-{stamp}")
    yield made
    delete_user(admin, made["login"])


@pytest.fixture
def contestant(admin: httpx.Client, stamp: str) -> Iterator[dict[str, Any]]:
    made = make_user(admin, f"live-contestant-{stamp}")
    yield made
    delete_user(admin, made["login"])


@pytest.fixture
async def live_setup(migrated_database_url: str, admin: httpx.Client) -> AsyncIterator[Setup]:
    settings = Settings.for_tests(
        database_url=migrated_database_url,
        public_url=APP_URL,
        forge_public_url=FORGE_PUBLIC_URL,
        internal_url="http://backend:8000",
    )
    built = Setup.build(
        settings, callback_path=CALLBACK_PATH, forge=ForgejoForge(forge_config(admin))
    )
    try:
        yield built
    finally:
        await built.stop()


async def _ready(setup: Setup, record: Record | None) -> None:
    assert record is not None
    assert (record.status, record.error) == ("ready", None), record


async def _signed_in(setup: Setup, admin: httpx.Client, person: dict[str, Any]) -> Session:
    async with setup.unit_of_work() as ctx:
        return await sessions.create(
            ctx,
            user=await ctx.forge.identity.find_user(int(person["id"])),
            credential=credential_of(admin, str(person["login"])),
            ip=None,
            user_agent=None,
        )


def _running(title: str) -> bytes:
    """A published, public contest that started an hour ago and ends in two."""
    now = datetime.now(UTC).replace(microsecond=0)
    start, end = (now - timedelta(hours=1)).isoformat(), (now + timedelta(hours=2)).isoformat()
    return (
        f"name: {title}\nstart: {start}\nend: {end}\nstate: published\nvisibility: public\n"
    ).encode()


def _permission(admin: httpx.Client, org: str, repo: str, login: str) -> str:
    found = admin.get(f"/api/v1/repos/{org}/{repo}/collaborators/{login}/permission")
    assert found.status_code == 200, found.text
    return str(found.json()["permission"])


async def test_an_organiser_makes_an_org_a_contest_and_a_task_and_a_save_publishes(
    live_setup: Setup,
    admin: httpx.Client,
    ci: httpx.Client,
    org: str,
    person: dict[str, Any],
    contestant: dict[str, Any],
) -> None:
    session = await _signed_in(live_setup, admin, person)

    await orgs.create(live_setup, session, OrgName(org), description="Live organiser path")
    await tick(live_setup, "provisioning")
    await _ready(live_setup, await orgs.status(live_setup, session, OrgName(org)))

    organiser = await access.organiser(live_setup, session, Scope(org), Role.MANAGER)
    await contests.create(live_setup, organiser, OrgName(org), "spring", title="Spring")
    await tick(live_setup, "provisioning")
    contest = ContestId(f"{org}/spring")
    await _ready(live_setup, await contests.status(live_setup, organiser, contest))
    await tasks.create(live_setup, organiser, contest, "sum", title="Sum of Two")
    await tick(live_setup, "provisioning")
    task = TaskId(f"{org}/spring/sum")
    await _ready(live_setup, await tasks.status(live_setup, organiser, task))
    assert await tasks.list(live_setup, organiser, contest) == (task,)

    starter = await files.read(live_setup, organiser, task, "task.yaml")
    saved = await files.write(
        live_setup,
        organiser,
        task,
        "task.yaml",
        starter.content.replace(b"value: 2.0", b"value: 3.0"),
        starter.token,
    )

    assert isinstance(saved, Published)
    assert (saved.number, saved.grading_changed, saved.activation) == (1, False, "done")
    repo = f"/api/v1/repos/{org}/spring.sum.task"
    tags = {tag["name"]: tag for tag in admin.get(f"{repo}/tags").json()}
    assert set(tags) == {"published/1"}
    commit = admin.get(f"{repo}/git/commits/{tags['published/1']['commit']['sha']}").json()
    assert commit["author"]["login"] == person["login"]
    assert sorted(entry["filename"] for entry in commit["files"]) == [
        "plans/default.json",
        "task.yaml",
    ]
    plan = admin.get(f"{repo}/contents/plans/default.json").json()
    assert plan["last_commit_sha"] == tags["published/1"]["commit"]["sha"]

    activated = ci.get(f"/api/repos/lookup/{org}/spring.sum.task")
    assert activated.status_code == 200, activated.text
    assert activated.json()["trusted"] == {"network": False, "volumes": True, "security": False}
    assert activated.json()["active"] is True
    assert CI_PUBLIC_URL
    hooks = admin.get(f"{repo}/hooks").json()
    assert [hook for hook in hooks if hook["config"]["url"].startswith(CI_PUBLIC_URL)] == []

    current = await files.read(live_setup, organiser, task, "task.yaml")
    broken = await files.write(
        live_setup,
        organiser,
        task,
        "task.yaml",
        current.content.replace(b"unicon/classic@v1", b"unicon/classic@v9"),
        current.token,
    )

    assert isinstance(broken, Draft)
    assert [error["path"] for error in broken.errors] == ["workflow"]
    assert {tag["name"] for tag in admin.get(f"{repo}/tags").json()} == {"published/1"}
    state = await tasks.state(live_setup, organiser, task)
    assert (state.draft, state.errors) == (True, broken.errors)
    assert state.latest is not None
    assert state.latest.number == 1
    with pytest.raises(NotFound):
        await files.read(live_setup, organiser, task, "plans/final.json")

    settings = await files.read(live_setup, organiser, contest, "contest.yaml")
    await files.write(
        live_setup, organiser, contest, "contest.yaml", _running("Spring"), settings.token
    )
    entrant = await _signed_in(live_setup, admin, contestant)
    registered = await contestants.register(live_setup, entrant, contest)
    assert registered.status is Status.PENDING
    approved = await contestants.approve(live_setup, organiser, contest, int(contestant["id"]))
    assert approved.workspace is WorkspaceState.PREPARING
    for _ in range(2):
        await tick(live_setup, "provisioning")

    login = str(contestant["login"])
    desk, place = f"spring.{login.lower()}.desk", f"spring.sum.{login.lower()}.sub"
    assert _permission(admin, org, desk, login) == "write"
    assert _permission(admin, org, place, login) == "write"
    mine = await contestants.mine(live_setup, entrant, contest)
    assert mine is not None and mine.workspace is WorkspaceState.READY
    home = await contest_home.home(live_setup, entrant, contest)
    assert [entry.task for entry in home.tasks] == [task]
    page = await contest_home.task(live_setup, entrant, task)
    public = await landing.statement(live_setup, task)
    assert page.statement == public.statement
    assert page.limits.submissions == 50

    await tasks.create(live_setup, organiser, contest, "later", title="Later")
    await tick(live_setup, "provisioning")
    later = TaskId(f"{org}/spring/later")
    starter = await files.read(live_setup, organiser, later, "statement.md")
    published = await files.write(
        live_setup, organiser, later, "statement.md", b"Later.\n", starter.token
    )
    assert isinstance(published, Published)
    await tick(live_setup, "provisioning")
    assert _permission(admin, org, f"spring.later.{login.lower()}.sub", login) == "write"

    removed = await contestants.remove(live_setup, organiser, contest, int(contestant["id"]))
    assert removed.status is Status.REMOVED
    for repo in (desk, place, f"spring.later.{login.lower()}.sub"):
        assert _permission(admin, org, repo, login) == "none"
