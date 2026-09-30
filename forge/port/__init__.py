"""The forge port: the one interface between this package and the git host
behind it, in the platform's own words. It is a set of areas, each a
`Protocol` in its own module, composed into one `Forge`. `forges.forgejo` and
`forges.fake` implement every area. The object store the platform keeps
uploads and logs in travels with the forge as its `objects` area, since the
deployment that runs the forge runs the store beside it.

Two rules hold the port together. Every reference the package stores is an
opaque id the port hands out, and nothing reads inside one. Every failure is
one of five typed errors: `NotFound`, `Forbidden`, `Conflict`, `Rejected` and
`Unavailable`; retries live inside the implementation, so a host that is busy
reaches the package only as `Unavailable` once they are used up. Operations
done for a person take the identity the call is made under, so the host
records the change as theirs and enforces their permissions underneath the
platform's own.
"""

from typing import Protocol, runtime_checkable

from forge.port.computes import ComputePort
from forge.port.content import ContentPort
from forge.port.grading import GradingPort
from forge.port.identity import IdentityPort, SignedIn
from forge.port.objects import ObjectStore
from forge.port.orgs import OrgPort
from forge.port.primitives import PrimitivePort
from forge.port.threads import ThreadPort
from forge.port.workflows import WorkflowPort
from forge.port.workspaces import WorkspacePort

__all__ = [
    "ComputePort",
    "ContentPort",
    "Forge",
    "GradingPort",
    "IdentityPort",
    "ObjectStore",
    "OrgPort",
    "PrimitivePort",
    "SignedIn",
    "ThreadPort",
    "WorkflowPort",
    "WorkspacePort",
]


@runtime_checkable
class Forge(Protocol):
    """A git host and its CI as the platform sees them, one area each."""

    @property
    def identity(self) -> IdentityPort: ...

    @property
    def orgs(self) -> OrgPort: ...

    @property
    def content(self) -> ContentPort: ...

    @property
    def workspaces(self) -> WorkspacePort: ...

    @property
    def threads(self) -> ThreadPort: ...

    @property
    def workflows(self) -> WorkflowPort: ...

    @property
    def primitives(self) -> PrimitivePort: ...

    @property
    def grading(self) -> GradingPort: ...

    @property
    def computes(self) -> ComputePort: ...

    @property
    def objects(self) -> ObjectStore: ...

    @property
    def name(self) -> str:
        """Which implementation this is, for logs."""
        ...

    async def aclose(self) -> None:
        """Release whatever the implementation holds open."""
        ...
