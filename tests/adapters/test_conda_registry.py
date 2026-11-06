"""Both solves use explicit machine facts and select one compatible version."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import rattler

from forge.adapters.registries import conda
from forge.domain.environment import PLATFORMS


@pytest.mark.parametrize("named", [False, True])
async def test_the_gateway_uses_fixed_facts_and_the_requested_age_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: bool
) -> None:
    gateway = object()
    clients: list[int | None] = []
    monkeypatch.setattr(rattler, "Gateway", lambda **kw: gateway)
    monkeypatch.setattr(rattler, "Client", lambda middlewares, timeout: clients.append(timeout))
    cutoff = None if named else datetime(2026, 10, 18, tzinfo=UTC)
    calls: list[dict[str, Any]] = []

    class Record:
        def __init__(self, platform: str, version: str) -> None:
            self.platform, self.version = platform, version

        def to_json(self) -> str:
            return json.dumps(
                {
                    "name": "gxx",
                    "version": self.version,
                    "build": "test",
                    "subdir": self.platform,
                    "fn": "gxx.conda",
                    "url": "https://conda.anaconda.org/conda-forge/gxx.conda",
                    "sha256": "a" * 64,
                    "size": 42,
                }
            )

    async def solve(channels: list[str], specs: list[str], **options: Any) -> list[Record]:
        assert channels == [conda.CHANNEL]
        assert specs == ["gxx 14.*"]
        assert options["gateway"] is gateway
        assert options["exclude_newer"] == cutoff
        assert options["timeout"] == timedelta(minutes=5)
        assert options["channel_relations"] == "disabled"
        platform = str(options["platforms"][0])
        assert str(options["platforms"][1]) == "noarch"
        virtual = {
            row.name.normalized: (str(row.version), str(row.build_string))
            for row in options["virtual_packages"]
        }
        assert virtual == {
            "__unix": ("0", "0"),
            "__linux": ("4.18", "0"),
            "__glibc": ("2.28", "0"),
            "__archspec": ("1", conda.ARCH[platform]),
        }
        calls.append(options)
        return [
            Record(
                platform,
                "14.3" if platform == "linux-64" and not options["constraints"] else "14.2",
            )
        ]

    monkeypatch.setattr(rattler, "solve", solve)
    answer = await conda.CondaRegistry(tmp_path).solve("cpp", ["gxx 14.*"], cutoff)
    assert clients == [conda.REQUEST_TIMEOUT_S]
    assert set(answer) == set(PLATFORMS)
    assert {rows[0].version for rows in answer.values()} == {"14.2"}
    assert len(calls) == 4
    assert [call["constraints"] for call in calls] == [None, None, ["gxx <=14.2"], ["gxx <=14.2"]]


async def test_a_companion_toolchain_agrees_on_both_platforms_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rattler, "Gateway", lambda **kw: object())
    calls: list[Any] = []

    class Record:
        def __init__(self, name: str, platform: str, version: str) -> None:
            self.name, self.platform, self.version = name, platform, version

        def to_json(self) -> str:
            return json.dumps(
                {
                    "name": self.name,
                    "version": self.version,
                    "build": "test",
                    "subdir": self.platform,
                    "fn": f"{self.name}.conda",
                    "url": f"https://conda.anaconda.org/conda-forge/{self.name}.conda",
                    "sha256": "a" * 64,
                    "size": 42,
                }
            )

    async def solve(channels: list[str], specs: list[str], **options: Any) -> list[Record]:
        platform = str(options["platforms"][0])
        calls.append(options["constraints"])
        newer = platform == "linux-64" and not options["constraints"]
        return [
            Record("kotlin", platform, "2.4.20"),
            Record("openjdk", platform, "25.0.3" if newer else "25.0.2"),
        ]

    monkeypatch.setattr(rattler, "solve", solve)
    answer = await conda.CondaRegistry(tmp_path).solve(
        "kotlin", ["kotlin 2.4.*", "openjdk 25.*"], None
    )
    assert {(piece.name, piece.version) for pieces in answer.values() for piece in pieces} == {
        ("kotlin", "2.4.20"),
        ("openjdk", "25.0.2"),
    }
    assert calls == [None, None, ["openjdk <=25.0.2"], ["openjdk <=25.0.2"]]
