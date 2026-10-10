"""Forgejo's large-file store as the upload port: the address a browser's
bytes go to, and the check that they arrived.

Forgejo runs a git-lfs server on every repository. An object is sent with
`PUT /<owner>/<repo>.git/info/lfs/objects/<oid>/<size>`, which needs write
access to that repository, hashes the body as it streams it into the bucket,
refuses it with 422 when the hash or the length is not the one the address
names, and records that the object belongs to the repository. No earlier
call is needed; the PUT alone does all of it.

**The credential is Basic, not Bearer.** Forgejo's LFS routes read
`Bearer` only as their own internal token, so the access token the platform
holds for a person reaches them in the password half of a Basic header
instead. Forgejo's basic verifier tries the password as an OAuth2 access
token before anything else, and the user half is ignored once it does, so
the header carries no name (measured 2026-10-03,
`findings/upload-door-test.md`). An API token is refused on these routes
either way.

**`holds` asks the batch endpoint, not `verify`.** This is the one that
matters. Once a caller can reach an object at all, `verify` answers 200 for
any length they claim, and so does a second PUT, so neither proves what the
platform has to know before it writes a pointer: that this place holds
*this* object at *this* length. The batch endpoint measures the stored
object and answers an error naming its real length when the claim is wrong.
Without it, someone who had legitimately uploaded one large file could claim
it again at any size they liked, and every pointer the platform then wrote
would name a length that was not the file's (measured, sections 3 and 7).

Two more things this adapter is careful about. The address is built here and
nowhere else, from the place and the object, so no part of a browser's
request decides where its bytes land. And `holds` asks as the person,
because an object's bytes being in the store is not the same as this
repository being allowed it: the question is whether a commit here may point
at it.
"""

import base64
from typing import Any

from forge.adapters.git.forgejo.http import Http, json_of, segment
from forge.adapters.ids import MalformedId, parse_task, parse_workspace
from forge.domain.errors import Forbidden, Unavailable
from forge.domain.identity import AsUser, Identity
from forge.domain.uploads import Door
from forge.port.uploads import SubmissionPlace, TaskPlace, UploadPlace

LFS_MEDIA_TYPE = "application/vnd.git-lfs+json"


class ForgejoUploads:
    def __init__(self, http: Http) -> None:
        self._http = http

    def door(self, place: UploadPlace, *, as_: Identity, digest: str, size: int) -> Door:
        owner, name = _repo_of(place)
        return Door(
            path=f"/{segment(owner)}/{segment(name)}.git/info/lfs/objects/{digest}/{size}",
            authorization=_basic(as_),
        )

    async def holds(self, place: UploadPlace, *, as_: Identity, digest: str, size: int) -> bool:
        owner, name = _repo_of(place)
        answered = json_of(
            await self._http.request(
                "POST",
                f"/{segment(owner)}/{segment(name)}.git/info/lfs/objects/batch",
                json={
                    "operation": "download",
                    "transfers": ["basic"],
                    "objects": [{"oid": digest, "size": size}],
                },
                headers={
                    "Authorization": _basic(as_),
                    "Accept": LFS_MEDIA_TYPE,
                    "Content-Type": LFS_MEDIA_TYPE,
                },
            )
        )
        return _holds(answered, digest)


def _holds(answered: dict[str, Any], digest: str) -> bool:
    """Whether the batch answer says the object is there at the length asked
    for. One object was asked about, so one is expected back, about that
    object; an answer of any other shape is the host behaving unlike itself,
    which is not a no and must not be read as one.
    """
    objects = answered.get("objects")
    if not isinstance(objects, list) or len(objects) != 1:
        raise Unavailable("the large-file store answered about no object")
    found = objects[0]
    if not isinstance(found, dict) or found.get("oid") != digest:
        raise Unavailable("the large-file store answered about another object")
    # The object carries an error of its own for a no: 404 where the place
    # does not hold it, 422 where it holds it at another length.
    return found.get("error") is None


def _repo_of(place: UploadPlace) -> tuple[str, str]:
    """The owner and name of the repository an upload's bytes belong to."""
    match place:
        case SubmissionPlace(workspace=workspace, task=task):
            workspace_ref = parse_workspace(workspace)
            return workspace_ref.org, workspace_ref.submission_repo(parse_task(task).task)
        case TaskPlace(task=task):
            task_ref = parse_task(task)
            return task_ref.org, task_ref.repo
    raise MalformedId(f"no repository for {place!r}")


def _basic(as_: Identity) -> str:
    """The person's access token in the password half of a Basic header,
    which is the only form Forgejo's large-file routes take.
    """
    match as_:
        case AsUser(credential=credential):
            return "Basic " + base64.b64encode(f":{credential.access}".encode()).decode()
    raise Forbidden("only a person uploads a file")
