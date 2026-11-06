"""One fixed channel, explicit machine facts and a persistent repodata gateway."""

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import rattler
from rattler.exceptions import GatewayError, SolverError
from rattler.networking import RetryMiddleware

from forge.domain.environment import PLATFORMS, CondaPiece
from forge.domain.environment.tools import COMPANIONS, TOOLS
from forge.domain.errors import Rejected, Unavailable

CHANNEL = "https://conda.anaconda.org/conda-forge"
ARCH = {"linux-64": "x86_64", "linux-aarch64": "aarch64"}
# rattler's own client sets no timeout, so a stalled read of the index would
# hold a preparation until its 15-minute limit rather than retry.
REQUEST_TIMEOUT_S = 120


class CondaRegistry:
    def __init__(self, cache: Path) -> None:
        self.gateway = rattler.Gateway(
            cache_dir=cache,
            client=rattler.Client([RetryMiddleware(3)], timeout=REQUEST_TIMEOUT_S),
        )

    async def solve(
        self, language: str, specs: Sequence[str], newest_before: datetime | None
    ) -> dict[str, list[CondaPiece]]:
        named = [TOOLS[language]]
        if companion := COMPANIONS.get(language):
            named.append(TOOLS[companion])
        limits: dict[str, str] = {}
        while True:
            found: dict[str, list[CondaPiece]] = {}
            versions: dict[str, list[rattler.Version]] = {name: [] for name in named}
            for platform in PLATFORMS:
                virtual = [
                    rattler.GenericVirtualPackage(
                        rattler.PackageName(name),
                        rattler.Version(version),
                        rattler.BuildString(build),
                    )
                    for name, version, build in (
                        ("__unix", "0", "0"),
                        ("__linux", "4.18", "0"),
                        ("__glibc", "2.28", "0"),
                        ("__archspec", "1", ARCH[platform]),
                    )
                ]
                try:
                    records = await rattler.solve(
                        [CHANNEL],
                        list(specs),
                        gateway=self.gateway,
                        platforms=[rattler.Subdir(platform), rattler.Subdir("noarch")],
                        virtual_packages=virtual,
                        exclude_newer=newest_before,
                        timeout=timedelta(minutes=5),
                        channel_relations="disabled",
                        constraints=[f"{name} <={limit}" for name, limit in limits.items()] or None,
                    )
                except SolverError as error:
                    raise Rejected(
                        f"The {language} toolchain could not be prepared for {platform}: {error}"
                    ) from error
                except GatewayError as error:
                    raise Unavailable("Could not read the conda-forge package index.") from error
                pieces = []
                for record in records:
                    raw = json.loads(record.to_json())
                    if not raw.get("sha256") or raw.get("size") is None:
                        raise Rejected(
                            f"conda-forge did not identify {raw['name']} by SHA-256 and size."
                        )
                    pieces.append(
                        CondaPiece(
                            name=raw["name"],
                            version=raw["version"],
                            build=raw["build"],
                            subdir=raw["subdir"],
                            file_name=raw["fn"],
                            url=raw["url"],
                            size=raw["size"],
                            sha256=raw["sha256"],
                            record=raw,
                        )
                    )
                for name in named:
                    picked = next(piece for piece in pieces if piece.name == name)
                    versions[name].append(rattler.Version(picked.version))
                found[platform] = pieces
            apart = {name: picks for name, picks in versions.items() if picks[0] != picks[1]}
            if not apart:
                return found
            # Each iteration lowers a pick until the newest compatible version agrees,
            # the toolchain's and the one it needs alike.
            limits.update({name: str(min(picks)) for name, picks in apart.items()})
