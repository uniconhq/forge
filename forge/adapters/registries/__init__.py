"""Prepare immutable per-language folders from conda-forge and PyPI pieces."""

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
from urllib.parse import urlsplit

import httpx

from forge.adapters.registries.conda import CondaRegistry
from forge.adapters.registries.pypi import PypiResolver
from forge.domain.environment import (
    PLATFORMS,
    CondaPiece,
    Environment,
    EnvironmentLock,
    LanguageLock,
    Wheel,
    fingerprint,
)
from forge.domain.environment.tools import TOOLS
from forge.domain.errors import Rejected, TooLarge, Unavailable
from forge.port.packages import PreparedEnvironment
from forge.port.registries import RegistryPort


class EnvironmentResolver:
    def __init__(self, cache: Path, *, registry: RegistryPort | None = None) -> None:
        self.registry = registry if registry is not None else CondaRegistry(cache)
        self.pypi = PypiResolver()

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
                    specs = [f"{tool} {version}.*" if version else tool]
                    specs.extend(
                        spec.replace("=", " ") + (".*" if "=" in spec else "")
                        for spec in getattr(asked, language).conda
                    )
                    solved = await self.registry.solve(
                        language, specs, None if version else datetime.now(UTC) - timedelta(days=14)
                    )
                    for platform, records in solved.items():
                        total = sum({piece.sha256: piece.size for piece in records}.values())
                        if total > cap:
                            raise TooLarge(
                                f"The {language} toolchain and packages total {total} bytes, "
                                f"more than {cap}."
                            )
                        for piece in records:
                            if piece.sha256 not in files:
                                if proceed is not None:
                                    await proceed()
                                path = root / piece.sha256
                                await _download(client, piece, path)
                                await asyncio.to_thread(_check_scripts, path, piece)
                                files[piece.sha256] = path
                        picked = next(piece.version for piece in records if piece.name == tool)
                        wheels: list[Wheel] = []
                        if (
                            language == "python"
                            and asked.python is not None
                            and asked.python.packages
                        ):
                            wheels, downloaded = await self.pypi.resolve(
                                asked.python.packages,
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
                            fingerprint=fingerprint(
                                [piece.sha256 for piece in records]
                                + [wheel.sha256 for wheel in wheels]
                            ),
                        )
            yield PreparedEnvironment(EnvironmentLock(platforms=platforms), files)


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
