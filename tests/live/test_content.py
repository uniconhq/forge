"""Contests, tasks, saves and publications through the Forgejo implementation
against a real Forgejo. A contest and a task are made bare and secured: the
roles of their contest, and of the task itself, attached; force-pushes to
`main` refused, even to an admin of the repository over git; and for a
task, `published/` tags refused to anyone but the platform account. A save
of several files as a person lands as one commit of theirs, a stale token
is a conflict with nothing written, and a publication tags the commit it is
given with its note, numbered after the last.
"""

import os
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from forge.adapters.git.forgejo import ForgejoForge
from forge.domain.definitions import starter_contest, starter_task
from forge.domain.errors import Conflict
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import ContestId, OrgId, TaskId
from forge.domain.publications import write_note
from forge.domain.roles import Role, Scope
from tests.live.conftest import (
    LIVE,
    PASSWORD,
    URL,
    as_person,
    delete_org,
    delete_user,
    make_user,
    platform_account,
)

pytestmark = LIVE

ROLES = ("admin", "manager", "observer")


@pytest.fixture(scope="module")
def org(admin: httpx.Client, stamp: str) -> Iterator[str]:
    name = f"live-content-{stamp}"
    yield name
    delete_org(admin, name)


@pytest.fixture(scope="module")
def people(admin: httpx.Client, stamp: str) -> Iterator[dict[str, dict[str, Any]]]:
    """A manager of the contest and an admin of the task alone."""
    made = {
        "manager": make_user(admin, f"live-manager-{stamp}"),
        "admin": make_user(admin, f"live-taskadmin-{stamp}"),
    }
    yield made
    for person in made.values():
        delete_user(admin, person["login"])


async def _made(forge: ForgejoForge, org: str, now: datetime) -> tuple[ContestId, TaskId]:
    await forge.orgs.create_org(OrgId(org), description="Live content org")
    await forge.orgs.create_roles(OrgId(org))
    contest = await forge.content.create_contest(
        OrgId(org), "spring", {"contest.yaml": starter_contest("Spring", now)}
    )
    task = await forge.content.create_task(contest, "sum", starter_task("Sum"))
    return contest, task


def _team_names(admin: httpx.Client, org: str, repo: str) -> set[str]:
    teams = admin.get(f"/api/v1/repos/{org}/{repo}/teams").json()
    return {str(team["name"]) for team in teams}


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "credential.helper=", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        check=False,
    )


async def test_a_contest_and_a_task_are_made_bare_then_secured(
    forge: ForgejoForge,
    org: str,
    admin: httpx.Client,
    people: dict[str, dict[str, Any]],
    tmp_path: Path,
) -> None:
    contest, task = await _made(forge, org, datetime.now(UTC))
    contest_teams = {f"{org}.spring-{role}" for role in ROLES}
    task_teams = {f"{org}.spring.sum-{role}" for role in ROLES}

    assert _team_names(admin, org, "spring.contest") & contest_teams == set()
    listed = admin.get(f"/api/v1/repos/{org}/spring.sum.task/contents").json()
    assert sorted(entry["name"] for entry in listed) == [
        "public",
        "statement.md",
        "task.yaml",
        "tests",
    ]
    assert await forge.content.exists(task) is True
    assert await forge.content.exists(TaskId(f"{org}/spring/nope")) is False

    assert await forge.content.secure(contest) == 4
    assert await forge.content.secure(task) == 8
    assert await forge.content.secure(task) == 0

    assert _team_names(admin, org, "spring.contest") >= contest_teams
    assert not _team_names(admin, org, "spring.contest") & task_teams
    assert _team_names(admin, org, "spring.sum.task") >= contest_teams | task_teams
    for repo in ("spring.contest", "spring.sum.task"):
        protection = admin.get(f"/api/v1/repos/{org}/{repo}/branch_protections/main").json()
        assert protection["branch_name"] == "main"
    reserved = admin.get(f"/api/v1/repos/{org}/spring.sum.task/tag_protections").json()
    assert [(entry["name_pattern"], entry["whitelist_usernames"]) for entry in reserved] == [
        ("published/*", [platform_account(admin)])
    ]
    assert admin.get(f"/api/v1/repos/{org}/spring.contest/tag_protections").json() == []

    teams = admin.get(f"/api/v1/orgs/{org}/teams", params={"limit": 50}).json()
    (manager_team,) = [team for team in teams if team["name"] == f"{org}.spring-manager"]
    detached = admin.delete(f"/api/v1/teams/{manager_team['id']}/repos/{org}/spring.sum.task")
    assert detached.status_code == 204
    assert await forge.content.secure(task) == 1
    assert _team_names(admin, org, "spring.sum.task") >= contest_teams

    assert await forge.content.list_contests(PLATFORM, OrgId(org)) == (contest,)
    assert await forge.content.list_tasks(PLATFORM, contest) == (task,)
    manager = people["manager"]
    await forge.orgs.grant_role(int(manager["id"]), Scope(org, "spring"), Role.MANAGER)
    as_manager = as_person(admin, manager)
    assert await forge.content.list_tasks(as_manager, contest) == (task,)
    assert (await forge.content.read_file(as_manager, task, "task.yaml")).content.startswith(
        b"# The task's settings"
    )


async def test_publications_are_the_platforms_alone_and_history_is_not_rewritten(
    forge: ForgejoForge,
    org: str,
    admin: httpx.Client,
    people: dict[str, dict[str, Any]],
    tmp_path: Path,
) -> None:
    assert URL
    task = TaskId(f"{org}/spring/sum")
    repo = "spring.sum.task"
    task_admin = people["admin"]
    await forge.orgs.grant_role(int(task_admin["id"]), Scope(org, "spring", "sum"), Role.ADMIN)
    as_admin = as_person(admin, task_admin)
    head = await forge.content.list_files(as_admin, task)
    await forge.content.save_files(
        as_admin,
        task,
        {"statement.md": b"Add two numbers.\n"},
        expected={"statement.md": head.tokens["statement.md"]},
        message="Statement",
    )
    permission = admin.get(
        f"/api/v1/repos/{org}/{repo}/collaborators/{task_admin['login']}/permission"
    ).json()
    assert permission["permission"] == "write"
    protections = admin.get(f"/api/v1/repos/{org}/{repo}/tag_protections").json()
    as_task_admin = {"Authorization": f"token {as_admin.credential.access}"}
    for protection in protections:
        removed = httpx.delete(
            f"{admin.base_url}/api/v1/repos/{org}/{repo}/tag_protections/{protection['id']}",
            headers=as_task_admin,
            timeout=30,
        )
        assert removed.status_code in (403, 404), removed.text
    branch_rules = httpx.get(
        f"{admin.base_url}/api/v1/repos/{org}/{repo}/branch_protections",
        headers=as_task_admin,
        timeout=30,
    )
    assert branch_rules.status_code in (403, 404), branch_rules.text
    assert admin.get(f"/api/v1/repos/{org}/{repo}/tag_protections").json() == protections

    tagged = httpx.post(
        f"{admin.base_url}/api/v1/repos/{org}/{repo}/tags",
        headers={"Authorization": f"token {as_admin.credential.access}"},
        json={"tag_name": "published/99", "target": "main"},
        timeout=30,
    )
    assert tagged.status_code in (403, 405, 422), tagged.text
    free = httpx.post(
        f"{admin.base_url}/api/v1/repos/{org}/{repo}/tags",
        headers={"Authorization": f"token {as_admin.credential.access}"},
        json={"tag_name": "mine-1", "target": "main"},
        timeout=30,
    )
    assert free.status_code == 201, free.text
    assert "published/99" not in [
        tag["name"] for tag in admin.get(f"/api/v1/repos/{org}/{repo}/tags").json()
    ]

    login = quote(str(task_admin["login"]), safe="")
    remote = f"{URL.rstrip('/').replace('://', f'://{login}:{PASSWORD}@', 1)}/{org}/{repo}.git"
    clone = tmp_path / "clone"
    cloned = _git("clone", remote, str(clone), cwd=tmp_path)
    assert cloned.returncode == 0, cloned.stderr.replace(PASSWORD, "***")
    assert _git("reset", "--hard", "HEAD~1", cwd=clone).returncode == 0
    pushed = _git("push", "--force", "origin", "main", cwd=clone)
    assert pushed.returncode != 0
    assert "protected" in pushed.stderr.lower(), pushed.stderr.replace(PASSWORD, "***")
    tag_pushed = _git("tag", "published/98", cwd=clone)
    assert tag_pushed.returncode == 0
    pushed_tag = _git("push", "origin", "published/98", cwd=clone)
    assert pushed_tag.returncode != 0
    after = await forge.content.list_files(PLATFORM, task)
    assert after.version != head.version
    commits = admin.get(f"/api/v1/repos/{org}/{repo}/commits", params={"sha": "main"}).json()
    assert [commit["commit"]["message"].rstrip("\n") for commit in commits] == [
        "Statement",
        "Create",
    ]


async def test_a_save_is_one_commit_of_the_persons_and_a_publication_tags_it(
    forge: ForgejoForge,
    org: str,
    admin: httpx.Client,
    people: dict[str, dict[str, Any]],
) -> None:
    task = TaskId(f"{org}/spring/sum")
    repo = "spring.sum.task"
    manager = people["manager"]
    as_manager: AsUser = as_person(admin, manager)
    head = await forge.content.list_files(as_manager, task)
    assert head.has("tests/main/1/") and "task.yaml" in head.tokens

    version = await forge.content.save_files(
        as_manager,
        task,
        {
            "task.yaml": b"name: Sum\n",
            "plans/plan.json": b"{}\n",
            "tests/main/1/answer": None,
            "tests/main/2/input": b"1 2\n",
        },
        expected={
            "task.yaml": head.tokens["task.yaml"],
            "tests/main/1/answer": head.tokens["tests/main/1/answer"],
        },
        message="Save",
    )

    commit = admin.get(f"/api/v1/repos/{org}/{repo}/git/commits/{version}").json()
    assert commit["author"]["login"] == manager["login"]
    assert [parent["sha"] for parent in commit["parents"]] == [head.version]
    assert sorted(entry["filename"] for entry in commit["files"]) == [
        "plans/plan.json",
        "task.yaml",
        "tests/main/1/answer",
        "tests/main/2/input",
    ]
    saved = await forge.content.list_files(as_manager, task)
    assert saved.version == version
    assert "tests/main/1/answer" not in saved.tokens

    with pytest.raises(Conflict):
        await forge.content.save_files(
            as_manager,
            task,
            {"task.yaml": b"name: Stale\n", "statement.md": b"x\n"},
            expected={
                "task.yaml": head.tokens["task.yaml"],
                "statement.md": saved.tokens["statement.md"],
            },
            message="Stale",
        )
    with pytest.raises(Conflict):
        await forge.content.save_files(
            as_manager, task, {"task.yaml": b"name: New\n"}, expected={}, message="Exists"
        )
    assert (await forge.content.list_files(PLATFORM, task)).version == version

    first = await forge.workspaces.publish(task, version, write_note(False, ()))
    note = write_note(True, ("plans/plan.json changed",))
    second = await forge.workspaces.publish(task, head.version, note)

    listed = await forge.workspaces.list_publications(task)
    assert [(entry.id, entry.number, entry.version) for entry in listed] == [
        (first, 1, version),
        (second, 2, head.version),
    ]
    assert (listed[0].grading_changed, listed[0].changes) == (False, ())
    assert (listed[1].grading_changed, listed[1].changes) == (True, ("plans/plan.json changed",))
    tags = {tag["name"]: tag for tag in admin.get(f"/api/v1/repos/{org}/{repo}/tags").json()}
    assert tags["published/1"]["commit"]["sha"] == version
    assert tags["published/1"]["message"].strip() == write_note(False, ()).strip()
    old = await forge.content.read_file(as_manager, task, "task.yaml", at=head.version)
    assert old.content.startswith(b"# The task's settings")
