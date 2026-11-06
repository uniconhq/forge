"""npm resolves only from its public registry, through a proxy that reaches
nothing else, with scripts and git off; each tarball
is downloaded and hashed here, and each platform keeps only its own optional
packages."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from forge.adapters.registries import EnvironmentResolver
from forge.adapters.registries.npm import NpmResolver, _confined, _runs_on
from forge.domain.environment import PLATFORMS, Environment
from forge.domain.errors import Rejected, TooLarge, Unavailable
from tests.adapters.test_package_resolver import Registry, archive

WATCHER = {"os": ["linux"], "libc": ["glibc"], "optional": True}
RECORDED: dict[str, dict[str, Any]] = {
    "": {"name": "unicon-packages", "dependencies": {"chai": "*", "jest": "30"}},
    "node_modules/jest": {"version": "30.5.2", "bin": {"jest": "bin/jest.js"}},
    "node_modules/chai": {"version": "6.3.0", "engines": {"node": ">=18"}},
    "node_modules/@parcel/watcher": {"version": "2.6.0", "hasInstallScript": True},
    "node_modules/@parcel/watcher-linux-arm64-glibc": {
        "version": "2.6.0",
        "cpu": ["arm64"],
        **WATCHER,
    },
    "node_modules/@parcel/watcher-linux-x64-glibc": {"version": "2.6.0", "cpu": ["x64"], **WATCHER},
    "node_modules/@parcel/watcher-linux-x64-musl": {
        "version": "2.6.0",
        "cpu": ["x64"],
        "os": ["linux"],
        "libc": ["musl"],
        "optional": True,
    },
    "node_modules/@parcel/watcher-darwin-arm64": {
        "version": "2.6.0",
        "cpu": ["arm64"],
        "os": ["darwin"],
        "optional": True,
    },
    "node_modules/@parcel/watcher-win32-x64": {
        "version": "2.6.0",
        "cpu": ["x64"],
        "os": ["win32"],
        "optional": True,
    },
    "node_modules/string-width-cjs": {"name": "string-width", "version": "4.2.3"},
    "node_modules/string-width": {"version": "5.1.2"},
    "node_modules/@isaacs/cliui/node_modules/string-width": {"version": "5.1.2"},
}
"""The shape of npm 11's lock for `jest@30 chai` on 2026-11-05, trimmed: the
two asked for, the package with an install script, its platform-only optional
packages, an alias, and one package placed twice in the tree."""


def named(path: str, entry: dict[str, Any]) -> str:
    return str(entry.get("name", path.rsplit("node_modules/", 1)[-1]))


def tarball(path: str, entry: dict[str, Any]) -> bytes:
    return f"tarball of {named(path, entry)}@{entry['version']}".encode()


def url(path: str, entry: dict[str, Any]) -> str:
    name = named(path, entry)
    return f"https://registry.npmjs.org/{name}/-/{name.rsplit('/', 1)[-1]}-{entry['version']}.tgz"


def locked(changes: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    packages: dict[str, Any] = {}
    for path, entry in {**RECORDED, **(changes or {})}.items():
        if path and not entry.get("inBundle") and not entry.get("link"):
            digest = hashlib.sha512(tarball(path, entry)).digest()
            entry = {
                "resolved": url(path, entry),
                "integrity": "sha512-" + base64.b64encode(digest).decode(),
                **entry,
            }
        packages[path] = entry
    return {"name": "unicon-packages", "lockfileVersion": 3, "requires": True, "packages": packages}


class Install:
    """npm as it ran: its version, then `install --package-lock-only`, which
    writes the lock and, when it fails, says so on standard error."""

    def __init__(self, lock: dict[str, Any] | None = None, version: str = "11.11.0") -> None:
        self.lock = lock or locked()
        self.version = version
        self.args: tuple[str, ...] = ()
        self.kwargs: dict[str, Any] = {}
        self.asked: dict[str, Any] = {}
        self.returncode = 0
        self.said = b"npm error code ERESOLVE\nnpm error fetch https://internal.test/secret"
        self.calls = 0

    async def __call__(self, *args: str, **kwargs: Any) -> Process:
        self.calls += 1
        if args[1:] == ("--version",):
            return Process(0, f"{self.version}\n".encode(), b"")
        self.args, self.kwargs = args, kwargs
        folder = Path(kwargs["cwd"])
        self.asked = json.loads((folder / "package.json").read_text())
        (folder / "package-lock.json").write_text(json.dumps(self.lock))
        return Process(self.returncode, b"", self.said if self.returncode else b"")


class Process:
    def __init__(self, returncode: int, out: bytes, error: bytes) -> None:
        self.returncode, self.out, self.error = returncode, out, error

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.out, self.error

    async def wait(self) -> int:
        return self.returncode


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []
    bodies = {url(path, entry): tarball(path, entry) for path, entry in RECORDED.items() if path}

    def serve(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if request.url.host == "conda.anaconda.org":
            return httpx.Response(200, content=archive())
        return httpx.Response(200, content=bodies.get(str(request.url), b"something else"))

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(serve), **kwargs)
    )
    return asked


async def resolve(
    tmp_path: Path, packages: list[str], cap: int = 1024 * 1024
) -> tuple[dict[str, Any], dict[str, Path]]:
    async with httpx.AsyncClient() as client:
        return await NpmResolver().resolve(packages, tmp_path, client, cap=cap)


async def test_each_platform_keeps_its_own_optional_packages_and_every_install_script_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[str]
) -> None:
    install = Install()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", install)
    monkeypatch.setenv("npm_config_registry", "https://private.test")
    monkeypatch.setenv("NPM_CONFIG_IGNORE_SCRIPTS", "false")
    trees, files = await resolve(tmp_path, ["jest@30", "chai"])

    assert install.asked["dependencies"] == {"jest": "30", "chai": "*"}
    assert install.args[:4] == ("npm", "install", "--package-lock-only", "--ignore-scripts")
    assert {"--allow-git=none", "--registry=https://registry.npmjs.org/", "--noproxy="} <= set(
        install.args
    )
    proxies = {each.split("=", 1)[1] for each in install.args if "proxy=http" in each}
    assert len(proxies) == 1 and next(iter(proxies)).startswith("http://127.0.0.1:")
    env = install.kwargs["env"]
    assert env.keys() == {"PATH", "HOME"}
    assert Path(env["HOME"]).is_dir() and not any(Path(env["HOME"]).iterdir())
    for each in ("userconfig", "globalconfig"):
        given = next(arg for arg in install.args if arg.startswith(f"--{each}="))
        assert Path(given.split("=", 1)[1]).read_text() == ""
    shared = {
        "node_modules/jest",
        "node_modules/chai",
        "node_modules/@parcel/watcher",
        "node_modules/string-width-cjs",
        "node_modules/string-width",
        "node_modules/@isaacs/cliui/node_modules/string-width",
    }
    assert {package.path for package in trees["linux-64"]} == shared | {
        "node_modules/@parcel/watcher-linux-x64-glibc"
    }
    assert {package.path for package in trees["linux-aarch64"]} == shared | {
        "node_modules/@parcel/watcher-linux-arm64-glibc"
    }
    alias = next(each for each in trees["linux-64"] if each.path.endswith("string-width-cjs"))
    body = tarball("node_modules/string-width-cjs", RECORDED["node_modules/string-width-cjs"])
    assert (alias.name, alias.version, alias.size, alias.sha256) == (
        "string-width",
        "4.2.3",
        len(body),
        hashlib.sha256(body).hexdigest(),
    )
    assert files[alias.sha256].read_bytes() == body
    assert len(served) == len(set(served)) == 7


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        (
            {"node_modules/chai": {"version": "6.3.0", "resolved": "https://npm.test/c.tgz"}},
            "chai did not come from the npm registry",
        ),
        (
            {"node_modules/chai": {"version": "6.3.0", "integrity": "sha512-AAAA"}},
            "chai did not match the integrity npm locked",
        ),
        (
            {"node_modules/chai": {"version": "6.3.0", "integrity": "sha1-AAAA"}},
            "npm did not identify chai by sha512",
        ),
        ({"node_modules/../../etc": {"version": "1.0.0"}}, "outside node_modules"),
        ({"lib/chai": {"version": "1.0.0"}}, "outside node_modules"),
        ({"node_modules/linked": {"link": True, "resolved": "../linked"}}, "linked linked"),
        (
            {"node_modules/fsevents": {"version": "2.3.3", "os": ["darwin"]}},
            "fsevents 2.3.3 does not run on both Linux platforms",
        ),
    ],
)
async def test_a_package_from_elsewhere_altered_outside_the_tree_or_for_another_system_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    served: list[str],
    changes: dict[str, dict[str, Any]],
    words: str,
) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", Install(locked(changes)))
    with pytest.raises(Rejected, match=words):
        await resolve(tmp_path, ["chai"])


async def test_a_bundled_package_comes_inside_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[str]
) -> None:
    lock = locked({"node_modules/jest/node_modules/inner": {"version": "1.0.0", "inBundle": True}})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", Install(lock))
    trees, _ = await resolve(tmp_path, ["jest"])
    assert not any(package.path.endswith("inner") for package in trees["linux-64"])


async def test_the_tarballs_are_held_to_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[str]
) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", Install())
    with pytest.raises(TooLarge, match="npm packages"):
        await resolve(tmp_path, ["jest"], cap=100)


async def test_a_failed_resolve_names_npm_s_error_but_not_its_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = Install()
    install.returncode = 1
    monkeypatch.setattr(asyncio, "create_subprocess_exec", install)
    with pytest.raises(Rejected) as refused:
        await resolve(tmp_path, ["jest"])
    assert "conflict" in str(refused.value) and "ERESOLVE" in str(refused.value)
    assert "internal.test" not in str(refused.value)


async def test_an_archive_from_another_host_is_named_and_a_registry_out_of_reach_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = Install()
    install.returncode = 1
    install.said = b"npm error code E403\nnpm error 403 Forbidden - GET https://example.com/a.tgz"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", install)
    with pytest.raises(Rejected, match="somewhere other than the npm registry") as refused:
        await resolve(tmp_path, ["jest"])
    assert "example.com" not in str(refused.value)
    for code in ("ECONNRESET", "ETIMEDOUT", "EAI_AGAIN"):
        install.said = f"npm error code {code}\nnpm error network to 127.0.0.1:1".encode()
        with pytest.raises(Unavailable, match=f"could not be reached .*{code}"):
            await resolve(tmp_path, ["jest"])


@pytest.mark.parametrize(
    ("entry", "kept"),
    [
        ({"os": ["any"], "cpu": "any"}, {"linux-64", "linux-aarch64"}),
        ({"os": ["linux", "any"]}, {"linux-64", "linux-aarch64"}),
        ({"os": ["darwin", "any"]}, set()),
        ({"cpu": ["!arm64"]}, {"linux-64"}),
        ({"cpu": ["!x64", "arm64"]}, {"linux-aarch64"}),
        ({"cpu": []}, {"linux-64", "linux-aarch64"}),
        ({"libc": ["musl"]}, set()),
    ],
)
def test_a_platform_is_admitted_as_npm_admits_it(entry: dict[str, Any], kept: set[str]) -> None:
    assert {platform for platform in PLATFORMS if _runs_on(entry, platform)} == kept


async def test_a_legacy_name_with_capitals_is_placed_as_exec_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[str]
) -> None:
    unknown = base64.b64encode(hashlib.sha512(b"something else").digest()).decode()
    lock = locked(
        {"node_modules/JSONStream": {"version": "1.3.5", "integrity": f"sha512-{unknown}"}}
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", Install(lock))
    trees, _ = await resolve(tmp_path, ["JSONStream@1"])
    assert "node_modules/JSONStream" in {package.path for package in trees["linux-64"]}


async def test_an_npm_too_old_to_refuse_git_dependencies_is_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = Install(version="11.9.4")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", install)
    with pytest.raises(Unavailable, match=r"11\.10"):
        await resolve(tmp_path, ["jest"])
    assert install.calls == 1


@pytest.mark.parametrize(
    "resolved",
    [
        "git+ssh://git@github.com/someone/chai.git#0123456789abcdef0123456789abcdef01234567",
        "https://codeload.github.com/someone/chai/tar.gz/0123456789abcdef",
        "https://evil.test/chai-6.3.0.tgz",
        "file:../chai",
    ],
)
async def test_a_dependency_from_git_a_remote_url_or_a_file_is_refused_before_any_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[str], resolved: str
) -> None:
    lock = locked({"node_modules/jest/node_modules/chai": {"version": "6.3.0"}})
    lock["packages"]["node_modules/jest/node_modules/chai"]["resolved"] = resolved
    monkeypatch.setattr(asyncio, "create_subprocess_exec", Install(lock))
    with pytest.raises(Rejected, match="chai did not come from the npm registry"):
        await resolve(tmp_path, ["jest"])
    assert served == []


async def ask(proxy: str, head: bytes) -> bytes:
    host, port = proxy.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(head)
    await writer.drain()
    answer = await reader.read()
    writer.close()
    return answer


@pytest.mark.parametrize(
    "head",
    [
        b"CONNECT evil.test:443 HTTP/1.1\r\nHost: evil.test:443\r\n\r\n",
        b"CONNECT registry.npmjs.org:80 HTTP/1.1\r\n\r\n",
        b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n",
        b"GET http://registry.npmjs.org/jest HTTP/1.1\r\nHost: registry.npmjs.org\r\n\r\n",
    ],
)
async def test_npm_s_proxy_refuses_every_other_host_without_connecting(
    monkeypatch: pytest.MonkeyPatch, head: bytes
) -> None:
    connected: list[str] = []
    real = asyncio.open_connection

    async def connect(host: str, port: int, **kwargs: Any) -> Any:
        if host != "127.0.0.1":
            connected.append(f"{host}:{port}")
        return await real(host, port, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", connect)
    async with _confined() as proxy:
        answer = await ask(proxy, head)
    assert answer.startswith(b"HTTP/1.1 403")
    assert connected == []


async def test_npm_s_proxy_tunnels_to_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"registry says " + await reader.readexactly(5))
        await writer.drain()
        writer.close()

    registry = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = registry.sockets[0].getsockname()[1]
    connected: list[str] = []
    real = asyncio.open_connection

    async def connect(host: str, to: int, **kwargs: Any) -> Any:
        if host == "registry.npmjs.org":
            connected.append(f"{host}:{to}")
            return await real("127.0.0.1", port, **kwargs)
        return await real(host, to, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", connect)
    async with _confined() as proxy:
        answer = await ask(proxy, b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\nhello")
    registry.close()
    assert answer == b"HTTP/1.1 200 Connection Established\r\n\r\nregistry says hello"
    assert connected == ["registry.npmjs.org:443"]


async def test_npm_s_proxy_closes_a_tunnel_still_open_when_it_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()

    registry = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = registry.sockets[0].getsockname()[1]
    real = asyncio.open_connection

    async def connect(host: str, to: int, **kwargs: Any) -> Any:
        if host == "registry.npmjs.org":
            return await real("127.0.0.1", port, **kwargs)
        return await real(host, to, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", connect)

    async def opened() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        async with _confined() as proxy:
            reader, writer = await real(*proxy.removeprefix("http://").split(":"))
            writer.write(b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
        return reader, writer

    reader, writer = await asyncio.wait_for(opened(), 10)
    assert await asyncio.wait_for(reader.read(), 5) == b""
    writer.close()
    registry.close()


async def test_a_typescript_lock_gives_each_platform_its_tree_in_its_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[str]
) -> None:
    monkeypatch.setattr("forge.adapters.registries._check_scripts", lambda *args: None)
    resolver = EnvironmentResolver(tmp_path, registry=Registry(tool="typescript"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", Install())
    asked = Environment.model_validate({"typescript": {"packages": ["jest@30", "chai"]}})
    async with resolver.prepare(asked, cap=1024 * 1024) as first:
        locks = [first.lock.platforms[platform]["typescript"] for platform in PLATFORMS]
        assert locks[0].fingerprint != locks[1].fingerprint
        assert {each.sha256 for lock in locks for each in lock.npm} <= first.files.keys()
    moved = {"node_modules/@isaacs/cliui/node_modules/string-width": None}
    lock = locked()
    lock["packages"] = {
        ("node_modules/other/node_modules/string-width" if path in moved else path): entry
        for path, entry in lock["packages"].items()
    }
    monkeypatch.setattr(asyncio, "create_subprocess_exec", Install(lock))
    async with resolver.prepare(asked, cap=1024 * 1024) as second:
        again = second.lock.platforms[PLATFORMS[0]]["typescript"]
        assert {each.sha256 for each in again.npm} == {each.sha256 for each in locks[0].npm}
        assert again.fingerprint != locks[0].fingerprint


TOOLCHAIN = {platform: {"node_modules/typescript": "typescript"} for platform in PLATFORMS}
"""Where the TypeScript toolchain's conda packages install TypeScript."""


async def test_a_package_where_the_toolchain_installs_its_own_is_refused_and_a_peer_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[str]
) -> None:
    peer = locked({"node_modules/typescript": {"version": "5.9.3", "peer": True}})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", Install(peer))
    async with httpx.AsyncClient() as client:
        trees, _ = await NpmResolver().resolve(
            ["jest"], tmp_path, client, cap=1024 * 1024, held=TOOLCHAIN
        )
    assert not any("typescript" in package.path for package in trees["linux-64"])
    assert not any("typescript" in each for each in served)

    served.clear()
    for path in ("node_modules/typescript", "node_modules/typescript/node_modules/x"):
        lock = locked({path: {"version": "5.9.3"}})
        monkeypatch.setattr(asyncio, "create_subprocess_exec", Install(lock))
        async with httpx.AsyncClient() as client:
            with pytest.raises(Rejected, match="the toolchain's typescript already installs"):
                await NpmResolver().resolve(
                    ["jest"], tmp_path, client, cap=1024 * 1024, held=TOOLCHAIN
                )
    assert served == []


async def test_the_places_the_toolchain_holds_come_from_its_conda_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = (
        "lib/node_modules/typescript/package.json",
        "lib/node_modules/@scope/tool/index.js",
        "lib/node_modules/README",
        "lib/libnode.so",
    )
    body = archive(members=members)

    async def downloaded(client: Any, piece: Any, path: Path) -> None:
        path.write_bytes(body)

    monkeypatch.setattr("forge.adapters.registries._download", downloaded)
    install = Install(locked({"node_modules/typescript": {"version": "5.9.3"}}))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", install)
    seen: list[Any] = []
    real = NpmResolver.resolve

    async def resolving(self: NpmResolver, *args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["held"])
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(NpmResolver, "resolve", resolving)
    resolver = EnvironmentResolver(tmp_path, registry=Registry(tool="typescript", members=members))
    asked = Environment.model_validate({"typescript": {"packages": ["jest@30"]}})
    with pytest.raises(Rejected, match="the toolchain's typescript already installs its own"):
        async with resolver.prepare(asked, cap=1024 * 1024):
            pass
    assert seen == [
        {
            platform: {
                "node_modules/typescript": "typescript",
                "node_modules/@scope/tool": "typescript",
            }
            for platform in PLATFORMS
        }
    ]
