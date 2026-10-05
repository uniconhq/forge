"""The thread area over Forgejo: issues with labels on the contest, task or
desk repository. An announcement lives where everyone in the contest can read
it; a clarification lives on the asker's desk, readable by the asker and the
organisers only. Marking a thread answered is the `answered` label and the
closed state, and taking the mark off is the label removed and the state
open, each left alone when it is already so, so a retry changes nothing. The
org-wide search is Forgejo's issue search by owner, label and state; it
cannot ask for the absence of a label, which is why an answered thread is
closed. The org's label ids are read once and kept, since a label is made
when the org is and never renamed.

The event Forgejo pushes for an issue or a comment names the repository by
its owner and name and the issue by its number, which is all a change needs.
"""

import contextlib
import json
from datetime import datetime
from typing import Any

from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import OrgId, ThreadId
from forge.domain.threads import Comment, Thread, ThreadChange, ThreadKind, ThreadPlace
from forge.forges.forgejo.http import Http, json_of, list_of
from forge.forges.forgejo.labels import ANSWERED
from forge.forges.ids import (
    MalformedId,
    location,
    parse_thread,
    place_of_repo,
    thread_change,
    thread_id,
)

THREAD_EVENTS = frozenset({"issues", "issue_comment"})
SEARCH_PATH = "/api/v1/repos/issues/search"


class ForgejoThreads:
    def __init__(self, http: Http) -> None:
        self._http = http
        self._labels: dict[tuple[str, str], int] = {}

    def thread_of(self, place: ThreadPlace, number: int) -> ThreadId:
        org, repo = location(place)
        return thread_id(org, repo, number)

    async def post_thread(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, title: str, body: str
    ) -> Thread:
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
        return _thread(org, repo, issue, [])

    async def list_threads(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, comments: bool = True
    ) -> tuple[Thread, ...]:
        org, repo = location(place)
        issues = await self._http.get_all(
            as_, f"/api/v1/repos/{org}/{repo}/issues", state="all", labels=kind.value, type="issues"
        )
        threads = [
            await self._with_comments(as_, org, repo, issue)
            if comments
            else _thread(org, repo, issue, [])
            for issue in issues
        ]
        return tuple(sorted(threads, key=lambda thread: thread.number))

    async def read_thread(self, as_: Identity, thread: ThreadId) -> Thread:
        org, repo, number = parse_thread(thread)
        issue = json_of(
            await self._http.call(as_, "GET", f"/api/v1/repos/{org}/{repo}/issues/{number}")
        )
        if issue.get("pull_request"):
            raise NotFound(f"no thread {thread}")
        return await self._with_comments(as_, org, repo, issue)

    async def search_threads(
        self, as_: Identity, org: OrgId, kind: ThreadKind, *, open_only: bool = True
    ) -> tuple[Thread, ...]:
        found = await self._http.get_all(
            as_,
            SEARCH_PATH,
            state="open" if open_only else "all",
            labels=kind.value,
            type="issues",
            owner=org,
        )
        threads = []
        for issue in found:
            repository = issue.get("repository") or {}
            owner, repo = str(repository.get("owner") or ""), str(repository.get("name") or "")
            if owner != org:
                continue
            try:
                threads.append(await self._with_comments(as_, owner, repo, issue))
            except MalformedId:
                continue
        return tuple(sorted(threads, key=lambda thread: thread.created_at))

    async def edit_thread(self, as_: Identity, thread: ThreadId, *, title: str, body: str) -> None:
        await self._patch(as_, thread, {"title": title, "body": body})

    async def close_thread(self, as_: Identity, thread: ThreadId) -> None:
        await self._patch(as_, thread, {"state": "closed"})

    async def comment(self, as_: Identity, thread: ThreadId, body: str) -> None:
        org, repo, number = parse_thread(thread)
        await self._http.call(
            as_, "POST", f"/api/v1/repos/{org}/{repo}/issues/{number}/comments", json={"body": body}
        )

    async def mark_answered(self, as_: Identity, thread: ThreadId) -> None:
        org, repo, number = parse_thread(thread)
        label = await self._label(org, ANSWERED)
        await self._http.call(
            as_,
            "POST",
            f"/api/v1/repos/{org}/{repo}/issues/{number}/labels",
            json={"labels": [label]},
        )
        await self._patch(as_, thread, {"state": "closed"})

    async def unmark_answered(self, as_: Identity, thread: ThreadId) -> None:
        org, repo, number = parse_thread(thread)
        label = await self._label(org, ANSWERED)
        with contextlib.suppress(NotFound):
            await self._http.call(
                as_, "DELETE", f"/api/v1/repos/{org}/{repo}/issues/{number}/labels/{label}"
            )
        await self._patch(as_, thread, {"state": "open"})

    def read_event(self, kind: str, body: bytes) -> ThreadChange | None:
        if kind not in THREAD_EVENTS:
            return None
        try:
            event = json.loads(body)
            repository = event["repository"]
            owner = str((repository.get("owner") or {}).get("login") or "")
            return thread_change(owner, str(repository["name"]), int(event["issue"]["number"]))
        except ValueError, KeyError, TypeError, AttributeError, MalformedId:
            return None

    async def _with_comments(
        self, as_: Identity, org: str, repo: str, issue: dict[str, Any]
    ) -> Thread:
        comments = list_of(
            await self._http.call(
                as_, "GET", f"/api/v1/repos/{org}/{repo}/issues/{issue['number']}/comments"
            )
        )
        return _thread(org, repo, issue, comments)

    async def _patch(self, as_: Identity, thread: ThreadId, change: dict[str, str]) -> None:
        org, repo, number = parse_thread(thread)
        await self._http.call(
            as_, "PATCH", f"/api/v1/repos/{org}/{repo}/issues/{number}", json=change
        )

    async def _label(self, org: str, name: str) -> int:
        """The id of the org's label `name`. The labels are made when the org
        is provisioned; one that is missing is `NotFound`, so an org whose
        label step never ran is reported rather than worked around.
        """
        kept = self._labels.get((org, name))
        if kept is not None:
            return kept
        for label in await self._http.get_all(PLATFORM, f"/api/v1/orgs/{org}/labels"):
            self._labels[(org, str(label["name"]))] = int(label["id"])
        found = self._labels.get((org, name))
        if found is None:
            raise NotFound(f"org {org} has no label {name}")
        return found


def _thread(org: str, repo: str, issue: dict[str, Any], comments: list[dict[str, Any]]) -> Thread:
    labels = {str(label["name"]) for label in issue.get("labels") or []}
    kind = (
        ThreadKind.CLARIFICATION
        if ThreadKind.CLARIFICATION.value in labels
        else ThreadKind.ANNOUNCEMENT
    )
    user = issue.get("user") or {}
    number = int(issue["number"])
    return Thread(
        id=thread_id(org, repo, number),
        place=place_of_repo(org, repo),
        number=number,
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
