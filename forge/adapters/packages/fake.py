"""Deterministic preparation for service tests, with no registry calls."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from forge.domain.environment import (
    PLATFORMS,
    Environment,
    EnvironmentLock,
    LanguageLock,
    fingerprint,
)
from forge.port.packages import PreparedEnvironment


class FakePackages:
    def __init__(self) -> None:
        self.calls: list[Environment] = []

    @asynccontextmanager
    async def prepare(self, asked: Environment, *, cap: int) -> AsyncIterator[PreparedEnvironment]:
        self.calls.append(asked)
        yield PreparedEnvironment(
            EnvironmentLock(
                platforms={
                    platform: {
                        "python": LanguageLock(
                            asked=asked.python.model_dump() if asked.python else {},
                            picked="python 3.14",
                            fingerprint=fingerprint([]),
                        )
                    }
                    for platform in PLATFORMS
                }
            ),
            {},
        )
