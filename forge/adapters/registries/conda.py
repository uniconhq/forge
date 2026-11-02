"""One fixed channel, explicit machine facts and a persistent repodata gateway."""

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import rattler
from rattler.exceptions import GatewayError, SolverError

from forge.domain.environment import PLATFORMS, CondaPiece
from forge.domain.environment.tools import TOOLS
from forge.domain.errors import Rejected, Unavailable

CHANNEL = "https://conda.anaconda.org/conda-forge"
ARCH = {"linux-64": "x86_64", "linux-aarch64": "aarch64"}


class CondaRegistry:
    def __init__(self, cache: Path) -> None:
        self.gateway = rattler.Gateway(cache_dir=cache)

    async def solve(
        self, language: str, specs: Sequence[str], newest_before: datetime | None
    ) -> dict[str, list[CondaPiece]]:
        limit: str | None = None
        while True:
            found: dict[str, list[CondaPiece]] = {}
            versions: list[rattler.Version] = []
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
                        constraints=[f"{TOOLS[language]} <={limit}"] if limit else None,
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
                picked = next(piece for piece in pieces if piece.name == TOOLS[language])
                versions.append(rattler.Version(picked.version))
                found[platform] = pieces
            if versions[0] == versions[1]:
                return found
            # Each iteration lowers a pick until the newest compatible version agrees.
            limit = str(min(versions))
