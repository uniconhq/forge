"""The forge port: the one interface between this package and whatever git
host sits behind it, written in Unicon's words rather than any one host's.
The operations are declared here as a `Protocol`; `forges.forgejo` and
`forges.fake` implement it.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Forge(Protocol):
    """A git host as this package sees it. Operations are added as the
    services that need them are written.
    """

    @property
    def name(self) -> str:
        """Which implementation this is, for logs: `forgejo` or `fake`."""
        ...
