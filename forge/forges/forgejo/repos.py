"""Repositories at Forgejo: creating them, writing and reading files, the
history, versions as tags, protected versions and the protected default
branch, collaborators, topics and stars. Every area of the implementation
that touches a repository goes through here.

The forge lets nobody but the platform create a repository: people have no
personal quota and no team may create under an org. So every repository is
made by the platform account, under an org it owns or, through the
administrator's endpoint, under the person who asked for it, who owns it
from then on. What goes into it is written as the caller. What needs a
repository admin, the protection, the mark, visibility and collaborators,
is done as the platform too, since no organiser's team is one.
"""

import base64
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote

from forge.domain.content import (
    Change,
    ConflictToken,
    EntryKind,
    File,
    Files,
    FileSet,
    TreeEntry,
)
from forge.domain.errors import Conflict, Forbidden, NotFound, Unavailable
from forge.domain.identity import PLATFORM, Identity, Platform
from forge.domain.ids import VersionId
from forge.domain.uploads import Door
from forge.forges.forgejo.http import (
    MAX_PAGES,
    PAGE_SIZE,
    Http,
    file_path,
    json_of,
    list_of,
    segment,
)

DEFAULT_BRANCH = "main"
CREATE_MESSAGE = "Create"
TREE_PAGE = 1000
MAX_TREE_PAGES = 100
PAGE_LIMIT = 50
WRITE_ATTEMPTS = 4


SEARCH_PATH = "/api/v1/repos/search"


class Repos:
    def __init__(self, http: Http, *, platform_account: str) -> None:
        self._http = http
        self._platform_account = platform_account
        self._org_ids: dict[str, int] = {}

    async def create(
        self, as_: Identity, owner: str, name: str, files: Files, *, private: bool
    ) -> None:
        """Make a repository under an org, or under the person `owner` names,
        as the platform, with its first commit written as `as_`. A repository
        of that name with no commit yet is one an earlier try made and did
        not fill, since only the platform makes repositories, so the files
        are written into it; one with commits is `Conflict`.
        """
        body = {
            "name": name,
            "private": private,
            "auto_init": False,
            "default_branch": DEFAULT_BRANCH,
        }
        target = (
            f"/api/v1/orgs/{segment(owner)}/repos"
            if await self._is_org(owner)
            else f"/api/v1/admin/users/{segment(owner)}/repos"
        )
        try:
            await self._http.call(PLATFORM, "POST", target, json=body)
        except Conflict:
            if not files or await self._existing(PLATFORM, owner, name) is not None:
                raise
        await self.write_files(as_, owner, name, files, message=CREATE_MESSAGE)

    async def delete(self, owner: str, name: str) -> None:
        """Delete the repository, as the platform, with everything in it. One
        not there changes nothing.
        """
        try:
            await self._http.call(
                PLATFORM, "DELETE", f"/api/v1/repos/{segment(owner)}/{segment(name)}"
            )
        except NotFound:
            return

    async def protect_branch(self, owner: str, name: str) -> bool:
        """Protect the default branch, which is what refuses a force-push to
        it, so nothing written can be rewritten, and say whether that had to
        be put back. Forgejo has no switch for force-pushes: a protected
        branch refuses them all. Forgejo refuses a second protection of the
        branch, so one another maker put there meanwhile counts as there.
        """
        path = f"/api/v1/repos/{segment(owner)}/{segment(name)}/branch_protections"
        try:
            await self._http.call(PLATFORM, "GET", f"{path}/{DEFAULT_BRANCH}")
        except NotFound:
            try:
                await self._http.call(
                    PLATFORM,
                    "POST",
                    path,
                    json={
                        "branch_name": DEFAULT_BRANCH,
                        "enable_push": True,
                        "block_on_rejected_reviews": False,
                    },
                )
            except Forbidden, Conflict:
                await self._http.call(PLATFORM, "GET", f"{path}/{DEFAULT_BRANCH}")
                return False
            return True
        return False

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
        return await self._write_repeatedly(as_, owner, name, files, message, replace=False)

    async def replace_files(
        self, as_: Identity, owner: str, name: str, files: Files, *, message: str
    ) -> VersionId:
        """Write the files in one commit as `as_` and remove every other file
        on the default branch in the same commit, so the commit holds exactly
        these files; repeated like `write_files` when the tree moves under it.
        """
        return await self._write_repeatedly(as_, owner, name, files, message, replace=True)

    async def _write_repeatedly(
        self, as_: Identity, owner: str, name: str, files: Files, message: str, *, replace: bool
    ) -> VersionId:
        for attempt in range(WRITE_ATTEMPTS):
            try:
                return await self._write_files_once(as_, owner, name, files, message, replace)
            except Conflict:
                if attempt == WRITE_ATTEMPTS - 1:
                    raise
        raise Conflict(f"{owner}/{name} kept changing while it was written")

    async def _write_files_once(
        self, as_: Identity, owner: str, name: str, files: Files, message: str, replace: bool
    ) -> VersionId:
        existing = await self._existing(as_, owner, name)
        first = {"new_branch": DEFAULT_BRANCH} if existing is None else {}
        present = existing or {}
        operations: list[dict[str, str]] = [
            {
                "operation": "update" if path in present else "create",
                "path": path,
                "content": _encoded(content),
                **({"sha": present[path]} if path in present else {}),
            }
            for path, content in sorted(files.items())
        ]
        if replace:
            operations.extend(
                {"operation": "delete", "path": path, "sha": blob}
                for path, blob in sorted(present.items())
                if path not in files
            )
        written = await self._http.call(
            as_,
            "POST",
            f"/api/v1/repos/{segment(owner)}/{segment(name)}/contents",
            json={"branch": DEFAULT_BRANCH, **first, "message": message, "files": operations},
        )
        return VersionId(str(json_of(written)["commit"]["sha"]))

    async def commit_files(
        self,
        as_: Identity,
        owner: str,
        name: str,
        files: Mapping[str, bytes | None],
        *,
        expected: Mapping[str, ConflictToken | None],
        message: str,
    ) -> VersionId:
        """Create, update and remove the files in one commit as `as_`. Each
        update and removal carries the blob it was read at, and the host
        refuses the whole commit when any has moved since, or when a file to
        create is there already; both are `Conflict`, and neither is tried
        again.
        """
        operations: list[dict[str, str]] = []
        for path, content in sorted(files.items()):
            token = expected.get(path)
            if content is None:
                if token is None:
                    raise NotFound(f"{path} is removed only with the version it was read at")
                operations.append({"operation": "delete", "path": path, "sha": str(token)})
            elif token is None:
                operations.append(
                    {"operation": "create", "path": path, "content": _encoded(content)}
                )
            else:
                operations.append(
                    {
                        "operation": "update",
                        "path": path,
                        "content": _encoded(content),
                        "sha": str(token),
                    }
                )
        written = await self._http.call(
            as_,
            "POST",
            f"/api/v1/repos/{segment(owner)}/{segment(name)}/contents",
            json={"branch": DEFAULT_BRANCH, "message": message, "files": operations},
        )
        return VersionId(str(json_of(written)["commit"]["sha"]))

    async def file_set(self, as_: Identity, owner: str, name: str, at: str | None) -> FileSet:
        """Every file at a version, or at the head of the default branch, with
        the commit that is and each file's blob.
        """
        version = at or await self.head(as_, owner, name)
        blobs = await self._blobs(as_, owner, name, version)
        return FileSet(
            version=VersionId(version),
            tokens={path: ConflictToken(blob) for path, blob in blobs.items()},
        )

    async def read_file(
        self, as_: Identity, owner: str, name: str, path: str, *, at: str | None = None
    ) -> File:
        params = {"ref": at} if at else {}
        entry = json_of(
            await self._http.call(
                as_,
                "GET",
                f"/api/v1/repos/{segment(owner)}/{segment(name)}/contents/{file_path(path)}",
                params=params,
            )
        )
        if entry.get("type") != "file":
            raise NotFound(f"{path} is not a file")
        return File(
            path=path,
            content=base64.b64decode(entry.get("content") or ""),
            token=ConflictToken(str(entry["sha"])),
        )

    async def media_door(self, as_: Identity, owner: str, name: str, path: str, *, at: str) -> Door:
        """Where one file's bytes at a version are read whole, big files
        included, and what to present there as `as_`: the media endpoint,
        which resolves a file kept in the large-file store and answers a
        range. Nothing is called; this is the address, for the proxy to fetch
        from, with the path and the version encoded the way the request line
        carries them.
        """
        media = f"/api/v1/repos/{segment(owner)}/{segment(name)}/media/{file_path(path)}"
        return Door(
            path=f"{media}?ref={quote(at, safe='')}",
            authorization=await self._http.authorization(as_),
        )

    async def read_raw(
        self, as_: Identity, owner: str, name: str, path: str, *, at: str, max_size: int
    ) -> bytes:
        """One file's bytes at a version, big files included, through the
        media endpoint, which resolves a file kept in the large-file store.
        `Rejected` for one over `max_size` bytes, read no further.
        """
        return await self._http.read_capped(
            as_,
            f"/api/v1/repos/{segment(owner)}/{segment(name)}/media/{file_path(path)}",
            params={"ref": at},
            max_size=max_size,
        )

    async def read_blob(
        self, as_: Identity, owner: str, name: str, path: str, *, at: str, max_size: int
    ) -> bytes:
        """One file at a version as the commit holds it, through the raw
        endpoint, which gives a large-file pointer as the pointer. `Rejected`
        for one over `max_size` bytes, read no further.
        """
        return await self._http.read_capped(
            as_,
            f"/api/v1/repos/{segment(owner)}/{segment(name)}/raw/{file_path(path)}",
            params={"ref": at},
            max_size=max_size,
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
            "content": _encoded(content),
            "message": message,
            "branch": DEFAULT_BRANCH,
        }
        target = f"/api/v1/repos/{segment(owner)}/{segment(name)}/contents/{file_path(path)}"
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
            await self._http.call(
                as_,
                "GET",
                f"/api/v1/repos/{segment(owner)}/{segment(name)}/contents/{file_path(path)}",
            )
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
        commits = await self._http.get_all(
            as_, f"/api/v1/repos/{segment(owner)}/{segment(name)}/commits", **params
        )
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
                as_,
                "GET",
                f"/api/v1/repos/{segment(owner)}/{segment(name)}/branches/{DEFAULT_BRANCH}",
            )
        )
        return str(branch["commit"]["id"])

    async def versions(self, as_: Identity, owner: str, name: str) -> list[str]:
        return [str(entry["name"]) for entry in await self.tags(as_, owner, name)]

    async def require_version(self, as_: Identity, owner: str, name: str, version: str) -> None:
        """Refuse as `NotFound` a version that is not one of the repository's
        tags, as `as_` reads them. Forgejo's `ref` also takes a branch or a
        commit, which move, so a version is looked up among the tags first.
        """
        if version not in await self.versions(as_, owner, name):
            raise NotFound(f"{owner}/{name} has no version {version}")

    async def tags(self, as_: Identity, owner: str, name: str) -> list[dict[str, Any]]:
        """Every tag, with its message and the commit it points at."""
        return await self._http.get_all(as_, f"/api/v1/repos/{segment(owner)}/{segment(name)}/tags")

    async def create_version(
        self,
        as_: Identity,
        owner: str,
        name: str,
        version: str,
        target: str,
        *,
        message: str | None = None,
    ) -> None:
        """A tag at `target`; given a `message`, an annotated tag carrying it."""
        body = {"tag_name": version, "target": target}
        if message is not None:
            body["message"] = message
        await self._http.call(
            as_, "POST", f"/api/v1/repos/{segment(owner)}/{segment(name)}/tags", json=body
        )

    async def reserve_versions(self, owner: str, name: str, prefix: str) -> bool:
        """Reserve versions under `prefix` for the platform account, and say
        whether that had to be put back.
        """
        path = f"/api/v1/repos/{segment(owner)}/{segment(name)}/tag_protections"
        pattern = f"{prefix}*"
        allowed = [self._platform_account]
        for protection in list_of(await self._http.call(PLATFORM, "GET", path)):
            if protection.get("name_pattern") != pattern:
                continue
            if list(protection.get("whitelist_usernames") or []) == allowed:
                return False
            await self._http.call(
                PLATFORM,
                "PATCH",
                f"{path}/{protection['id']}",
                json={"name_pattern": pattern, "whitelist_usernames": allowed},
            )
            return True
        await self._http.call(
            PLATFORM, "POST", path, json={"name_pattern": pattern, "whitelist_usernames": allowed}
        )
        return True

    async def require_write(self, as_: Identity, owner: str, name: str) -> None:
        """Refuse as `Forbidden` unless the forge says, to `as_`'s own
        credential, that they may write the repository, for a change the
        platform then makes in their name because it needs a repository
        admin. The platform itself may.
        """
        if isinstance(as_, Platform):
            return
        record = json_of(
            await self._http.call(as_, "GET", f"/api/v1/repos/{segment(owner)}/{segment(name)}")
        )
        if not (record.get("permissions") or {}).get("push"):
            raise Forbidden(f"the caller may not change {owner}/{name}")

    async def add_collaborator(
        self, owner: str, name: str, username: str, *, permission: str
    ) -> None:
        await self._http.call(
            PLATFORM,
            "PUT",
            f"/api/v1/repos/{segment(owner)}/{segment(name)}/collaborators/{segment(username)}",
            json={"permission": permission},
        )

    async def remove_collaborator(self, owner: str, name: str, username: str) -> None:
        await self._http.call(
            PLATFORM,
            "DELETE",
            f"/api/v1/repos/{segment(owner)}/{segment(name)}/collaborators/{segment(username)}",
        )

    async def permission_of(self, owner: str, name: str, username: str) -> str | None:
        """What `username` may do at the repository, `none` when nothing, or
        none at all when the repository is not there.
        """
        try:
            record = json_of(
                await self._http.call(
                    PLATFORM,
                    "GET",
                    f"/api/v1/repos/{segment(owner)}/{segment(name)}/collaborators/{segment(username)}/permission",
                )
            )
        except NotFound:
            return None
        return str(record.get("permission"))

    async def collaborators(self, owner: str, name: str) -> list[dict[str, Any]]:
        return list_of(
            await self._http.call(
                PLATFORM, "GET", f"/api/v1/repos/{segment(owner)}/{segment(name)}/collaborators"
            )
        )

    async def set_private(self, owner: str, name: str, private: bool) -> None:
        await self._http.call(
            PLATFORM,
            "PATCH",
            f"/api/v1/repos/{segment(owner)}/{segment(name)}",
            json={"private": private},
        )

    async def mark(self, owner: str, name: str, topic: str) -> None:
        await self._http.call(
            PLATFORM,
            "PUT",
            f"/api/v1/repos/{segment(owner)}/{segment(name)}/topics/{segment(topic)}",
        )

    async def star(self, as_: Identity, owner: str, name: str) -> None:
        await self._http.call(as_, "PUT", f"/api/v1/user/starred/{segment(owner)}/{segment(name)}")

    async def marked(self, topic: str, as_: Identity = PLATFORM) -> list[dict[str, Any]]:
        """Every repository carrying the topic that `as_` may see."""
        found = json_of(
            await self._http.call(
                as_,
                "GET",
                "/api/v1/repos/search",
                params={"q": topic, "topic": "true", "limit": PAGE_LIMIT},
            )
        )
        repos: list[dict[str, Any]] = found.get("data") or []
        return repos

    async def reached_by(self, as_: Identity) -> list[dict[str, Any]]:
        """Every repository `as_` owns or reaches as a collaborator or through
        an org's team, page by page.
        """
        return await self._http.get_all(as_, "/api/v1/user/repos")

    async def owned_by(self, username: str) -> list[dict[str, Any]]:
        return await self._http.get_all(PLATFORM, f"/api/v1/users/{segment(username)}/repos")

    async def under(self, org: str, as_: Identity = PLATFORM) -> list[dict[str, Any]]:
        """Every repository in the org that `as_` may see."""
        return await self._http.get_all(as_, f"/api/v1/orgs/{segment(org)}/repos")

    async def named_with(
        self, org: str, part: str, as_: Identity = PLATFORM
    ) -> list[dict[str, Any]]:
        """Every repository the org owns whose name holds `part` and `as_` may
        see, found through the forge's search, so a listing costs what it
        finds: an org holds a repository for every contestant at every task,
        and reading them all to find a contest's few tasks grew with each one.
        The caller still checks each name, since the search matches anywhere
        in it.
        """
        owner = await self._org_id(org)
        found: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            response = await self._http.call(
                as_,
                "GET",
                SEARCH_PATH,
                params={
                    "q": part,
                    "uid": owner,
                    "exclusive": "true",
                    "limit": PAGE_SIZE,
                    "page": page,
                },
            )
            batch = json_of(response).get("data") or []
            found.extend(batch)
            if len(batch) < PAGE_SIZE:
                return found
        raise Unavailable(f"the search for {part} in {org} did not end within {MAX_PAGES} pages")

    async def _org_id(self, org: str) -> int:
        """The forge's number for the org, read once: an org is named by a key
        that is never reused, so the number never changes under its name.
        """
        known = self._org_ids.get(org)
        if known is None:
            known = int(
                json_of(await self._http.call(PLATFORM, "GET", f"/api/v1/orgs/{segment(org)}"))[
                    "id"
                ]
            )
            self._org_ids[org] = known
        return known

    async def record(self, owner: str, name: str) -> dict[str, Any]:
        return await self.seen_by(PLATFORM, owner, name)

    async def seen_by(self, as_: Identity, owner: str, name: str) -> dict[str, Any]:
        """The repository's record as `as_` reads it. The forge answers a
        private repository `as_` may not read as not there.
        """
        return json_of(
            await self._http.call(as_, "GET", f"/api/v1/repos/{segment(owner)}/{segment(name)}")
        )

    async def exists(self, owner: str, name: str) -> bool:
        try:
            await self.record(owner, name)
        except NotFound:
            return False
        return True

    async def _existing(self, as_: Identity, owner: str, name: str) -> dict[str, str] | None:
        """The blob of every file on the default branch, or none for a
        repository with no commit yet.
        """
        try:
            await self._http.call(
                as_,
                "GET",
                f"/api/v1/repos/{segment(owner)}/{segment(name)}/branches/{DEFAULT_BRANCH}",
            )
        except NotFound:
            return None
        return await self._blobs(as_, owner, name, DEFAULT_BRANCH)

    async def _blobs(self, as_: Identity, owner: str, name: str, at: str) -> dict[str, str]:
        """The blob of every file at a version, read page by page while the
        host says the listing was cut short.
        """
        blobs: dict[str, str] = {}
        for page in range(1, MAX_TREE_PAGES + 1):
            tree = json_of(
                await self._http.call(
                    as_,
                    "GET",
                    f"/api/v1/repos/{segment(owner)}/{segment(name)}/git/trees/{segment(at)}",
                    params={"recursive": "true", "per_page": TREE_PAGE, "page": page},
                )
            )
            blobs.update(
                (str(entry["path"]), str(entry["sha"]))
                for entry in tree.get("tree") or []
                if entry.get("type") == "blob"
            )
            if not tree.get("truncated"):
                return blobs
        raise Unavailable(f"the tree of {owner}/{name} did not end within {MAX_TREE_PAGES} pages")

    async def _exists(self, as_: Identity, owner: str, name: str, path: str) -> bool:
        try:
            await self._http.call(
                as_,
                "GET",
                f"/api/v1/repos/{segment(owner)}/{segment(name)}/contents/{file_path(path)}",
            )
        except NotFound:
            return False
        return True

    async def _is_org(self, owner: str) -> bool:
        try:
            await self._http.call(PLATFORM, "GET", f"/api/v1/orgs/{segment(owner)}")
        except NotFound:
            return False
        return True


def _encoded(content: bytes) -> str:
    return base64.b64encode(content).decode()


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
