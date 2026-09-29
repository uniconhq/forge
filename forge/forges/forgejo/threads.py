"""The thread area over Forgejo: issues with labels on the contest, task or
desk repository. An announcement lives where everyone in the contest can read
it; a clarification lives on the asker's desk, readable by the asker and the
organisers only.
"""

from datetime import datetime
from typing import Any

from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import ThreadId
from forge.domain.threads import Comment, Thread, ThreadKind, ThreadPlace
from forge.forges.forgejo.http import Http, json_of, list_of
from forge.forges.forgejo.labels import ANSWERED
from forge.forges.ids import location, parse_thread, thread_id


class ForgejoThreads:
    def __init__(self, http: Http) -> None:
        self._http = http

    async def post_thread(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, title: str, body: str
    ) -> ThreadId:
        org, repo = location(place)
        label = await self._label(org, kind.value)
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
        org, repo = location(place)
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
            label = await self._label(org, ANSWERED)
            await self._http.call(
                as_,
                "POST",
                f"/api/v1/repos/{org}/{repo}/issues/{number}/labels",
                json={"labels": [label]},
            )
            await self.close_thread(as_, thread)

    async def _label(self, org: str, name: str) -> int:
        """The id of the org's label `name`. The labels are made when the org
        is provisioned; one that is missing is `NotFound`, so an org whose
        label step never ran is reported rather than worked around.
        """
        for label in await self._http.get_all(PLATFORM, f"/api/v1/orgs/{org}/labels"):
            if label["name"] == name:
                return int(label["id"])
        raise NotFound(f"org {org} has no label {name}")


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
        author_id=_author_id(user),
        created_at=datetime.fromisoformat(str(issue["created_at"])),
        closed=issue.get("state") == "closed",
        answered=ANSWERED in labels,
        comments=tuple(
            _comment(comment) for comment in sorted(comments, key=lambda found: int(found["id"]))
        ),
    )


def _comment(comment: dict[str, Any]) -> Comment:
    user = comment.get("user") or {}
    return Comment(
        id=str(comment["id"]),
        author_id=_author_id(user),
        body=str(comment.get("body") or ""),
        at=datetime.fromisoformat(str(comment["created_at"])),
    )


def _author_id(user: dict[str, Any]) -> int | None:
    """The author, or none for what a deleted user left behind: Forgejo hands
    those to its ghost user, whose id is below zero.
    """
    if user.get("id") is None or int(user["id"]) <= 0:
        return None
    return int(user["id"])
