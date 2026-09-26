"""The port over Forgejo and Woodpecker. May import `forge.port` and
`forge.domain`, never `forge.services` or `forge.db`.
"""

from forge.port import Forge


class ForgejoForge:
    """The real implementation. Operations land with the services that call
    them.
    """

    @property
    def name(self) -> str:
        return "forgejo"


_is_a_forge: type[Forge] = ForgejoForge
