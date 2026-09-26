"""Announcements and clarifications at Forgejo: issues with labels on the
contest, task or desk repository. An announcement lives where everyone in
the contest can read it; a clarification lives on the asker's desk, readable
by the asker and the organisers only.
"""

from datetime import datetime
from typing import Any

from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import ContestId, TaskId, ThreadId, WorkspaceId
from forge.domain.threads import Comment, Thread, ThreadKind, ThreadPlace
from forge.forges.forgejo.base import ForgejoBase, json_of, list_of
from forge.forges.forgejo.names import (
    is_workspace,
    parse_contest,
    parse_task,
    parse_thread,
    parse_workspace,
    thread_id,
)

ANSWERED = "answered"


class ThreadOps(ForgejoBase):
    async def post_thread(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, title: str, body: str
    ) -> ThreadId:
        org, repo = _location(place)
        label = await self._label_id(org, kind.value)
        issue = json_of(
            await self._http.call(
                as_,
                "POST",
                f"/api/v1/repos/{org}/{repo}/issues",
                json={"title": title, "body": body, "labels": [label]},
            )
        )
        return thread_id(org, repo, int(issue["number"]))

    async def list_threads(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind
    ) -> tuple[Thread, ...]:
        org, repo = _location(place)
        issues = await self._http.get_all(
            as_, f"/api/v1/repos/{org}/{repo}/issues", state="all", labels=kind.value, type="issues"
        )
        threads = []
        for issue in issues:
            comments = list_of(
                await self._http.call(
                    as_, "GET", f"/api/v1/repos/{org}/{repo}/issues/{issue['number']}/comments"
                )
            )
            threads.append(_thread(org, repo, issue, kind, comments))
        return tuple(threads)

    async def edit_thread(self, as_: Identity, thread: ThreadId, *, title: str, body: str) -> None:
        org, repo, number = parse_thread(thread)
        await self._http.call(
            as_,
            "PATCH",
            f"/api/v1/repos/{org}/{repo}/issues/{number}",
            json={"title": title, "body": body},
        )

    async def close_thread(self, as_: Identity, thread: ThreadId) -> None:
        org, repo, number = parse_thread(thread)
        await self._http.call(
            as_, "PATCH", f"/api/v1/repos/{org}/{repo}/issues/{number}", json={"state": "closed"}
        )

    async def comment(
        self, as_: Identity, thread: ThreadId, body: str, *, answered: bool = False
    ) -> None:
        org, repo, number = parse_thread(thread)
        await self._http.call(
            as_, "POST", f"/api/v1/repos/{org}/{repo}/issues/{number}/comments", json={"body": body}
        )
        if answered:
            label = await self._label_id(org, ANSWERED)
            await self._http.call(
                as_,
                "POST",
                f"/api/v1/repos/{org}/{repo}/issues/{number}/labels",
                json={"labels": [label]},
            )
            await self.close_thread(as_, thread)

    async def _label_id(self, org: str, name: str) -> int:
        labels = await self._http.get_all(PLATFORM, f"/api/v1/orgs/{org}/labels")
        for label in labels:
            if label["name"] == name:
                return int(label["id"])
        created = json_of(
            await self._http.call(
                PLATFORM,
                "POST",
                f"/api/v1/orgs/{org}/labels",
                json={"name": name, "color": "cccccc"},
            )
        )
        return int(created["id"])


def _location(place: ThreadPlace) -> tuple[str, str]:
    if place.count("/") == 1:
        contest = parse_contest(ContestId(place))
        return contest.org, contest.repo
    if is_workspace(place):
        workspace = parse_workspace(WorkspaceId(place))
        return workspace.org, workspace.desk_repo
    task = parse_task(TaskId(place))
    return task.org, task.repo


def _thread(
    org: str, repo: str, issue: dict[str, Any], kind: ThreadKind, comments: list[dict[str, Any]]
) -> Thread:
    labels = {str(label["name"]) for label in issue.get("labels") or []}
    user = issue.get("user") or {}
    return Thread(
        id=thread_id(org, repo, int(issue["number"])),
        kind=kind,
        title=str(issue["title"]),
        body=str(issue.get("body") or ""),
        author_id=int(user["id"]) if user.get("id") is not None else None,
        created_at=datetime.fromisoformat(str(issue["created_at"])),
        closed=issue.get("state") == "closed",
        answered=ANSWERED in labels,
        comments=tuple(_comment(comment) for comment in comments),
    )


def _comment(comment: dict[str, Any]) -> Comment:
    user = comment.get("user") or {}
    return Comment(
        id=str(comment["id"]),
        author_id=int(user["id"]) if user.get("id") is not None else None,
        body=str(comment.get("body") or ""),
        at=datetime.fromisoformat(str(comment["created_at"])),
    )
