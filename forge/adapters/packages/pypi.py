"""Wheel-only uv resolution for both grading platforms; no project configuration."""

import asyncio
import hashlib
import os
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

import httpx
import uv

from forge.domain.environment import Environment, EnvironmentLock, LanguageLock, Wheel, fingerprint
from forge.domain.errors import Rejected, TooLarge, Unavailable
from forge.port.packages import PreparedEnvironment

TARGETS = {"linux-64": "x86_64-manylinux_2_28", "linux-aarch64": "aarch64-manylinux_2_28"}


class PypiResolver:
    @asynccontextmanager
    async def prepare(self, asked: Environment, *, cap: int) -> AsyncIterator[PreparedEnvironment]:
        with TemporaryDirectory(prefix="unicon-packages-") as folder:
            root = Path(folder)
            requirements = root / "requirements.in"
            packages = asked.python.packages if asked.python is not None else []
            requirements.write_text("\n".join(packages) + "\n")
            platforms: dict[str, dict[str, LanguageLock]] = {}
            files: dict[str, Path] = {}
            total = 0
            async with httpx.AsyncClient(
                timeout=60, follow_redirects=False, trust_env=False
            ) as client:
                for platform, target in TARGETS.items():
                    output = root / f"pylock.{platform}.toml"
                    await _compile(requirements, output, target)
                    resolved = tomllib.loads(output.read_text())
                    wheels: list[Wheel] = []
                    for package in resolved.get("packages", []):
                        candidates = package.get("wheels", [])
                        if not candidates:
                            raise Rejected(
                                f"{package['name']} has no Python 3.14 wheel for {platform}."
                            )
                        piece = candidates[0]
                        url = urlsplit(piece["url"])
                        if (
                            url.scheme != "https"
                            or url.hostname != "files.pythonhosted.org"
                            or url.username
                            or url.password
                        ):
                            raise Rejected("The package did not come from PyPI's wheel store.")
                        name = unquote(url.path.rsplit("/", 1)[-1])
                        if not name.endswith(".whl") or "/" in name or "\\" in name:
                            raise Rejected("The package registry did not name a wheel.")
                        digest = piece["hashes"]["sha256"]
                        wheel = Wheel(
                            name=package["name"],
                            version=package["version"],
                            file_name=name,
                            sha256=digest,
                            size=piece["size"],
                        )
                        wheels.append(wheel)
                        if digest in files:
                            continue
                        total += wheel.size
                        if total > cap:
                            raise TooLarge(
                                f"The packages total {total} bytes, more than the {cap} byte limit."
                            )
                        path = root / digest
                        await _download(client, piece["url"], path, wheel)
                        files[digest] = path
                    platforms[platform] = {
                        "python": LanguageLock(
                            asked={"packages": packages},
                            picked="python 3.14",
                            fingerprint=fingerprint([each.sha256 for each in wheels]),
                            pypi=wheels,
                        )
                    }
            yield PreparedEnvironment(EnvironmentLock(platforms=platforms), files)


async def _compile(requirements: Path, output: Path, target: str) -> None:
    # Configuration and environment variables cannot add a registry or a source.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("UV_", "PIP_"))
        and key not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    }
    process = await asyncio.create_subprocess_exec(
        uv.find_uv_bin(),
        "--no-config",
        "pip",
        "compile",
        str(requirements),
        "--only-binary",
        ":all:",
        "--generate-hashes",
        "--python-version",
        "3.14",
        "--python-platform",
        target,
        "--index-url",
        "https://pypi.org/simple",
        "--format",
        "pylock.toml",
        "--output-file",
        str(output),
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, error = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise Rejected(
            "The Python packages could not be prepared: " + error.decode(errors="replace")[-4000:]
        )


async def _download(client: httpx.AsyncClient, url: str, path: Path, wheel: Wheel) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with path.open("wb") as output:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > wheel.size:
                        raise Rejected(f"{wheel.file_name} was larger than its lock says.")
                    digest.update(chunk)
                    output.write(chunk)
    except httpx.HTTPError as error:
        raise Unavailable(f"Could not download {wheel.file_name} from PyPI.") from error
    if size != wheel.size or digest.hexdigest() != wheel.sha256:
        raise Rejected(f"{wheel.file_name} did not match its locked size and SHA-256.")
