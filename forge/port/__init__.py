"""The forge port: the one interface between this package and the git host
behind it, in the platform's own words. It is a set of areas, each a
`Protocol` in its own module, composed into one `Forge`. The areas fall into
four groups by the service behind them, each filled by an adapter of its
own under `forge.adapters`: the git host's (`GitHost`), the CI's (`Ci`), the
object store run logs are kept in (`objects`) and the mail server invite
mail goes through (`mail`).

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
from forge.port.mail import MailPort
from forge.port.objects import ObjectStore
from forge.port.orgs import OrgPort
from forge.port.primitives import PrimitivePort
from forge.port.threads import ThreadPort
from forge.port.uploads import UploadPort
from forge.port.workflows import WorkflowPort
from forge.port.workspaces import WorkspacePort

__all__ = [
    "Ci",
    "ComputePort",
    "ContentPort",
    "Forge",
    "GitHost",
    "GradingPort",
    "IdentityPort",
    "MailPort",
    "ObjectStore",
    "OrgPort",
    "PrimitivePort",
    "SignedIn",
    "ThreadPort",
    "UploadPort",
    "WorkflowPort",
    "WorkspacePort",
]


class GitHost(Protocol):
    """The git host's areas: the people, the orgs and their roles, the
    contests, tasks, workspaces and workflows and what is in them, and the
    uploads into them.
    """

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
    def uploads(self) -> UploadPort: ...

    @property
    def name(self) -> str:
        """Which git host this is, for logs."""
        ...

    async def aclose(self) -> None:
        """Release whatever the adapter holds open."""
        ...


class Ci(Protocol):
    """The CI's areas: the grading runs and the machines that run them."""

    @property
    def grading(self) -> GradingPort: ...

    @property
    def computes(self) -> ComputePort: ...

    async def aclose(self) -> None:
        """Release whatever the adapter holds open."""
        ...


@runtime_checkable
class Forge(Protocol):
    """Every service outside the platform as the platform sees it, one area
    each: the git host's, the CI's, the object store and the mail server.
    """

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
    def uploads(self) -> UploadPort: ...

    @property
    def mail(self) -> MailPort: ...

    @property
    def name(self) -> str:
        """Which implementation this is, for logs."""
        ...

    async def aclose(self) -> None:
        """Release whatever the implementation holds open."""
        ...
