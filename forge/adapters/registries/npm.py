"""npm packages, resolved by npm itself into a lock with every script off and
only the public registry reachable, each tarball downloaded and hashed with SHA-256
here, since the lock gives only sha512. A package with an install script is
kept like any other, and its script is never run; of the platform-only
optional packages, each platform keeps only its own."""

import asyncio
import base64
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import mkdtemp
from urllib.parse import urlsplit

import httpx

from forge.domain.environment import NPM_ENTRY, NPM_NAME, PLATFORMS, NpmPackage
from forge.domain.errors import Rejected, TooLarge, Unavailable
from forge.log import get_logger

REGISTRY = "https://registry.npmjs.org/"
TARGETS = {
    "linux-64": {"os": "linux", "cpu": "x64", "libc": "glibc"},
    "linux-aarch64": {"os": "linux", "cpu": "arm64", "libc": "glibc"},
}
"""Each platform as npm names it in a package's `os`, `cpu` and `libc`."""
TREE = re.compile(rf"node_modules/({NPM_NAME})")
ALLOWED = b"registry.npmjs.org:443"
"""The one host and port npm's connections may reach."""
OLDEST = (11, 10)
"""The first npm that can refuse git dependencies, with `--allow-git=none`."""
FAILURES = {
    "E404": "a package is not on the npm registry",
    "ETARGET": "no version of a package matches the one asked for",
    "ERESOLVE": "the packages ask for versions of each other that conflict",
    "EALLOWGIT": "a package depends on a git repository, which is not fetched",
    "E403": "a package depends on an archive somewhere other than the npm registry",
}
"""What npm's error codes mean, in words for the task's author; npm's own
words, which may name hosts it tried, are only logged."""
UNREACHED = {"ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "EAI_AGAIN", "ENOTFOUND"}
"""npm's error codes for a registry it could not reach."""

log = get_logger(__name__)


class NpmResolver:
    def __init__(self, program: str = "npm") -> None:
        self.program = program

    async def resolve(
        self,
        packages: list[str],
        root: Path,
        client: httpx.AsyncClient,
        *,
        cap: int,
        proceed: Callable[[], Awaitable[None]] | None = None,
        held: Mapping[str, Mapping[str, str]] | None = None,
    ) -> tuple[dict[str, list[NpmPackage]], dict[str, Path]]:
        """Each platform's tree of the npm packages `packages` asks for, and
        their tarballs by SHA-256. `held` names, for each platform, the
        places in the tree the toolchain's conda packages already install,
        with the conda package that does: a peer placed there is the
        toolchain's own and left out, and any other package placed there is
        refused, since it would install over the toolchain's files."""
        work = Path(mkdtemp(prefix="npm-", dir=root))
        dependencies = {}
        for each in packages:
            found = NPM_ENTRY.fullmatch(each)
            assert found is not None
            dependencies[found[1]] = found[2] or "*"
        (work / "package.json").write_text(
            json.dumps({"name": "unicon-packages", "private": True, "dependencies": dependencies})
        )
        if proceed is not None:
            await proceed()
        await self._lock(work)
        unreadable = Rejected("npm did not write a lock of the packages it resolved.")
        try:
            lock = json.loads((work / "package-lock.json").read_text())
        except (OSError, ValueError) as error:
            raise unreadable from error
        listed = lock.get("packages") if isinstance(lock, dict) else None
        if not isinstance(listed, dict) or lock.get("lockfileVersion") != 3:
            raise unreadable
        # Every entry is checked before anything is downloaded, so a lock
        # with one package from elsewhere fetches nothing at all.
        kept: list[tuple[str, str, str, str, str, list[str]]] = []
        for path, entry in sorted(listed.items()):
            if path == "":
                continue
            if not isinstance(entry, dict):
                raise unreadable
            if entry.get("inBundle"):
                # Its files are inside the package that bundles it.
                continue
            name = _placed(path)
            if name is None:
                raise Rejected(f"npm placed a package at {path}, outside node_modules.")
            if entry.get("link"):
                raise Rejected(f"npm linked {name} to a folder, not a package from the registry.")
            version, url, integrity = (
                entry.get("version"),
                entry.get("resolved"),
                entry.get("integrity"),
            )
            if not (
                isinstance(version, str) and isinstance(url, str) and isinstance(integrity, str)
            ):
                raise Rejected(f"npm did not lock {name} to one version and archive.")
            expected = _checked(name, url, integrity)
            for_platforms = [platform for platform in PLATFORMS if _runs_on(entry, platform)]
            if len(for_platforms) < len(PLATFORMS) and not (
                entry.get("optional") or entry.get("devOptional")
            ):
                raise Rejected(f"{name} {version} does not run on both Linux platforms.")
            for platform in list(for_platforms):
                holder = _holder(path, (held or {}).get(platform, {}))
                if holder is None:
                    continue
                if not entry.get("peer"):
                    raise Rejected(
                        f"npm placed {name} {version} at {path}, where the toolchain's "
                        f"{holder} already installs its own. Leave {name} out of the "
                        "packages, or ask for ones that do not need another copy of it."
                    )
                for_platforms.remove(platform)
            if for_platforms:
                named = str(entry.get("name", name))
                kept.append((path, named, version, url, expected, for_platforms))
        trees: dict[str, list[NpmPackage]] = {platform: [] for platform in PLATFORMS}
        files: dict[str, Path] = {}
        fetched: dict[str, tuple[str, int]] = {}
        total = 0
        for path, name, version, url, expected, for_platforms in kept:
            if url not in fetched:
                if proceed is not None:
                    await proceed()
                digest, size = await _download(client, url, expected, root, name, cap, total)
                total += size
                fetched[url] = (digest, size)
                files[digest] = root / digest
            digest, size = fetched[url]
            package = NpmPackage(
                path=path, name=name, version=version, url=url, size=size, sha256=digest
            )
            for platform in for_platforms:
                trees[platform].append(package)
        return trees, files

    async def _lock(self, work: Path) -> None:
        # npm reads only the configuration written here, and the environment
        # gives it nothing of the forge's own: no registry, credentials,
        # proxy or secret. It runs no script and fetches no git repository,
        # and its connections go through a proxy that reaches only the
        # registry, so a dependency named by a URL or a file fails.
        home = work / "home"
        home.mkdir()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}
        for each in ("user", "global"):
            (work / f"{each}.npmrc").write_text("")
        code, said = await self._run(env, work, "--version")
        found = re.fullmatch(r"(\d+)\.(\d+)\.\d+\s*", said)
        if code or found is None or (int(found[1]), int(found[2])) < OLDEST:
            raise Unavailable("npm 11.10 or later is needed to resolve npm packages.")
        async with _confined() as proxy:
            code, said = await self._run(
                env,
                work,
                "install",
                "--package-lock-only",
                "--ignore-scripts",
                "--allow-git=none",
                "--no-audit",
                "--no-fund",
                "--no-update-notifier",
                f"--registry={REGISTRY}",
                f"--https-proxy={proxy}",
                f"--proxy={proxy}",
                "--noproxy=",
                f"--userconfig={work / 'user.npmrc'}",
                f"--globalconfig={work / 'global.npmrc'}",
                f"--cache={work / 'cache'}",
            )
        if code:
            log.warning("npm.failed", said=said[-4000:])
            found = re.search(r"npm error code (\w+)", said)
            named = found[1] if found else "unknown"
            if named in UNREACHED:
                raise Unavailable(
                    f"The npm registry could not be reached to resolve npm packages "
                    f"(npm error {named})."
                )
            raise Rejected(
                f"The npm packages could not be resolved: {FAILURES.get(named, 'npm failed')} "
                f"(npm error {named})."
            )

    async def _run(self, env: dict[str, str], work: Path, *args: str) -> tuple[int, str]:
        """npm's exit code and what it said, on standard output for its
        version and on standard error otherwise."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.program,
                *args,
                cwd=work,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise Unavailable("npm could not be started to resolve npm packages.") from error
        try:
            out, err = await process.communicate()
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        said = out if args == ("--version",) else err
        return process.returncode or 0, said.decode(errors="replace")


@asynccontextmanager
async def _confined() -> AsyncIterator[str]:
    """A proxy on the loopback, for npm alone, that tunnels to the registry's
    HTTPS port and refuses every other request without connecting anywhere.
    It closes the connections it still holds itself when it stops, since
    uvloop's server, the one the API runs on, has no `close_clients`."""
    held: set[asyncio.StreamWriter] = set()

    async def tunnel(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        held.add(writer)
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            if head.split(b"\r\n", 1)[0].split() != [b"CONNECT", ALLOWED, b"HTTP/1.1"]:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            host, port = ALLOWED.decode().split(":")
            upstream, to_upstream = await asyncio.open_connection(host, int(port))
            held.add(to_upstream)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await asyncio.gather(_pipe(reader, to_upstream), _pipe(upstream, writer))
        except OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(tunnel, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        for writer in held:
            writer.close()
        await server.wait_closed()


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while chunk := await source.read(64 * 1024):
            sink.write(chunk)
            await sink.drain()
    finally:
        sink.close()


def _placed(path: str) -> str | None:
    """The name of the package npm places at `path`, one `node_modules/<name>`
    below another, or None for any other path."""
    parts = path.split("/node_modules/")
    if not parts[0].startswith("node_modules/"):
        return None
    parts[0] = parts[0].removeprefix("node_modules/")
    for part in parts:
        if not TREE.fullmatch(f"node_modules/{part}") or any(
            segment in (".", "..") for segment in part.split("/")
        ):
            return None
    return parts[-1]


def _holder(path: str, held: Mapping[str, str]) -> str | None:
    """The conda package that already installs `path`, a place in the tree,
    or a place it is inside of; None when none does."""
    for place, piece in held.items():
        if path == place or path.startswith(f"{place}/"):
            return piece
    return None


def _runs_on(entry: dict[str, object], platform: str) -> bool:
    """Whether a package's `os`, `cpu` and `libc` admit the platform, as npm's
    own check reads them: a lone `any` admits every one, a `!` refuses one,
    and any other entries name the only ones."""
    for key, value in TARGETS[platform].items():
        wanted = entry.get(key)
        if wanted is None:
            continue
        listed = [wanted] if isinstance(wanted, str) else wanted
        if not isinstance(listed, list):
            return False
        if listed == ["any"]:
            continue
        refused = [str(each)[1:] for each in listed if str(each).startswith("!")]
        if value in refused or (len(refused) < len(listed) and value not in listed):
            return False
    return True


def _checked(name: str, url: str, integrity: str) -> str:
    """The sha512 npm locked for a package's archive, once its archive is one
    on the registry; a git, remote or file dependency is refused here."""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "registry.npmjs.org"
        or not parts.path.endswith(".tgz")
        or parts.query
        or parts.fragment
    ):
        raise Rejected(f"{name} did not come from the npm registry.")
    algorithm, _, expected = integrity.partition("-")
    if algorithm != "sha512":
        raise Rejected(f"npm did not identify {name} by sha512.")
    return expected


async def _download(
    client: httpx.AsyncClient, url: str, expected: str, root: Path, name: str, cap: int, used: int
) -> tuple[str, int]:
    sha256, sha512, size = hashlib.sha256(), hashlib.sha512(), 0
    path = root / f"{name.replace('/', '-')}-{os.urandom(8).hex()}.part"
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with path.open("wb") as output:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    size += len(chunk)
                    if used + size > cap:
                        raise TooLarge(f"The npm packages exceed the {cap} byte limit.")
                    sha256.update(chunk)
                    sha512.update(chunk)
                    output.write(chunk)
    except httpx.HTTPError as error:
        raise Unavailable(f"Could not download {name} from the npm registry.") from error
    if base64.b64encode(sha512.digest()).decode() != expected:
        raise Rejected(f"{name} did not match the integrity npm locked.")
    digest = sha256.hexdigest()
    os.replace(path, root / digest)
    return digest, size
