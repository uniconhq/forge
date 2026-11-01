"""Deterministic language locks without registry calls or runnable toolchains."""

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from forge.domain.environment import (
    PLATFORMS,
    CondaPiece,
    Environment,
    EnvironmentLock,
    LanguageLock,
    fingerprint,
)
from forge.domain.environment.tools import TOOLS
from forge.port.packages import PreparedEnvironment


class FakePackages:
    def __init__(self) -> None:
        self.calls: list[Environment] = []

    @asynccontextmanager
    async def prepare(
        self,
        asked: Environment,
        *,
        cap: int,
        previous: EnvironmentLock | None = None,
        proceed: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[PreparedEnvironment]:
        self.calls.append(asked)
        platforms: dict[str, dict[str, LanguageLock]] = {platform: {} for platform in PLATFORMS}
        files: dict[str, Path] = {}
        with TemporaryDirectory(prefix="unicon-test-packages-") as folder:
            for language, entry in asked.requirements().items():
                if proceed is not None:
                    await proceed()
                for platform in PLATFORMS:
                    old = previous.platforms.get(platform, {}).get(language) if previous else None
                    if old and old.conda and old.asked == entry:
                        platforms[platform][language] = old
                        continue
                    version = str(
                        entry.get(
                            "version",
                            {"c": "16.2.0", "cpp": "16.2.0", "java": "25.0.2", "python": "3.14"}[
                                language
                            ],
                        )
                    )
                    body = json.dumps([platform, language, entry], sort_keys=True).encode()
                    digest = hashlib.sha256(body).hexdigest()
                    path = Path(folder) / digest
                    path.write_bytes(body)
                    files[digest] = path
                    piece = CondaPiece(
                        name=TOOLS[language],
                        version=version,
                        build="test",
                        subdir=platform,
                        file_name=f"{TOOLS[language]}-{version}-test.conda",
                        url=f"https://conda.anaconda.org/conda-forge/{platform}/test.conda",
                        sha256=digest,
                        size=len(body),
                        record={},
                    )
                    platforms[platform][language] = LanguageLock(
                        asked=entry,
                        picked=f"{TOOLS[language]} {version}",
                        fingerprint=fingerprint([digest]),
                        conda=[piece],
                    )
            yield PreparedEnvironment(EnvironmentLock(platforms=platforms), files)
