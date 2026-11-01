"""The resolver uses only the fixed wheel registry and verifies every download."""

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from forge.adapters.packages import pypi
from forge.domain.environment import PLATFORMS, Environment
from forge.domain.errors import TooLarge


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    wheels = {platform: f"wheel for {platform}".encode() for platform in PLATFORMS}
    real_client = httpx.AsyncClient

    def download(request: httpx.Request) -> httpx.Response:
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
        assert args[args.index("--python-version") + 1] == "3.14"
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
) -> None:
    asked = Environment.model_validate({"python": {"packages": ["numpy>=2.3"]}})
    async with pypi.PypiResolver().prepare(asked, cap=1024) as prepared:
        assert set(prepared.lock.platforms) == set(PLATFORMS)
        for platform, body in registry.items():
            (wheel,) = prepared.lock.platforms[platform]["python"].pypi
            assert wheel.sha256 == hashlib.sha256(body).hexdigest()
            assert prepared.files[wheel.sha256].read_bytes() == body
        paths = list(prepared.files.values())
    assert all(not path.exists() for path in paths)


async def test_the_environment_cap_is_checked_before_downloading(
    registry: dict[str, bytes],
) -> None:
    asked = Environment.model_validate({"python": {"packages": ["numpy"]}})
    with pytest.raises(TooLarge, match="total"):
        async with pypi.PypiResolver().prepare(asked, cap=1):
            pytest.fail("an oversized environment cannot be prepared")
