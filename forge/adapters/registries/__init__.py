"""Prepare immutable per-language folders from conda-forge, PyPI, Maven Central
and npm pieces."""

import asyncio
import hashlib
import tarfile
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from compression.zstd import ZstdError, ZstdFile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from urllib.parse import urlsplit

import httpx

from forge.adapters.registries.conda import CondaRegistry
from forge.adapters.registries.maven import MavenResolver
from forge.adapters.registries.npm import NpmResolver
from forge.adapters.registries.pypi import PypiResolver
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
from forge.domain.environment.tools import COMPANIONS, SOURCES, TOOLS, VERSIONS
from forge.domain.errors import Rejected, TooLarge, Unavailable
from forge.domain.yaml_models import size_in_words
from forge.port.packages import PreparedEnvironment
from forge.port.registries import RegistryPort


class EnvironmentResolver:
    def __init__(
        self,
        cache: Path,
        *,
        registry: RegistryPort | None = None,
        maven: MavenResolver | None = None,
        npm: NpmResolver | None = None,
    ) -> None:
        self.registry = registry if registry is not None else CondaRegistry(cache)
        self.pypi = PypiResolver()
        self.maven = maven if maven is not None else MavenResolver()
        self.npm = npm if npm is not None else NpmResolver()

    @asynccontextmanager
    async def prepare(
        self,
        asked: Environment,
        *,
        cap: int,
        previous: EnvironmentLock | None = None,
        proceed: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[PreparedEnvironment]:
        platforms: dict[str, dict[str, LanguageLock]] = {platform: {} for platform in PLATFORMS}
        files: dict[str, Path] = {}
        with TemporaryDirectory(prefix="unicon-packages-") as folder:
            root = Path(folder)
            async with httpx.AsyncClient(
                timeout=60, follow_redirects=False, trust_env=False
            ) as client:
                for language, entry in asked.requirements().items():
                    if proceed is not None:
                        await proceed()
                    if previous is not None and all(
                        (old := previous.platforms.get(platform, {}).get(language)) is not None
                        and old.conda
                        and old.asked == entry
                        for platform in PLATFORMS
                    ):
                        for platform in PLATFORMS:
                            platforms[platform][language] = previous.platforms[platform][language]
                        continue
                    tool = TOOLS[language]
                    version = entry.get("version")
                    packages = cast(list[str], entry.get("packages", []))
                    conda = cast(list[str], entry.get("conda", []))
                    source = SOURCES.get(language)
                    specs = [f"{tool} {version}.*" if version else _offered(language)]
                    if companion := COMPANIONS.get(language):
                        specs.append(_offered(companion))
                    if source == "r":
                        specs.extend(f"r-{_spec(each)}" for each in packages)
                    if language in ("c", "cpp") and conda:
                        specs.append("pkg-config")
                    specs.extend(_spec(each) for each in conda)
                    solved = await self.registry.solve(
                        language, specs, None if version else datetime.now(UTC) - timedelta(days=14)
                    )
                    sizes = {
                        platform: sum({piece.sha256: piece.size for piece in records}.values())
                        for platform, records in solved.items()
                    }
                    if (largest := max(sizes.values())) > cap:
                        raise TooLarge(
                            f"The {language} toolchain and packages come to "
                            f"{size_in_words(largest)}, more than {size_in_words(cap)}."
                        )
                    for records in solved.values():
                        for piece in records:
                            if piece.sha256 not in files:
                                if proceed is not None:
                                    await proceed()
                                path = root / piece.sha256
                                await _download(client, piece, path)
                                await asyncio.to_thread(_check_scripts, path, piece)
                                files[piece.sha256] = path
                    jars: list[MavenJar] = []
                    trees: dict[str, list[NpmPackage]] = {platform: [] for platform in PLATFORMS}
                    if packages and source == "maven":
                        if proceed is not None:
                            await proceed()
                        jars, downloaded = await self.maven.resolve(
                            packages, root, cap=cap - largest
                        )
                        files.update(downloaded)
                    elif packages and source == "npm":
                        held = {
                            platform: await asyncio.to_thread(_node_places, records, files)
                            for platform, records in solved.items()
                        }
                        trees, downloaded = await self.npm.resolve(
                            packages,
                            root,
                            client,
                            cap=cap - largest,
                            proceed=proceed,
                            held=held,
                        )
                        files.update(downloaded)
                    for platform, records in solved.items():
                        total = (
                            sizes[platform]
                            + sum(jar.size for jar in jars)
                            + sum({each.sha256: each.size for each in trees[platform]}.values())
                        )
                        if total > cap:
                            raise TooLarge(
                                f"The {language} toolchain and packages come to "
                                f"{size_in_words(total)}, more than {size_in_words(cap)}."
                            )
                        picked = next(piece.version for piece in records if piece.name == tool)
                        wheels: list[Wheel] = []
                        if packages and source == "pypi":
                            wheels, downloaded = await self.pypi.resolve(
                                packages,
                                picked,
                                platform,
                                root,
                                client,
                                cap=cap - total,
                            )
                            files.update(downloaded)
                        platforms[platform][language] = LanguageLock(
                            asked=entry,
                            picked=f"{tool} {picked}",
                            conda=records,
                            pypi=wheels,
                            maven=jars,
                            npm=trees[platform],
                            fingerprint=pieces_fingerprint(records, wheels, jars, trees[platform]),
                        )
            yield PreparedEnvironment(EnvironmentLock(platforms=platforms), files)


def _spec(entry: str) -> str:
    """A task's `name=version` entry as the conda spec of that version prefix."""
    return entry.replace("=", " ") + (".*" if "=" in entry else "")


def _offered(language: str) -> str:
    """The conda spec of the language's toolchain at any version it offers."""
    return f"{TOOLS[language]} {'|'.join(f'{each}.*' for each in VERSIONS[language])}"


async def _download(client: httpx.AsyncClient, piece: CondaPiece, path: Path) -> None:
    url = urlsplit(piece.url)
    if (
        url.scheme != "https"
        or url.netloc != "conda.anaconda.org"
        or not url.path.startswith("/conda-forge/")
        or url.query
        or url.fragment
    ):
        raise Rejected(f"{piece.name} did not come from conda-forge.")
    digest, size = hashlib.sha256(), 0
    try:
        async with client.stream("GET", piece.url) as response:
            response.raise_for_status()
            with path.open("wb") as output:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > piece.size:
                        raise Rejected(f"{piece.name} was larger than its locked size.")
                    digest.update(chunk)
                    output.write(chunk)
    except httpx.HTTPError as error:
        raise Unavailable(f"Could not download {piece.name} from conda-forge.") from error
    if size != piece.size or digest.hexdigest() != piece.sha256:
        raise Rejected(f"{piece.name} did not match its locked size and SHA-256.")


def _members(path: Path, piece: CondaPiece) -> Iterator[str]:
    if piece.file_name.endswith(".conda"):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith(".tar.zst"):
                    with (
                        archive.open(name) as compressed,
                        ZstdFile(compressed) as stream,
                        tarfile.open(fileobj=stream, mode="r|") as content,
                    ):
                        for member in content:
                            yield member.name
    elif piece.file_name.endswith(".tar.bz2"):
        with tarfile.open(path) as archive:
            for member in archive:
                yield member.name
    else:
        raise Rejected(f"{piece.name} has an unsupported conda archive format.")


def _node_places(records: list[CondaPiece], files: dict[str, Path]) -> dict[str, str]:
    """Each place under `lib/` that the toolchain's conda packages install an
    npm package at, as npm's lock names a place (`node_modules/npm`), with
    the conda package that installs it."""
    places: dict[str, str] = {}
    for piece in records:
        try:
            for name in _members(files[piece.sha256], piece):
                parts = name.split("/")
                if parts[:2] != ["lib", "node_modules"] or len(parts) < 4:
                    continue
                end = 4 if parts[2].startswith("@") else 3
                if len(parts) > end:
                    places.setdefault("/".join(parts[1:end]), piece.name)
        except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile, ZstdError) as error:
            raise Rejected(f"{piece.name} could not be inspected as a conda package.") from error
    return places


def _check_scripts(path: Path, piece: CondaPiece) -> None:
    try:
        for name in _members(path, piece):
            if name.endswith(
                (
                    "-pre-link.sh",
                    "-post-link.sh",
                    "-pre-unlink.sh",
                    "-pre-link.bat",
                    "-post-link.bat",
                    "-pre-unlink.bat",
                )
            ):
                raise Rejected(
                    f"{piece.name} carries a link script and cannot be installed "
                    "with scripts disabled."
                )
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile, ZstdError) as error:
        raise Rejected(f"{piece.name} could not be inspected as a conda package.") from error
