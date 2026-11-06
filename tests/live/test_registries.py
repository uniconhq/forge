"""The package sources as they answer today: Coursier from Maven Central, npm
from its registry and conda-forge's solve. `UNICON_LIVE_REGISTRIES` turns
them on; `UNICON_LIVE_COURSIER` names Coursier's launcher when it is not `cs`
on the path, and npm is found on the path."""

import hashlib
import os
from pathlib import Path

import httpx
import pytest

from forge.adapters.registries.conda import CondaRegistry
from forge.adapters.registries.maven import MavenResolver
from forge.adapters.registries.npm import NpmResolver
from forge.domain.environment import PLATFORMS

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("UNICON_LIVE_REGISTRIES"), reason="no live registries configured"
    ),
]


async def test_coursier_pins_junit_5_and_each_jar_matches_maven_centrals_own_sha1(
    tmp_path: Path,
) -> None:
    resolver = MavenResolver(os.environ.get("UNICON_LIVE_COURSIER", "cs"))
    jars, files = await resolver.resolve(
        ["org.junit.jupiter:junit-jupiter:5.+"], tmp_path, cap=64 * 1024 * 1024
    )
    junit = next(jar for jar in jars if jar.coord.startswith("org.junit.jupiter:junit-jupiter:"))
    assert junit.coord.rsplit(":", 1)[1].startswith("5.")
    assert len(jars) == 8
    async with httpx.AsyncClient(timeout=60) as client:
        for jar in jars:
            body = files[jar.sha256].read_bytes()
            assert hashlib.sha256(body).hexdigest() == jar.sha256 and len(body) == jar.size
            published = (await client.get(f"{jar.url}.sha1")).raise_for_status().text
            assert published.split()[0] == hashlib.sha1(body).hexdigest()


async def test_npm_resolves_jest_with_its_install_script_package_and_each_platforms_own(
    tmp_path: Path,
) -> None:
    async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as client:
        trees, files = await NpmResolver().resolve(
            ["jest@30"], tmp_path, client, cap=512 * 1024 * 1024
        )
    paths = {platform: {each.path for each in trees[platform]} for platform in PLATFORMS}
    for platform, arch, other in (("linux-64", "x64", "arm64"), ("linux-aarch64", "arm64", "x64")):
        assert {"node_modules/jest", "node_modules/@parcel/watcher"} <= paths[platform]
        assert f"node_modules/@parcel/watcher-linux-{arch}-glibc" in paths[platform]
        assert f"node_modules/@parcel/watcher-linux-{other}-glibc" not in paths[platform]
        assert not any("darwin" in path or "musl" in path for path in paths[platform])
    assert {each.sha256 for tree in trees.values() for each in tree} == files.keys()


async def test_conda_forge_picks_r_for_testthat_and_pkg_config_beside_the_cpp_libraries(
    tmp_path: Path,
) -> None:
    registry = CondaRegistry(tmp_path / "repodata")
    r = await registry.solve("r", ["r-base 4.6.*|4.5.*", "r-testthat"], None)
    cpp = await registry.solve("cpp", ["gxx 16.*", "pkg-config", "gtest", "eigen"], None)
    for platform in PLATFORMS:
        names = {piece.name: piece.version for piece in r[platform]}
        assert names["r-base"].startswith(("4.6.", "4.5.")) and "r-testthat" in names
        assert {"gxx", "pkg-config", "gtest", "eigen"} <= {piece.name for piece in cpp[platform]}
    assert (
        len({next(p.version for p in r[platform] if p.name == "r-base") for platform in PLATFORMS})
        == 1
    )
