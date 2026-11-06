"""Coursier resolves only from Maven Central, and every jar is hashed here."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.registries import EnvironmentResolver, watch
from forge.adapters.registries.maven import MavenResolver
from forge.domain.environment import PLATFORMS, Environment, pieces_fingerprint
from forge.domain.errors import Rejected, TooLarge, Unavailable
from tests.adapters.test_package_resolver import Registry

JUNIT = "org.junit.jupiter:junit-jupiter:5.+"
CENTRAL = "https/repo1.maven.org/maven2"
RECORDED = {
    "org.apiguardian:apiguardian-api:1.1.2": "org/apiguardian/apiguardian-api/1.1.2",
    "org.junit.jupiter:junit-jupiter-api:5.14.4": "org/junit/jupiter/junit-jupiter-api/5.14.4",
    "org.junit.jupiter:junit-jupiter-engine:5.14.4": (
        "org/junit/jupiter/junit-jupiter-engine/5.14.4"
    ),
    "org.junit.jupiter:junit-jupiter-params:5.14.4": (
        "org/junit/jupiter/junit-jupiter-params/5.14.4"
    ),
    "org.junit.jupiter:junit-jupiter:5.14.4": "org/junit/jupiter/junit-jupiter/5.14.4",
    "org.junit.platform:junit-platform-commons:1.14.4": (
        "org/junit/platform/junit-platform-commons/1.14.4"
    ),
    "org.junit.platform:junit-platform-engine:1.14.4": (
        "org/junit/platform/junit-platform-engine/1.14.4"
    ),
    "org.opentest4j:opentest4j:1.3.0": "org/opentest4j/opentest4j/1.3.0",
}
"""What Coursier 2.1.26's report named for `junit-jupiter:5.+` on 2026-11-05:
each coordinate and its folder under Maven Central in Coursier's cache."""


NOT_FOUND = b"""\
Downloading https://repo1.maven.org/maven2/org/nope/other/2.0/other-2.0.pom
Failed to download /srv/forge/cache/maven-x1/cache/https/repo1.maven.org/maven2/org/nope
Resolution error: Error downloading org.nope:other:2.0
  not found: https://repo1.maven.org/maven2/org/nope/other/2.0/other-2.0.pom
Error downloading org.nope:nothing:1.0
  not found: /srv/forge/cache/maven-x1/cache/https/repo1.maven.org/maven2/org/nope/nothing.pom
"""
"""What Coursier 2.1.26 says when two packages are not on Central, with a
folder of the forge's."""
UNREACHED = b"""\
Resolution error: Error downloading org.opentest4j:opentest4j:1.3.0
  download error: Caught java.net.ConnectException (Connection refused) while downloading \
https://repo1.maven.org/maven2/org/opentest4j/opentest4j/1.3.0/opentest4j-1.3.0.pom
"""
"""What Coursier 2.1.26 says when Central cannot be reached."""


class Fetch:
    """Coursier's fetch as it ran: the report it writes and the jars it
    leaves in the cache it was given, with `place` choosing a jar's path."""

    def __init__(self, place: dict[str, str] | None = None) -> None:
        self.place = place or {}
        self.args: tuple[str, ...] = ()
        self.env: dict[str, str] = {}
        self.returncode = 0
        self.said = NOT_FOUND

    async def __call__(self, *args: str, **kwargs: Any) -> Fetch:
        self.args, self.env = args, kwargs["env"]
        cache = Path(args[args.index("--cache") + 1])
        dependencies = []
        for coord, folder in RECORDED.items():
            artifact, version = coord.split(":")[1:]
            path = cache / self.place.get(coord, f"{CENTRAL}/{folder}/{artifact}-{version}.jar")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"jar of {coord}".encode())
            dependencies.append(
                {"coord": coord, "file": str(path), "directDependencies": [], "dependencies": []}
            )
        dependencies.append(
            {"coord": "org.junit:junit-bom:5.14.4", "directDependencies": [], "dependencies": []}
        )
        Path(args[args.index("--json-output-file") + 1]).write_text(
            json.dumps(
                {
                    "conflict_resolution": {JUNIT: "org.junit.jupiter:junit-jupiter:5.14.4"},
                    "dependencies": dependencies,
                    "version": "0.1.0",
                }
            )
        )
        return self

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self.said

    async def wait(self) -> int:
        return self.returncode


async def test_each_jar_is_hashed_and_named_by_its_central_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = Fetch()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fetch)
    monkeypatch.setenv("COURSIER_REPOSITORIES", "https://private.test/maven")
    monkeypatch.setenv("COURSIER_MIRRORS", "/etc/mirrors")
    jars, files = await MavenResolver().resolve([JUNIT], tmp_path, cap=1024 * 1024)

    assert fetch.args[:5] == ("cs", "fetch", "--no-default", "-r", "central")
    assert fetch.args[-1] == JUNIT
    assert fetch.args[fetch.args.index("--checksum") + 1] == "SHA-1"
    assert set(fetch.env) == {"PATH", "HOME", "COURSIER_CONFIG_DIR"}
    assert [jar.coord for jar in jars] == sorted(RECORDED)
    api = next(jar for jar in jars if jar.coord == "org.junit.jupiter:junit-jupiter-api:5.14.4")
    body = b"jar of org.junit.jupiter:junit-jupiter-api:5.14.4"
    assert (api.file_name, api.url, api.size, api.sha256) == (
        "junit-jupiter-api-5.14.4.jar",
        "https://repo1.maven.org/maven2/org/junit/jupiter/junit-jupiter-api/5.14.4/"
        "junit-jupiter-api-5.14.4.jar",
        len(body),
        hashlib.sha256(body).hexdigest(),
    )
    assert files[api.sha256] == tmp_path / api.sha256
    assert files[api.sha256].read_bytes() == body


@pytest.mark.parametrize(
    ("place", "words"),
    [
        (
            {"org.opentest4j:opentest4j:1.3.0": "https/jitpack.io/org/opentest4j-1.3.0.jar"},
            "did not come from Maven Central",
        ),
        (
            {"org.opentest4j:opentest4j:1.3.0": f"{CENTRAL}/../../outside/opentest4j.jar"},
            "did not come from Maven Central",
        ),
        (
            {"org.opentest4j:opentest4j:1.3.0": f"{CENTRAL}/org/opentest4j/1.3.0/a.pom"},
            "did not come from Maven Central",
        ),
        (
            {
                "org.opentest4j:opentest4j:1.3.0": (
                    f"{CENTRAL}/elsewhere/1.3.0/junit-jupiter-5.14.4.jar"
                )
            },
            "both named junit-jupiter-5.14.4.jar",
        ),
    ],
)
async def test_a_jar_from_elsewhere_or_sharing_a_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, place: dict[str, str], words: str
) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", Fetch(place))
    with pytest.raises(Rejected, match=words):
        await MavenResolver().resolve([JUNIT], tmp_path, cap=1024 * 1024)


async def test_the_jars_are_held_to_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", Fetch())
    with pytest.raises(TooLarge, match="Maven packages"):
        await MavenResolver().resolve([JUNIT], tmp_path, cap=100)


GROWING = """#!/bin/sh
echo $$ > "$PIDS"
while [ "$1" != "--cache" ]; do shift; done
mkdir -p "$2/https/repo1.maven.org/maven2/big"
while :; do
    head -c 65536 /dev/zero >> "$2/https/repo1.maven.org/maven2/big/big.jar.part"
    sleep 0.05
done
"""
"""A Coursier that downloads without end."""


async def test_coursier_is_stopped_once_its_cache_passes_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = tmp_path / "bin" / "cs"
    program.parent.mkdir()
    program.write_text(GROWING)
    program.chmod(0o755)
    pids = tmp_path / "pids"
    monkeypatch.setattr(watch, "WATCHED", 0.05)
    real = asyncio.create_subprocess_exec

    async def started(*args: Any, **kwargs: Any) -> Any:
        return await real(*args, **{**kwargs, "env": {**kwargs["env"], "PIDS": str(pids)}})

    monkeypatch.setattr(asyncio, "create_subprocess_exec", started)
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(TooLarge, match="Maven packages"):
        await asyncio.wait_for(
            MavenResolver(str(program)).resolve([JUNIT], root, cap=256 * 1024), 20
        )
    with pytest.raises(ProcessLookupError):
        os.kill(int(pids.read_text()), 0)


async def test_a_failed_fetch_names_the_packages_central_lacks_and_nothing_of_the_forge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = Fetch()
    fetch.returncode = 1
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fetch)
    with pytest.raises(Rejected) as refused:
        await MavenResolver().resolve([JUNIT], tmp_path, cap=1024 * 1024)
    assert str(refused.value) == (
        "The Maven packages could not be resolved from Maven Central. "
        "Not found: org.nope:nothing:1.0, org.nope:other:2.0."
    )


async def test_a_fetch_that_cannot_reach_central_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = Fetch()
    fetch.returncode = 1
    fetch.said = UNREACHED
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fetch)
    with pytest.raises(Unavailable) as refused:
        await MavenResolver().resolve([JUNIT], tmp_path, cap=1024 * 1024)
    assert "/srv" not in str(refused.value) and "Central" in str(refused.value)


async def test_a_missing_coursier_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(Unavailable, match="Coursier"):
        await MavenResolver(str(tmp_path / "no-cs")).resolve([JUNIT], tmp_path, cap=1024)


async def test_a_kotlin_lock_gives_both_platforms_the_same_jars_in_its_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def downloaded(*args: Any) -> None:
        pass

    monkeypatch.setattr("forge.adapters.registries._download", downloaded)
    monkeypatch.setattr("forge.adapters.registries._check_scripts", lambda *args: None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", Fetch())
    resolver = EnvironmentResolver(tmp_path, registry=Registry(tool="kotlin"))
    async with (
        resolver.prepare(Environment.model_validate({"kotlin": {}}), cap=1024 * 1024) as bare,
        resolver.prepare(
            Environment.model_validate({"kotlin": {"packages": [JUNIT]}}), cap=1024 * 1024
        ) as tested,
    ):
        locks = [tested.lock.platforms[platform]["kotlin"] for platform in PLATFORMS]
        assert locks[0].maven == locks[1].maven and len(locks[0].maven) == len(RECORDED)
        assert locks[0].asked == {"packages": [JUNIT]}
        assert {jar.sha256 for jar in locks[0].maven} <= tested.files.keys()
        assert locks[0].fingerprint != bare.lock.platforms[PLATFORMS[0]]["kotlin"].fingerprint
    renamed = [jar.model_copy(update={"file_name": f"x-{jar.file_name}"}) for jar in locks[0].maven]
    conda = locks[0].conda
    assert pieces_fingerprint(conda, maven=renamed) != pieces_fingerprint(
        conda, maven=locks[0].maven
    )
    assert pieces_fingerprint(conda, maven=locks[0].maven) == locks[0].fingerprint
