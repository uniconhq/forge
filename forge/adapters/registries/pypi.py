"""Wheel-only uv resolution for both grading platforms; no project configuration."""

import asyncio
import hashlib
import os
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import uv

from forge.domain.environment import Wheel
from forge.domain.errors import Rejected, TooLarge, Unavailable
from forge.domain.yaml_models import size_in_words

TARGETS = {"linux-64": "x86_64-manylinux_2_28", "linux-aarch64": "aarch64-manylinux_2_28"}


class PypiResolver:
    async def resolve(
        self,
        packages: list[str],
        python_version: str,
        platform: str,
        root: Path,
        client: httpx.AsyncClient,
        *,
        cap: int,
    ) -> tuple[list[Wheel], dict[str, Path]]:
        requirements = root / "requirements.in"
        requirements.write_text("\n".join(packages) + "\n")
        output = root / f"pylock.{platform}.toml"
        await _compile(requirements, output, TARGETS[platform], python_version)
        resolved = tomllib.loads(output.read_text())
        wheels: list[Wheel] = []
        files: dict[str, Path] = {}
        total = 0
        for package in resolved.get("packages", []):
            candidates = package.get("wheels", [])
            if not candidates:
                raise Rejected(
                    f"{package['name']} has no ready-built wheel for Python "
                    f"{python_version} on {platform}."
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
            wheel = Wheel(
                name=package["name"],
                version=package["version"],
                file_name=name,
                sha256=piece["hashes"]["sha256"],
                size=piece["size"],
            )
            total += wheel.size
            if total > cap:
                raise TooLarge(f"The Python packages come to more than {size_in_words(cap)}.")
            wheels.append(wheel)
            path = root / wheel.sha256
            if not path.exists():
                await _download(client, piece["url"], path, wheel)
            files[wheel.sha256] = path
        return wheels, files


async def _compile(requirements: Path, output: Path, target: str, python_version: str) -> None:
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
        python_version,
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
            f"The Python packages have no ready-built version for Python {python_version}: "
            + error.decode(errors="replace")[-4000:]
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
