"""Checked task requirements and the immutable pieces each platform uses."""

import hashlib
import json
from typing import Annotated, Literal

from packaging.requirements import InvalidRequirement, Requirement
from pydantic import Field, PlainValidator, model_validator

from forge.domain.yaml_models import Model

LOCK_PATH = "environment.lock"
PLATFORMS = ("linux-64", "linux-aarch64")


def _requirement(value: object) -> str:
    if isinstance(value, str):
        try:
            parsed = Requirement(value)
            if parsed.url is None and parsed.marker is None:
                return str(parsed)
        except InvalidRequirement:
            pass
    raise ValueError(
        f"{value} is not a package and version. Name a package from PyPI, such as numpy==2.3.*"
    )


RequirementText = Annotated[str, PlainValidator(_requirement)]


class PythonEnvironment(Model):
    @model_validator(mode="before")
    @classmethod
    def stage_one(cls, value: object) -> object:
        if isinstance(value, dict) and set(value) - {"packages"}:
            raise ValueError("Only Python packages from PyPI are supported yet")
        return value

    packages: list[RequirementText] = Field(default_factory=list)


class Environment(Model):
    @model_validator(mode="before")
    @classmethod
    def stage_one(cls, value: object) -> object:
        if isinstance(value, dict) and set(value) - {"python"}:
            raise ValueError("Only Python packages from PyPI are supported yet")
        return value

    python: PythonEnvironment | None = None


class Wheel(Model):
    name: str
    version: str
    file_name: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size: Annotated[int, Field(ge=0)]


class LanguageLock(Model):
    asked: dict[str, object]
    picked: str
    fingerprint: str
    pypi: list[Wheel] = Field(default_factory=list)


class EnvironmentLock(Model):
    version: Literal[2] = 2
    prefix: Literal["/env"] = "/env"
    platforms: dict[str, dict[str, LanguageLock]]

    def to_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()


def fingerprint(hashes: list[str], prefix: str = "/env") -> str:
    return hashlib.sha256(
        json.dumps([prefix, *sorted(hashes)], separators=(",", ":")).encode()
    ).hexdigest()
