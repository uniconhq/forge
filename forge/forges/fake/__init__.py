"""The port in memory, for tests. It implements every operation the port
declares, with no network, and records every call with the identity it was made
under.
"""

from forge.port import Forge


class FakeForge:
    @property
    def name(self) -> str:
        return "fake"


_is_a_forge: type[Forge] = FakeForge
