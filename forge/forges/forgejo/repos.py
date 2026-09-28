"""Repositories at Forgejo: creating them, writing and reading files, the
history, versions as tags, protected versions, collaborators, topics and
stars. Every area of the implementation that touches a repository goes
through here.

The forge lets nobody but the platform create a repository: people have no
personal quota and no team may create under an org. So every repository is
made by the platform account, under an org it owns or, through the
administrator's endpoint, under the person who asked for it, who owns it
from then on. What goes into it is written as the caller.
"""

import base64
from datetime import datetime
from typing import Any

from forge.domain.content import Change, ConflictToken, EntryKind, File, Files, TreeEntry
from forge.domain.errors import Conflict, NotFound
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import VersionId
from forge.forges.forgejo.http import Http, json_of, list_of
from forge.forges.forgejo.names import DEFAULT_BRANCH

CREATE_MESSAGE = "Create"
TREE_PAGE = 1000
PAGE_LIMIT = 50
WRITE_ATTEMPTS = 4


class Repos:
    def __init__(self, http: Http, *, platform_account: str) -> None:
        self._http = http
        self._platform_account = platform_account

    async def create(
        self, as_: Identity, owner: str, name: str, files: Files, *, private: bool
    ) -> None:
        """Make a repository under an org, or under the person `owner` names,
        as the platform, with its first commit written as `as_` and no
        force-push on its default branch.
        """
        body = {
            "name": name,
            "private": private,
            "auto_init": False,
            "default_branch": DEFAULT_BRANCH,
        }
        target = (
            f"/api/v1/orgs/{owner}/repos"
            if await self._is_org(owner)
            else f"/api/v1/admin/users/{owner}/repos"
        )
        await self._http.call(PLATFORM, "POST", target, json=body)
        await self.write_files(as_, owner, name, files, message=CREATE_MESSAGE)
        await self._http.call(
            PLATFORM,
            "POST",
            f"/api/v1/repos/{owner}/{name}/branch_protections",
            json={
                "branch_name": DEFAULT_BRANCH,
                "enable_push": True,
                "enable_force_push": False,
                "block_on_rejected_reviews": False,
            },
        )

    async def write_files(
        self, as_: Identity, owner: str, name: str, files: Files, *, message: str
    ) -> VersionId | None:
        """Create or update the files in one commit and return its version,
        or none when there was nothing to write. The host refuses a write
        whose view of the tree has moved under it, which two writers at once
        do to each other, so the tree is read again and the write repeated a
        few times before that is reported.
        """
        if not files:
            return None
        for attempt in range(WRITE_ATTEMPTS):
            try:
                return await self._write_files_once(as_, owner, name, files, message)
            except Conflict:
                if attempt == WRITE_ATTEMPTS - 1:
                    raise
        raise Conflict(f"{owner}/{name} kept changing while it was written")

    async def _write_files_once(
        self, as_: Identity, owner: str, name: str, files: Files, message: str
    ) -> VersionId:
        existing = await self._existing(as_, owner, name)
        first = {"new_branch": DEFAULT_BRANCH} if existing is None else {}
        present = existing or {}
        written = await self._http.call(
            as_,
            "POST",
            f"/api/v1/repos/{owner}/{name}/contents",
            json={
                "branch": DEFAULT_BRANCH,
                **first,
                "message": message,
                "files": [
                    {
                        "operation": "update" if path in present else "create",
                        "path": path,
                        "content": base64.b64encode(content).decode(),
                        **({"sha": present[path]} if path in present else {}),
                    }
                    for path, content in sorted(files.items())
                ],
            },
        )
        return VersionId(str(json_of(written)["commit"]["sha"]))

    async def read_file(
        self, as_: Identity, owner: str, name: str, path: str, *, at: str | None = None
    ) -> File:
        params = {"ref": at} if at else {}
        entry = json_of(
            await self._http.call(
                as_, "GET", f"/api/v1/repos/{owner}/{name}/contents/{path}", params=params
            )
        )
        if entry.get("type") != "file":
            raise NotFound(f"{path} is not a file")
        return File(
            path=path,
            content=base64.b64decode(entry.get("content") or ""),
            token=ConflictToken(str(entry["sha"])),
        )

    async def write_file(
        self,
        as_: Identity,
        owner: str,
        name: str,
        path: str,
        content: bytes,
        *,
        message: str,
        expected: ConflictToken | None,
    ) -> VersionId:
        body: dict[str, Any] = {
            "content": base64.b64encode(content).decode(),
            "message": message,
            "branch": DEFAULT_BRANCH,
        }
        target = f"/api/v1/repos/{owner}/{name}/contents/{path}"
        if expected is None:
            if await self._exists(as_, owner, name, path):
                raise Conflict(f"{path} already exists")
            written = await self._http.call(as_, "POST", target, json=body)
        else:
            body["sha"] = str(expected)
            written = await self._http.call(as_, "PUT", target, json=body)
        return VersionId(str(json_of(written)["commit"]["sha"]))

    async def tree(
        self, as_: Identity, owner: str, name: str, path: str = ""
    ) -> tuple[TreeEntry, ...]:
        entries = list_of(
            await self._http.call(as_, "GET", f"/api/v1/repos/{owner}/{name}/contents/{path}")
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
        self, as_: Identity, owner: str, name: str, path: str | None = None
    ) -> tuple[Change, ...]:
        params: dict[str, str | int] = {"sha": DEFAULT_BRANCH}
        if path:
            params["path"] = path
        commits = await self._http.get_all(as_, f"/api/v1/repos/{owner}/{name}/commits", **params)
        return tuple(_change(commit) for commit in commits)

    async def files_at(self, as_: Identity, owner: str, name: str, at: str) -> Files:
        """Every file at a version."""
        found = await self._blobs(as_, owner, name, at)
        return {
            path: (await self.read_file(as_, owner, name, path, at=at)).content for path in found
        }

    async def head(self, as_: Identity, owner: str, name: str) -> str:
        branch = json_of(
            await self._http.call(
                as_, "GET", f"/api/v1/repos/{owner}/{name}/branches/{DEFAULT_BRANCH}"
            )
        )
        return str(branch["commit"]["id"])

    async def versions(self, as_: Identity, owner: str, name: str) -> list[str]:
        found = await self._http.get_all(as_, f"/api/v1/repos/{owner}/{name}/tags")
        return [str(entry["name"]) for entry in found]

    async def create_version(
        self, as_: Identity, owner: str, name: str, version: str, target: str
    ) -> None:
        await self._http.call(
            as_,
            "POST",
            f"/api/v1/repos/{owner}/{name}/tags",
            json={"tag_name": version, "target": target},
        )

    async def protect_versions(self, owner: str, name: str, prefix: str) -> None:
        """Reserve versions under `prefix` for the platform account."""
        await self._http.call(
            PLATFORM,
            "POST",
            f"/api/v1/repos/{owner}/{name}/tag_protections",
            json={"name_pattern": f"{prefix}*", "whitelist_usernames": [self._platform_account]},
        )

    async def add_collaborator(
        self, as_: Identity, owner: str, name: str, username: str, *, permission: str
    ) -> None:
        await self._http.call(
            as_,
            "PUT",
            f"/api/v1/repos/{owner}/{name}/collaborators/{username}",
            json={"permission": permission},
        )

    async def remove_collaborator(
        self, as_: Identity, owner: str, name: str, username: str
    ) -> None:
        await self._http.call(
            as_, "DELETE", f"/api/v1/repos/{owner}/{name}/collaborators/{username}"
        )

    async def collaborators(self, owner: str, name: str) -> list[dict[str, Any]]:
        return list_of(
            await self._http.call(PLATFORM, "GET", f"/api/v1/repos/{owner}/{name}/collaborators")
        )

    async def set_private(self, as_: Identity, owner: str, name: str, private: bool) -> None:
        await self._http.call(
            as_, "PATCH", f"/api/v1/repos/{owner}/{name}", json={"private": private}
        )

    async def mark(self, as_: Identity, owner: str, name: str, topic: str) -> None:
        await self._http.call(as_, "PUT", f"/api/v1/repos/{owner}/{name}/topics/{topic}")

    async def star(self, as_: Identity, owner: str, name: str) -> None:
        await self._http.call(as_, "PUT", f"/api/v1/user/starred/{owner}/{name}")

    async def marked(self, topic: str) -> list[dict[str, Any]]:
        """Every repository carrying the topic the caller may see."""
        found = json_of(
            await self._http.call(
                PLATFORM,
                "GET",
                "/api/v1/repos/search",
                params={"q": topic, "topic": "true", "limit": PAGE_LIMIT},
            )
        )
        repos: list[dict[str, Any]] = found.get("data") or []
        return repos

    async def owned_by(self, username: str) -> list[dict[str, Any]]:
        return await self._http.get_all(PLATFORM, f"/api/v1/users/{username}/repos")

    async def under(self, org: str) -> list[dict[str, Any]]:
        return await self._http.get_all(PLATFORM, f"/api/v1/orgs/{org}/repos")

    async def record(self, owner: str, name: str) -> dict[str, Any]:
        return json_of(await self._http.call(PLATFORM, "GET", f"/api/v1/repos/{owner}/{name}"))

    async def _existing(self, as_: Identity, owner: str, name: str) -> dict[str, str] | None:
        """The blob of every file on the default branch, or none for a
        repository with no commit yet.
        """
        try:
            await self._http.call(
                as_, "GET", f"/api/v1/repos/{owner}/{name}/branches/{DEFAULT_BRANCH}"
            )
        except NotFound:
            return None
        return await self._blobs(as_, owner, name, DEFAULT_BRANCH)

    async def _blobs(self, as_: Identity, owner: str, name: str, at: str) -> dict[str, str]:
        tree = json_of(
            await self._http.call(
                as_,
                "GET",
                f"/api/v1/repos/{owner}/{name}/git/trees/{at}",
                params={"recursive": "true", "per_page": TREE_PAGE},
            )
        )
        return {
            str(entry["path"]): str(entry["sha"])
            for entry in tree.get("tree") or []
            if entry.get("type") == "blob"
        }

    async def _exists(self, as_: Identity, owner: str, name: str, path: str) -> bool:
        try:
            await self._http.call(as_, "GET", f"/api/v1/repos/{owner}/{name}/contents/{path}")
        except NotFound:
            return False
        return True

    async def _is_org(self, owner: str) -> bool:
        try:
            await self._http.call(PLATFORM, "GET", f"/api/v1/orgs/{owner}")
        except NotFound:
            return False
        return True


def _change(commit: dict[str, Any]) -> Change:
    author = commit.get("author") or {}
    inner = commit.get("commit") or {}
    at = inner.get("committer", {}).get("date") or inner.get("author", {}).get("date")
    return Change(
        version=VersionId(str(commit["sha"])),
        author_id=int(author["id"]) if author.get("id") is not None else None,
        message=str(inner.get("message", "")).rstrip("\n"),
        at=datetime.fromisoformat(str(at)),
    )
