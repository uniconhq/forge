"""Deterministic language locks without registry calls or runnable toolchains."""

import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from forge.domain.environment import (
    PLATFORMS,
    CondaPiece,
    Environment,
    EnvironmentLock,
    LanguageLock,
    MavenJar,
    NpmPackage,
    Wheel,
    pieces_fingerprint,
)
from forge.domain.environment.tools import SOURCES, TOOLS
from forge.port.packages import PreparedEnvironment

NEWEST = {
    "c": "16.2.0",
    "cpp": "16.2.0",
    "java": "25.0.2",
    "python": "3.15.2",
    "javascript": "26.13.0",
    "typescript": "6.0.2",
    "kotlin": "2.4.20",
    "csharp": "10.0.401",
    "rust": "1.99.1",
    "r": "4.6.2",
    "ruby": "4.0.9",
}
"""The version each language's toolchain gives when a task names none."""


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
                    version = str(entry.get("version", NEWEST[language]))
                    digest, size = _kept(folder, files, platform, language, entry)
                    piece = CondaPiece(
                        name=TOOLS[language],
                        version=version,
                        build="test",
                        subdir=platform,
                        file_name=f"{TOOLS[language]}-{version}-test.conda",
                        url=f"https://conda.anaconda.org/conda-forge/{platform}/test.conda",
                        sha256=digest,
                        size=size,
                        record={},
                    )
                    conda: list[CondaPiece] = [piece]
                    wheels: list[Wheel] = []
                    jars: list[MavenJar] = []
                    tree: list[NpmPackage] = []
                    for each in cast(list[str], entry.get("packages", [])):
                        digest, size = _kept(folder, files, platform, language, each)
                        name = (
                            each.split(":")[1]
                            if ":" in each
                            else re.split(r"(?<!^)[@=<>!~\[ ;]", each)[0]
                        )
                        source = SOURCES[language]
                        if source == "r":
                            conda.append(
                                piece.model_copy(
                                    update={"name": f"r-{name}", "sha256": digest, "size": size}
                                )
                            )
                        elif source == "pypi":
                            wheels.append(
                                Wheel(
                                    name=name,
                                    version="1",
                                    file_name=f"{name}-1-py3-none-any.whl",
                                    sha256=digest,
                                    size=size,
                                )
                            )
                        elif source == "maven":
                            jars.append(
                                MavenJar(
                                    coord=each,
                                    file_name=f"{name}-1.jar",
                                    url=f"https://repo1.maven.org/maven2/{name}-1.jar",
                                    sha256=digest,
                                    size=size,
                                )
                            )
                        else:
                            tree.append(
                                NpmPackage(
                                    path=f"node_modules/{name}",
                                    name=name,
                                    version="1.0.0",
                                    url=f"https://registry.npmjs.org/{name}/-/{name}-1.0.0.tgz",
                                    sha256=digest,
                                    size=size,
                                )
                            )
                    platforms[platform][language] = LanguageLock(
                        asked=entry,
                        picked=f"{TOOLS[language]} {version}",
                        fingerprint=pieces_fingerprint(conda, wheels, jars, tree),
                        conda=conda,
                        pypi=wheels,
                        maven=jars,
                        npm=tree,
                    )
            yield PreparedEnvironment(EnvironmentLock(platforms=platforms), files)


def _kept(folder: str, files: dict[str, Path], *parts: object) -> tuple[str, int]:
    """A piece's stand-in, the JSON of what it stands for, kept by its SHA-256."""
    body = json.dumps(parts, sort_keys=True).encode()
    digest = hashlib.sha256(body).hexdigest()
    path = Path(folder) / digest
    path.write_bytes(body)
    files[digest] = path
    return digest, len(body)
