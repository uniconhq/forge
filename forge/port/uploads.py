"""A file's bytes going into the large-file store of the place that will hold
them, without passing through the platform.

The platform never carries the bytes and never signs for them. It says where
one upload's bytes go and what to present there (`door`), and the proxy puts
the browser's body through that address; the host checks that the person may
write to the place, hashes the body as it stores it, refuses anything that is
not the size and digest the address names, and records that the object
belongs to the place. Afterwards the platform asks whether the place holds
the object (`holds`), and a change to the place carries a pointer to it.

Any host behind this port must therefore offer: an address a client sends one
object's bytes to, which names the object by digest and length and is refused
unless the caller may write to the place; a check that the stored bytes are
the ones named, made by the host and not by the client; and a way to ask
whether a place holds an object.
"""

from dataclasses import dataclass
from typing import Protocol

from forge.domain.identity import Identity
from forge.domain.ids import TaskId, WorkspaceId
from forge.domain.uploads import Door


@dataclass(frozen=True, slots=True)
class SubmissionPlace:
    """Where one contestant's submissions to one task are kept."""

    workspace: WorkspaceId
    task: TaskId


@dataclass(frozen=True, slots=True)
class TaskPlace:
    """A task itself, which its organisers write."""

    task: TaskId


UploadPlace = SubmissionPlace | TaskPlace


class UploadPort(Protocol):
    def door(self, place: UploadPlace, *, as_: Identity, digest: str, size: int) -> Door:
        """Where the bytes of one object go in the place's large-file store,
        and what to present there as `as_`. Nothing is called and nothing is
        written: this is the address, worked out from the place and the
        object, for the proxy to send a body to.
        """
        ...

    async def holds(self, place: UploadPlace, *, as_: Identity, digest: str, size: int) -> bool:
        """Whether the place's large-file store holds that object, asked as
        `as_`. False when it does not; `NotFound` when there is no such
        place.
        """
        ...
