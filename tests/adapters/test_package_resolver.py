"""The resolver uses only the fixed wheel registry and verifies every download."""

import asyncio
import hashlib
import io
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from forge.adapters.registries import EnvironmentResolver, pypi
from forge.domain.environment import PLATFORMS, CondaPiece, Environment
from forge.domain.errors import Rejected, TooLarge


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    wheels = {platform: f"wheel for {platform}".encode() for platform in PLATFORMS}
    real_client = httpx.AsyncClient

    def download(request: httpx.Request) -> httpx.Response:
        if request.url.host == "conda.anaconda.org":
            return httpx.Response(200, content=archive())
        assert request.url.host == "files.pythonhosted.org"
        return httpx.Response(
            200,
            content=next(body for platform, body in wheels.items() if platform in request.url.path),
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(download), **kwargs),
    )
    monkeypatch.setenv("UV_INDEX_URL", "http://private.test")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "http://private.test")

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def compile(*args: str, **kwargs: Any) -> Process:
        assert args[1:4] == ("--no-config", "pip", "compile")
        assert args[args.index("--index-url") + 1] == "https://pypi.org/simple"
        assert args[args.index("--only-binary") + 1] == ":all:"
        assert args[args.index("--python-version") + 1] == "3.13"
        assert "--generate-hashes" in args
        assert not any(key.startswith(("UV_", "PIP_")) for key in kwargs["env"])
        target = args[args.index("--python-platform") + 1]
        platform = next(platform for platform, wanted in pypi.TARGETS.items() if wanted == target)
        output = Path(args[args.index("--output-file") + 1])
        assert output.name == f"pylock.{platform}.toml"
        body = wheels[platform]
        digest = hashlib.sha256(body).hexdigest()
        output.write_text(f'''[[packages]]
name = "numpy"
version = "2.3.0"
[[packages.wheels]]
url = "https://files.pythonhosted.org/packages/numpy-{platform}.whl"
size = {len(body)}
[packages.wheels.hashes]
sha256 = "{digest}"
''')
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", compile)
    return wheels


async def test_each_platform_is_resolved_and_each_wheel_is_verified_and_retained(
    registry: dict[str, bytes],
    tmp_path: Path,
) -> None:
    asked = Environment.model_validate({"python": {"packages": ["numpy>=2.3"]}})
    async with EnvironmentResolver(tmp_path, registry=Registry()).prepare(
        asked, cap=1024 * 1024
    ) as prepared:
        assert set(prepared.lock.platforms) == set(PLATFORMS)
        for platform, body in registry.items():
            (wheel,) = prepared.lock.platforms[platform]["python"].pypi
            assert wheel.sha256 == hashlib.sha256(body).hexdigest()
            assert prepared.files[wheel.sha256].read_bytes() == body
        paths = list(prepared.files.values())
    assert all(not path.exists() for path in paths)


async def test_the_environment_cap_is_checked_before_downloading(
    registry: dict[str, bytes],
    tmp_path: Path,
) -> None:
    asked = Environment.model_validate({"python": {"packages": ["numpy"]}})
    with pytest.raises(TooLarge, match=r"come to .*, more than 1 B"):
        async with EnvironmentResolver(tmp_path, registry=Registry()).prepare(asked, cap=1):
            pytest.fail("an oversized environment cannot be prepared")


def archive(script: bool = False, members: tuple[str, ...] = ()) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:bz2") as tar:
        for name in (*members, *(["bin/.python-post-link.sh"] if script else [])):
            item = tarfile.TarInfo(name)
            item.size = 4
            tar.addfile(item, io.BytesIO(b"exit"))
    return output.getvalue()


class Registry:
    def __init__(
        self, script: bool = False, tool: str = "python", members: tuple[str, ...] = ()
    ) -> None:
        self.script = script
        self.tool = tool
        self.members = members
        self.calls: list[str] = []

    async def solve(
        self, language: str, specs: Any, newest_before: Any
    ) -> dict[str, list[CondaPiece]]:
        self.calls.append(language)
        body = archive(self.script, self.members)
        digest = hashlib.sha256(body).hexdigest()
        return {
            platform: [
                CondaPiece(
                    name=self.tool,
                    version="3.13",
                    build="test",
                    subdir=platform,
                    file_name="python.tar.bz2",
                    url="https://conda.anaconda.org/conda-forge/noarch/python.tar.bz2",
                    sha256=digest,
                    size=len(body),
                    record={},
                )
            ]
            for platform in PLATFORMS
        }


async def test_unchanged_language_records_are_reused_without_another_solve_or_download(
    registry: dict[str, bytes],
    tmp_path: Path,
) -> None:
    source = Registry()
    resolver = EnvironmentResolver(tmp_path, registry=source)
    asked = Environment.model_validate({"python": {"packages": ["numpy"]}})
    async with resolver.prepare(asked, cap=1024 * 1024) as first:
        previous = first.lock
    async with resolver.prepare(asked, cap=1024 * 1024, previous=previous) as second:
        assert second.lock == previous and second.files == {}
    assert source.calls == ["python"]


@pytest.mark.parametrize("allowed", [0, 1])
async def test_a_stopped_preparation_solves_and_downloads_nothing_more(
    registry: dict[str, bytes],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allowed: int,
) -> None:
    import forge.adapters.registries as registries

    class Stopped(Exception):
        pass

    asked: list[int] = []
    downloads: list[str] = []

    async def proceed() -> None:
        asked.append(1)
        if len(asked) > allowed:
            raise Stopped

    async def downloaded(client: httpx.AsyncClient, piece: CondaPiece, path: Path) -> None:
        downloads.append(piece.name)

    monkeypatch.setattr(registries, "_download", downloaded)
    source = Registry()
    with pytest.raises(Stopped):
        async with EnvironmentResolver(tmp_path, registry=source).prepare(
            Environment.model_validate({"python": {}}), cap=1024 * 1024, proceed=proceed
        ):
            pytest.fail("a stopped preparation yields nothing")
    assert source.calls == ["python"][:allowed] and downloads == []


async def test_a_link_script_is_refused_naming_the_package(
    registry: dict[str, bytes],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import forge.adapters.registries as registries

    async def downloaded(client: httpx.AsyncClient, piece: CondaPiece, path: Path) -> None:
        path.write_bytes(archive(True))

    monkeypatch.setattr(registries, "_download", downloaded)
    with pytest.raises(Rejected, match="python carries a link script"):
        async with EnvironmentResolver(tmp_path, registry=Registry(True)).prepare(
            Environment.model_validate({"python": {}}),
            cap=1024 * 1024,
        ):
            pytest.fail("link scripts cannot be installed")


@pytest.mark.parametrize("version", [None, "3.15"])
async def test_unnamed_tools_age_fourteen_days_but_named_versions_do_not(
    registry: dict[str, bytes], tmp_path: Path, version: str | None
) -> None:
    from datetime import UTC, datetime, timedelta

    class Recording(Registry):
        async def solve(
            self, language: str, specs: Any, newest_before: Any
        ) -> dict[str, list[CondaPiece]]:
            assert specs == ["python 3.15.*"]
            if version:
                assert newest_before is None
            else:
                age = datetime.now(UTC) - newest_before
                assert timedelta(days=14) <= age < timedelta(days=14, seconds=5)
            return await super().solve(language, specs, newest_before)

    value = {"version": version} if version else {}
    async with EnvironmentResolver(tmp_path, registry=Recording()).prepare(
        Environment.model_validate({"python": value}), cap=1024 * 1024
    ) as prepared:
        assert prepared.lock.platforms["linux-64"]["python"].picked == "python 3.13"


@pytest.mark.parametrize(
    ("language", "entry", "specs"),
    [
        ("javascript", {}, ["nodejs 26.*|24.*"]),
        ("javascript", {"version": "24"}, ["nodejs 24.*"]),
        ("typescript", {}, ["typescript 6.0.*", "nodejs 26.*|24.*"]),
        ("kotlin", {}, ["kotlin 2.4.*", "openjdk 25.*"]),
        ("rust", {}, ["rust 1.99.*", "gcc 16.*"]),
        ("csharp", {}, ["dotnet 10.0.*"]),
        ("ruby", {"conda": ["libyaml"]}, ["ruby 4.0.*", "libyaml"]),
        (
            "r",
            {"packages": ["testthat", "data.table=1.17"]},
            ["r-base 4.6.*|4.5.*", "r-testthat", "r-data.table 1.17.*"],
        ),
        ("r", {"version": "4.5", "conda": ["r-ggplot2"]}, ["r-base 4.5.*", "r-ggplot2"]),
        ("cpp", {}, ["gxx 16.*"]),
        (
            "cpp",
            {"conda": ["gtest", "eigen=5.0"]},
            ["gxx 16.*", "pkg-config", "gtest", "eigen 5.0.*"],
        ),
        ("c", {"conda": ["zlib"]}, ["gcc 16.*", "pkg-config", "zlib"]),
    ],
)
async def test_each_language_solves_its_toolchain_its_companion_and_its_conda_libraries(
    registry: dict[str, bytes],
    tmp_path: Path,
    language: str,
    entry: dict[str, object],
    specs: list[str],
) -> None:
    from forge.domain.environment.tools import TOOLS

    asked: list[list[str]] = []

    class Recording(Registry):
        async def solve(
            self, language: str, specs: Any, newest_before: Any
        ) -> dict[str, list[CondaPiece]]:
            asked.append(specs)
            solved = await super().solve(language, specs, newest_before)
            return {
                platform: [piece.model_copy(update={"name": TOOLS[language]}) for piece in pieces]
                for platform, pieces in solved.items()
            }

    async with EnvironmentResolver(tmp_path, registry=Recording()).prepare(
        Environment.model_validate({language: entry}), cap=1024 * 1024
    ) as prepared:
        assert prepared.lock.version == 3
        assert prepared.lock.platforms["linux-64"][language].picked == f"{TOOLS[language]} 3.13"
    assert asked == [specs]


async def test_no_wheel_refusal_names_the_solved_python_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def compile(requirements: Path, output: Path, target: str, python_version: str) -> None:
        assert python_version == "3.12.9"
        output.write_text('[[packages]]\nname="no-wheel"\nversion="1"\n')

    monkeypatch.setattr(pypi, "_compile", compile)
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            Rejected, match=r"no-wheel has no ready-built wheel for Python 3\.12\.9"
        ):
            await pypi.PypiResolver().resolve(
                ["no-wheel"], "3.12.9", "linux-aarch64", tmp_path, client, cap=1024
            )


@pytest.mark.parametrize("truncated", [False, True])
async def test_a_malformed_conda_zstd_member_is_refused_without_leaving_preparation_pending(
    tmp_path: Path,
    truncated: bool,
) -> None:
    import zipfile
    from compression.zstd import compress

    from forge.adapters.registries import _check_scripts

    records = await Registry().solve("python", ["python"], None)
    piece = records["linux-64"][0].model_copy(update={"file_name": "python.conda"})
    path = tmp_path / "python.conda"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "pkg-python.tar.zst", compress(b"data")[:-1] if truncated else b"not zstd data"
        )
    with pytest.raises(Rejected, match="could not be inspected"):
        _check_scripts(path, piece)
