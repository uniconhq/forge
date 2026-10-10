"""The forge's large-file store in memory. A test plays the browser through
`send`, handing back the address the door gave out.

It refuses what Forgejo refuses, measured on the dev stack
(`findings/upload-door-test.md`): a body that does not hash to the digest in
the address, and a body whose length is not the one in the address.

It is also **as lax as Forgejo where that matters**. A second send of an
object the store already holds is taken at whatever length its address
claims, without the body being read, which is the behaviour that makes
`holds` have to measure rather than ask whether a pair is registered; a fake
that were stricter here would hide the one mistake this store can lead the
platform into. `holds` answers on the object's real length, as the batch
endpoint does.

What it does not model is Forgejo's own permission check, which has no
meaning without repositories, nor its refusal of an address nobody was given:
the real one takes any well-formed address from anyone who may write there,
so the tests that care about who may write what drive the real thing.
"""

import base64
import hashlib
from dataclasses import dataclass, field

from forge.domain.errors import Forbidden, Rejected
from forge.domain.identity import AsUser, Identity
from forge.domain.uploads import Door, read_pointer
from forge.port.uploads import SubmissionPlace, TaskPlace, UploadPlace


def repo_of(place: UploadPlace) -> str:
    """The key a place's objects are held under."""
    return _repo_of(place)


def _repo_of(place: UploadPlace) -> str:
    match place:
        case SubmissionPlace(workspace=workspace, task=task):
            return f"{workspace}::{task}"
        case TaskPlace(task=task):
            return f"{task}"
    raise Rejected(f"no repository for {place!r}")


@dataclass(frozen=True, slots=True)
class _Address:
    repo: str
    digest: str
    size: int
    authorization: str


@dataclass
class FakeUploads:
    """The objects each repository holds, by (digest, size), with the bytes,
    so a pointer can be resolved the way Forgejo's media endpoint resolves
    one.
    """

    held: dict[str, dict[tuple[str, int], bytes]] = field(default_factory=dict)
    _addresses: dict[str, _Address] = field(default_factory=dict)

    def door(self, place: UploadPlace, *, as_: Identity, digest: str, size: int) -> Door:
        repo = _repo_of(place)
        path = f"/{repo}.git/info/lfs/objects/{digest}/{size}"
        authorization = _basic(as_)
        self._addresses[path] = _Address(repo, digest, size, authorization)
        return Door(path=path, authorization=authorization)

    async def holds(self, place: UploadPlace, *, as_: Identity, digest: str, size: int) -> bool:
        """Whether the place holds that object at that length, measured
        rather than looked up, so a pair registered at a length the object
        never had answers no.
        """
        content = self._content(_repo_of(place), digest)
        return content is not None and len(content) == size

    def _content(self, repo: str, digest: str) -> bytes | None:
        for (held, _), content in self.held.get(repo, {}).items():
            if held == digest:
                return content
        return None

    def resolve(self, repo: str, content: bytes) -> bytes:
        """What a read of `content` gives back: the object's bytes when it is
        a pointer this repository holds, and the content itself otherwise.
        Forgejo's media endpoint does exactly this (measured,
        `findings/upload-door-test.md` section 4), and a pointer to an object
        the repository does not hold reads back as the pointer.
        """
        named = read_pointer(content)
        if named is None:
            return content
        held = self._content(repo, named[0])
        return held if held is not None else content

    def send(self, path: str, authorization: str, body: bytes) -> None:
        """What the proxy does with the browser's body once the door let it
        through. `Forbidden` for an address nobody was given or a credential
        that is not the one it came with; `Rejected` for a body that is not
        what the address names, and then nothing is kept.

        An object the place already holds is linked at the length the address
        claims without the body being read at all, which is what the real one
        does and what `holds` is written not to trust.
        """
        address = self._addresses.get(path)
        if address is None or address.authorization != authorization:
            raise Forbidden("no such address")
        kept = self.held.setdefault(address.repo, {})
        if address.digest in {digest for digest, _ in kept}:
            kept[(address.digest, address.size)] = next(
                content for (digest, _), content in kept.items() if digest == address.digest
            )
            return
        if len(body) != address.size:
            raise Rejected("content size does not match")
        if hashlib.sha256(body).hexdigest() != address.digest:
            raise Rejected("content hash does not match OID")
        kept[(address.digest, address.size)] = body

    def put(self, place: UploadPlace, content: bytes) -> tuple[str, int]:
        """Put an object into a place without going through a door, for a
        test that is not about the door. Returns its digest and size.
        """
        digest, size = hashlib.sha256(content).hexdigest(), len(content)
        self.held.setdefault(_repo_of(place), {})[(digest, size)] = content
        return digest, size

    def forget(self, place: UploadPlace, digest: str, size: int) -> None:
        """Drop an object the way the forge's collector would."""
        self.held.get(_repo_of(place), {}).pop((digest, size), None)


def _basic(as_: Identity) -> str:
    match as_:
        case AsUser(credential=credential):
            return "Basic " + base64.b64encode(f":{credential.access}".encode()).decode()
    raise Forbidden("only a person uploads a file")
