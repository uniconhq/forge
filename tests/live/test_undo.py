"""Undoing a create that failed, against the real Forgejo and Woodpecker. The
removals the undo uses: a service account is refused deletion while it is in
its place in the org and deleted once it has left it; an org goes with its
teams and hook once it holds no repository, and an org's address never
reaches a person; a contest or a task goes with its own teams and leaves the
contest's. Then the whole thing over a `Setup`: an org whose commit fails
after every step leaves no org, account or CI user behind, and a task whose
commit fails leaves no repository, team or activation, and its contest's
`contest.yaml` as it was. Everything made is removed afterwards.
"""

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forge.adapters.git.forgejo import ForgejoForge
from forge.domain.definitions import starter_contest, starter_task
from forge.domain.errors import Rejected
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, OrgId, TaskId
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.runtime.setup import Setup
from forge.services import access, contests, orgs, sessions, tasks
from tests.live.conftest import (
    LIVE,
    credential_of,
    delete_org,
    delete_user,
    make_user,
    needs_ci,
)

pytestmark = LIVE

ROLES = ("admin", "manager", "observer")


@pytest.fixture
def org(admin: httpx.Client, stamp: str) -> Iterator[str]:
    name = f"live-undo-{stamp}"
    yield name
    delete_org(admin, name)
    delete_user(admin, f"unicon-ci-{name}")


@pytest.fixture
def person(admin: httpx.Client, stamp: str) -> Iterator[dict[str, Any]]:
    made = make_user(admin, f"live-undoer-{stamp}")
    yield made
    delete_user(admin, made["login"])


def _team_names(admin: httpx.Client, org: str) -> set[str]:
    teams = admin.get(f"/api/v1/orgs/{org}/teams", params={"limit": 50}).json()
    return {str(team["name"]) for team in teams}


async def test_an_org_and_its_service_account_are_removed_in_turn(
    forge: ForgejoForge, admin: httpx.Client, org: str, person: dict[str, Any]
) -> None:
    await forge.orgs.create_org(OrgId(org), description="Live undo")
    await forge.orgs.create_roles(OrgId(org))
    await forge.orgs.create_thread_labels(OrgId(org))
    await forge.orgs.create_event_push(
        OrgId(org), url=f"http://backend/api/v1/events/forge/{org}", secret="live-secret"
    )
    await forge.orgs.grant_role(int(person["id"]), Scope(org), Role.ADMIN)
    account = await forge.identity.create_user(
        f"unicon-ci-{org}",
        f"unicon-ci-{org}@unicon.invalid",
        "live-" + secrets.token_urlsafe(12),
        must_change_password=False,
        visibility="private",
    )
    await forge.orgs.ensure_account_membership(OrgId(org), account.id)

    with pytest.raises(Rejected):
        await forge.identity.delete_user(account.id)
    await forge.orgs.remove_account_membership(OrgId(org), account.id)
    await forge.orgs.remove_account_membership(OrgId(org), account.id)
    await forge.identity.delete_user(account.id)
    await forge.orgs.delete_org(OrgId(org))
    await forge.orgs.delete_org(OrgId(org))
    await forge.orgs.delete_org(OrgId(str(person["login"])))

    assert admin.get(f"/api/v1/orgs/{org}").status_code == 404
    assert admin.get(f"/api/v1/users/unicon-ci-{org}").status_code == 404
    assert admin.get(f"/api/v1/users/{person['login']}").status_code == 200
    teams = admin.get("/api/v1/user/teams", params={"sudo": person["login"]}).json()
    assert all(team["organization"]["username"] != org for team in teams)


async def test_a_task_and_a_contest_are_removed_with_their_own_teams(
    forge: ForgejoForge, admin: httpx.Client, org: str
) -> None:
    await forge.orgs.create_org(OrgId(org), description="Live undo")
    await forge.orgs.create_roles(OrgId(org))
    contest = await forge.content.create_contest(
        OrgId(org), "spring", {"contest.yaml": starter_contest("Spring", datetime.now(UTC))}
    )
    await forge.content.secure(contest)
    task = await forge.content.create_task(contest, "sum", starter_task("Sum"))
    await forge.content.secure(task)
    contest_teams = {f"{org}.spring-{role}" for role in ROLES}
    task_teams = {f"{org}.spring.sum-{role}" for role in ROLES}
    assert _team_names(admin, org) >= contest_teams | task_teams

    with pytest.raises(Rejected):
        await forge.orgs.delete_org(OrgId(org))
    await forge.content.delete_place(task)
    await forge.content.delete_place(task)

    assert admin.get(f"/api/v1/repos/{org}/spring.sum.task").status_code == 404
    assert _team_names(admin, org) & task_teams == set()
    assert _team_names(admin, org) >= contest_teams
    assert await forge.content.exists(contest) is True

    await forge.content.delete_place(contest)

    assert admin.get(f"/api/v1/repos/{org}/spring.contest").status_code == 404
    assert _team_names(admin, org) & contest_teams == set()
    await forge.orgs.delete_org(OrgId(org))
    assert admin.get(f"/api/v1/orgs/{org}").status_code == 404


@pytest.fixture
def made_org(admin: httpx.Client, ci: httpx.Client, stamp: str) -> Iterator[str]:
    """The org's name for one test over a setup; whatever is left under it,
    at the forge and at the CI, is removed afterwards. The CI user goes
    before the CI's org of the forge's org, and takes its own org with it.
    """
    name = f"live-undone-{stamp}-{secrets.token_hex(2)}"
    yield name
    found = ci.get(f"/api/repos/lookup/{name}/spring.sum.task")
    if found.status_code == 200:
        ci.delete(f"/api/repos/{found.json()['id']}", params={"remove": "true"})
    ci.delete(f"/api/users/unicon-ci-{name}")
    found = ci.get(f"/api/orgs/lookup/{name}")
    if found.status_code == 200 and found.json().get("id"):
        ci.delete(f"/api/orgs/{found.json()['id']}")
    delete_org(admin, name)
    delete_user(admin, f"unicon-ci-{name}")


async def _signed_in(setup: Setup, admin: httpx.Client, person: dict[str, Any]) -> Session:
    async with setup.unit_of_work() as ctx:
        return await sessions.create(
            ctx,
            user=await ctx.forge.identity.find_user(int(person["id"])),
            credential=credential_of(admin, str(person["login"])),
            ip=None,
            user_agent=None,
        )


async def _refuse(self: AsyncSession) -> None:
    raise RuntimeError("the database went away at commit")


@needs_ci
async def test_an_org_whose_commit_fails_leaves_nothing_at_the_forge_or_the_ci(
    live_setup: Setup,
    admin: httpx.Client,
    ci: httpx.Client,
    made_org: str,
    person: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = await _signed_in(live_setup, admin, person)

    monkeypatch.setattr(AsyncSession, "commit", _refuse)
    with pytest.raises(RuntimeError, match="went away at commit"):
        await orgs.create(live_setup, session, OrgId(made_org), description="Live undo")
    monkeypatch.undo()

    assert admin.get(f"/api/v1/orgs/{made_org}").status_code == 404
    assert admin.get(f"/api/v1/users/unicon-ci-{made_org}").status_code == 404
    assert ci.get(f"/api/users/unicon-ci-{made_org}").status_code == 404
    assert ci.get(f"/api/orgs/lookup/unicon-ci-{made_org}").status_code == 404

    made = await orgs.create(live_setup, session, OrgId(made_org), description="Live undo")
    assert made.name == made_org


@needs_ci
async def test_a_task_whose_commit_fails_leaves_no_place_team_or_activation(
    live_setup: Setup,
    admin: httpx.Client,
    ci: httpx.Client,
    made_org: str,
    person: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = await _signed_in(live_setup, admin, person)
    await orgs.create(live_setup, session, OrgId(made_org), description="Live undo")
    organiser = await access.organiser(live_setup, session, Scope(made_org), Role.MANAGER)
    await contests.create(live_setup, organiser, OrgId(made_org), "spring")
    contest = ContestId(f"{made_org}/spring")
    before = await live_setup.forge.content.read_file(PLATFORM, contest, "contest.yaml")

    monkeypatch.setattr(AsyncSession, "commit", _refuse)
    with pytest.raises(RuntimeError, match="went away at commit"):
        await tasks.create(live_setup, organiser, contest, "sum")
    monkeypatch.undo()

    assert admin.get(f"/api/v1/repos/{made_org}/spring.sum.task").status_code == 404
    assert _team_names(admin, made_org) & {f"{made_org}.spring.sum-{role}" for role in ROLES} == (
        set()
    )
    assert ci.get(f"/api/repos/lookup/{made_org}/spring.sum.task").status_code == 404
    after = await live_setup.forge.content.read_file(PLATFORM, contest, "contest.yaml")
    assert after.content == before.content

    made = await tasks.create(live_setup, organiser, contest, "sum")
    assert made.id == TaskId(f"{made_org}/spring/sum")
