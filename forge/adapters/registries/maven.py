"""Maven Central jars, resolved by Coursier with only Central and no settings,
each hashed with SHA-256 here, since neither Coursier's report nor Maven
Central gives one."""

import asyncio
import hashlib
import json
import os
import re
from contextlib import suppress
from pathlib import Path
from tempfile import mkdtemp

from forge.domain.environment import MavenJar
from forge.domain.errors import Rejected, TooLarge, Unavailable
from forge.domain.yaml_models import size_in_words
from forge.log import get_logger

CENTRAL = ("https", "repo1.maven.org", "maven2")
"""Where every jar comes from, as the first parts of its path in Coursier's
cache."""
MISSING = re.compile(
    r"^(?:Resolution error: )?Error downloading ([\w.-]+:[\w.-]+:[\w.+-]+)\n  not found: ", re.M
)
"""A package Coursier could not find, as its failure names it."""
UNREACHED = re.compile(
    r"download error:|ConnectException|UnknownHostException|SocketTimeoutException|"
    r"SSLException|Connection reset"
)
"""What Coursier says when Maven Central could not be reached."""
WATCHED = 0.2
"""How often, in seconds, the size of Coursier's cache is checked while it runs."""

log = get_logger(__name__)


class MavenResolver:
    def __init__(self, program: str = "cs") -> None:
        self.program = program

    async def resolve(
        self, packages: list[str], root: Path, *, cap: int
    ) -> tuple[list[MavenJar], dict[str, Path]]:
        work = Path(mkdtemp(prefix="maven-", dir=root)).resolve()
        cache = work / "cache"
        report = work / "report.json"
        await self._fetch(packages, work, cache, report, cap)
        unreported = Rejected("Coursier did not report the Maven packages it resolved.")
        try:
            found = json.loads(report.read_text())["dependencies"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise unreported from error
        if not isinstance(found, list) or not all(isinstance(each, dict) for each in found):
            raise unreported
        jars: list[MavenJar] = []
        files: dict[str, Path] = {}
        names: set[str] = set()
        total = 0
        for dependency in sorted(found, key=lambda each: str(each.get("coord"))):
            if not dependency.get("file"):
                continue
            file = Path(dependency["file"]).resolve()
            parts = file.relative_to(cache).parts if file.is_relative_to(cache) else ()
            if (
                parts[:3] != CENTRAL
                or len(parts) < 5
                or not file.name.endswith(".jar")
                or not file.is_file()
            ):
                raise Rejected(f"{dependency.get('coord')} did not come from Maven Central.")
            if parts[-1] in names:
                raise Rejected(f"Two Maven packages are both named {parts[-1]}.")
            names.add(parts[-1])
            size, digest = await asyncio.to_thread(_hashed, file)
            total += size
            if total > cap:
                raise TooLarge(f"The Maven packages come to more than {size_in_words(cap)}.")
            jar = MavenJar(
                coord=str(dependency["coord"]),
                file_name=parts[-1],
                url=f"https://{'/'.join(parts[1:])}",
                size=size,
                sha256=digest,
            )
            jars.append(jar)
            kept = root / jar.sha256
            if not kept.exists():
                os.replace(file, kept)
            files[jar.sha256] = kept
        return jars, files

    async def _fetch(
        self, packages: list[str], work: Path, cache: Path, report: Path, cap: int
    ) -> None:
        # Nothing of the host's reaches Coursier: no repositories, mirrors,
        # credentials or proxies, only Maven Central and an empty cache. Every
        # file must match the SHA-1 Central gives for it, and Coursier is
        # stopped once its cache holds more than the cap.
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(work / "home")}
        env["COURSIER_CONFIG_DIR"] = str(work / "config")
        try:
            process = await asyncio.create_subprocess_exec(
                self.program,
                "fetch",
                "--no-default",
                "-r",
                "central",
                "--cache",
                str(cache),
                "--checksum",
                "SHA-1",
                "--json-output-file",
                str(report),
                *packages,
                cwd=work,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise Unavailable("Coursier could not be started to resolve Maven packages.") from error
        finished = asyncio.ensure_future(process.communicate())
        try:
            while not finished.done():
                await asyncio.wait({finished}, timeout=WATCHED)
                if not finished.done() and await asyncio.to_thread(_held, cache) > cap:
                    raise TooLarge(f"The Maven packages come to more than {size_in_words(cap)}.")
            _, said = finished.result()
        except BaseException:
            if process.returncode is None:
                process.kill()
            await asyncio.wait({finished})
            raise
        if process.returncode:
            # Coursier's words name this forge's folders, so only the log
            # holds them; the author is told which packages Central lacks.
            words = said.decode(errors="replace")
            log.warning("coursier.failed", said=words[-4000:])
            if UNREACHED.search(words):
                raise Unavailable("Maven Central could not be reached to resolve Maven packages.")
            missing = sorted(set(MISSING.findall(words)))
            raise Rejected(
                "The Maven packages could not be resolved from Maven Central."
                + (f" Not found: {', '.join(missing)}." if missing else "")
            )


def _held(folder: Path) -> int:
    """The bytes in the files under a folder, as they are being written."""
    total = 0
    for parent, _, names in os.walk(folder):
        for name in names:
            with suppress(OSError):
                total += os.lstat(os.path.join(parent, name)).st_size
    return total


def _hashed(path: Path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()
