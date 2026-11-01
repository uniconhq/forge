"""Checked task requirements and the immutable pieces each platform uses."""

import hashlib
import json
import re
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


def _version(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        return value
    raise ValueError("Name one version, such as 3.13")


VersionText = Annotated[str, PlainValidator(_version)]
CONDA_ENTRY = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)(?:(?:==?| +)([0-9]+(?:\.[0-9]+)*))?")


def _conda(value: object) -> str:
    if isinstance(value, str) and (found := CONDA_ENTRY.fullmatch(value)):
        name, version = found.groups()
        return name.lower() + (f"={version}" if version else "")
    raise ValueError(
        "A conda entry is a package name and optional version, such as libboost-headers=1.89"
    )


CondaText = Annotated[str, PlainValidator(_conda)]


class LanguageEnvironment(Model):
    version: VersionText | None = None
    conda: list[CondaText] = Field(default_factory=list)


class PythonEnvironment(LanguageEnvironment):
    packages: list[RequirementText] = Field(default_factory=list)


class Environment(Model):
    c: LanguageEnvironment | None = None
    cpp: LanguageEnvironment | None = None
    java: LanguageEnvironment | None = None
    python: PythonEnvironment | None = None

    @model_validator(mode="before")
    @classmethod
    def language_keys(cls, value: object) -> object:
        if isinstance(value, dict):
            if "ipynb" in value:
                raise ValueError("Notebooks use the python entry")
            if any(
                isinstance(value.get(name), dict) and "packages" in value[name]
                for name in ("c", "cpp", "java")
            ):
                raise ValueError(
                    "Only Python takes `packages` yet; use `conda` for conda-forge packages"
                )
        return value

    @model_validator(mode="after")
    def agreement(self) -> Environment:
        if (
            self.c
            and self.cpp
            and self.c.version
            and self.cpp.version
            and self.c.version != self.cpp.version
        ):
            raise ValueError(
                f"C version {self.c.version} and C++ version {self.cpp.version} must agree"
            )
        if self.python:
            pypi = {
                re.sub(r"[-_.]+", "-", Requirement(each).name).lower()
                for each in self.python.packages
            }
            conda = {
                re.sub(r"[-_.]+", "-", each.split("=", 1)[0]).lower() for each in self.python.conda
            }
            if overlap := pypi & conda:
                raise ValueError(
                    f"{', '.join(sorted(overlap))} is named in both "
                    "python.packages and python.conda"
                )
        return self

    def requirements(self) -> dict[str, dict[str, object]]:
        return {
            name: entry.model_dump(exclude_none=True, exclude_defaults=True)
            for name in ("c", "cpp", "java", "python")
            if (entry := getattr(self, name)) is not None
        }

    def covering(self, languages: set[str]) -> Environment:
        given = self.requirements()
        return Environment.model_validate(
            {name: given.get(name, {}) for name in languages | given.keys()}
        )


class Wheel(Model):
    name: str
    version: str
    file_name: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size: Annotated[int, Field(ge=0)]


class CondaPiece(Model):
    name: str
    version: str
    build: str
    subdir: str
    file_name: str
    url: str
    size: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    record: dict[str, object]


class LanguageLock(Model):
    asked: dict[str, object]
    picked: str
    fingerprint: str
    conda: list[CondaPiece] = Field(default_factory=list)
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
