"""Contest and task content at Forgejo: repositories created under their org
with starter files, read and written through the contents API as the calling
user, with the file's blob SHA as the conflict check on every write.
"""

import base64
from datetime import datetime
from typing import Any

from forge.domain.content import Change, EntryKind, File, Files, TreeEntry
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import ContestId, TaskId, VersionId
from forge.domain.roles import Scope
from forge.forges.forgejo.base import PLATFORM_ACCOUNT, json_of, list_of
from forge.forges.forgejo.names import (
    DEFAULT_BRANCH,
    PUBLISHED_PREFIX,
    ContestRef,
    TaskRef,
    parse_contest,
    parse_task,
)
from forge.forges.forgejo.orgs import OrgOps
from forge.port import ContentPlace

STARTER_MESSAGE = "Create"


class ContentOps(OrgOps):
    async def create_contest(self, as_: Identity, org: str, name: str, files: Files) -> ContestId:
        ref = ContestRef(org, name)
        await self.create_repo(org, ref.repo, files, private=True)
        await self.attach_scope_teams(Scope(org, name), ref.repo)
        return ref.id

    async def create_task(
        self, as_: Identity, contest: ContestId, name: str, files: Files
    ) -> TaskId:
        parent = parse_contest(contest)
        ref = TaskRef(parent.org, parent.contest, name)
        await self.create_repo(parent.org, ref.repo, files, private=True)
        await self.attach_scope_teams(Scope(parent.org, parent.contest, name), ref.repo)
        await self.protect_versions(parent.org, ref.repo, PUBLISHED_PREFIX)
        return ref.id

    async def read_file(
        self, as_: Identity, place: ContentPlace, path: str, *, version: VersionId | None = None
    ) -> File:
        org, repo = _location(place)
        params = {"ref": str(version)} if version else {}
        entry = json_of(
            await self._http.call(
                as_, "GET", f"/api/v1/repos/{org}/{repo}/contents/{path}", params=params
            )
        )
        return File(path=path, content=_decode(entry), version=VersionId(str(entry["sha"])))

    async def write_file(
        self,
        as_: Identity,
        place: ContentPlace,
        path: str,
        content: bytes,
        *,
        message: str,
        expected_version: VersionId | None,
    ) -> VersionId:
        org, repo = _location(place)
        body: dict[str, Any] = {
            "content": base64.b64encode(content).decode(),
            "message": message,
            "branch": DEFAULT_BRANCH,
        }
        if expected_version is None:
            written = await self._http.call(
                as_, "POST", f"/api/v1/repos/{org}/{repo}/contents/{path}", json=body
            )
        else:
            body["sha"] = str(expected_version)
            written = await self._http.call(
                as_, "PUT", f"/api/v1/repos/{org}/{repo}/contents/{path}", json=body
            )
        return VersionId(str(json_of(written)["commit"]["sha"]))

    async def list_tree(
        self, as_: Identity, place: ContentPlace, path: str = ""
    ) -> tuple[TreeEntry, ...]:
        org, repo = _location(place)
        entries = list_of(
            await self._http.call(as_, "GET", f"/api/v1/repos/{org}/{repo}/contents/{path}")
        )
        return tuple(
            TreeEntry(
                path=str(entry["path"]),
                kind=EntryKind.DIRECTORY if entry["type"] == "dir" else EntryKind.FILE,
                size=int(entry["size"]) if entry.get("size") is not None else None,
            )
            for entry in entries
        )

    async def history(
        self, as_: Identity, place: ContentPlace, path: str | None = None
    ) -> tuple[Change, ...]:
        org, repo = _location(place)
        params: dict[str, str | int] = {"sha": DEFAULT_BRANCH}
        if path:
            params["path"] = path
        commits = await self._http.get_all(as_, f"/api/v1/repos/{org}/{repo}/commits", **params)
        return tuple(_change(commit) for commit in commits)

    async def create_repo(self, org: str, repo: str, files: Files, *, private: bool) -> None:
        await self._http.call(
            PLATFORM,
            "POST",
            f"/api/v1/orgs/{org}/repos",
            json={
                "name": repo,
                "private": private,
                "auto_init": False,
                "default_branch": DEFAULT_BRANCH,
            },
        )
        if files:
            await self._http.call(
                PLATFORM,
                "POST",
                f"/api/v1/repos/{org}/{repo}/contents",
                json={
                    "branch": DEFAULT_BRANCH,
                    "new_branch": DEFAULT_BRANCH,
                    "message": STARTER_MESSAGE,
                    "files": [
                        {
                            "operation": "create",
                            "path": path,
                            "content": base64.b64encode(content).decode(),
                        }
                        for path, content in sorted(files.items())
                    ],
                },
            )
        await self._http.call(
            PLATFORM,
            "POST",
            f"/api/v1/repos/{org}/{repo}/branch_protections",
            json={
                "branch_name": DEFAULT_BRANCH,
                "enable_push": True,
                "enable_force_push": False,
                "block_on_rejected_reviews": False,
            },
        )

    async def protect_versions(self, org: str, repo: str, prefix: str) -> None:
        """Reserve versions under `prefix` for the platform account."""
        await self._http.call(
            PLATFORM,
            "POST",
            f"/api/v1/repos/{org}/{repo}/tag_protections",
            json={"name_pattern": f"{prefix}*", "whitelist_usernames": [PLATFORM_ACCOUNT]},
        )

    async def version_names(self, as_: Identity, org: str, repo: str, prefix: str) -> list[str]:
        found = await self._http.get_all(as_, f"/api/v1/repos/{org}/{repo}/tags")
        return [str(entry["name"]) for entry in found if str(entry["name"]).startswith(prefix)]

    async def create_version(self, org: str, repo: str, name: str, target: str) -> None:
        await self._http.call(
            PLATFORM,
            "POST",
            f"/api/v1/repos/{org}/{repo}/tags",
            json={"tag_name": name, "target": target},
        )

    async def head_version(self, as_: Identity, org: str, repo: str) -> str:
        branch = json_of(
            await self._http.call(
                as_, "GET", f"/api/v1/repos/{org}/{repo}/branches/{DEFAULT_BRANCH}"
            )
        )
        return str(branch["commit"]["id"])


def _location(place: ContentPlace) -> tuple[str, str]:
    if place.count("/") == 1:
        contest = parse_contest(ContestId(place))
        return contest.org, contest.repo
    task = parse_task(TaskId(place))
    return task.org, task.repo


def _decode(entry: dict[str, Any]) -> bytes:
    content = entry.get("content") or ""
    return base64.b64decode(content)


def _change(commit: dict[str, Any]) -> Change:
    author = commit.get("author") or {}
    inner = commit.get("commit") or {}
    message = str(inner.get("message", "")).rstrip("\n")
    at = inner.get("committer", {}).get("date") or inner.get("author", {}).get("date")
    return Change(
        version=VersionId(str(commit["sha"])),
        author_id=int(author["id"]) if author.get("id") is not None else None,
        message=message,
        at=datetime.fromisoformat(str(at)),
    )
