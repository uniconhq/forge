"""Checked task requirements and the immutable pieces each platform uses."""

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Annotated, Literal

from packaging.requirements import InvalidRequirement, Requirement
from pydantic import ConfigDict, Field, PlainValidator, RootModel, model_validator

from forge.domain.environment.tools import LANGUAGES, NAMES, SOURCES, TOOLS, VERSIONS
from forge.domain.yaml_models import Model, Problems, joined

LOCK_PATH = "environment.lock"
PLATFORMS = ("linux-64", "linux-aarch64")
PreparationStatus = Literal["preparing", "ready", "refused", "replaced"]
"""Where a task's background package preparation stands."""
PIECES = ("conda", "pypi", "maven", "npm")
"""A language lock's lists of pieces, each fetched by its SHA-256."""


def _requirement(value: str) -> str | None:
    try:
        parsed = Requirement(value)
    except InvalidRequirement:
        return None
    return str(parsed) if parsed.url is None and parsed.marker is None else None


def _version(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        return value
    raise ValueError("Name one version, such as 3.13")


VersionText = Annotated[str, PlainValidator(_version)]
CONDA_ENTRY = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)(?:(?:==?| +)([0-9]+(?:\.[0-9]+)*))?")
R_ENTRY = re.compile(r"([A-Za-z][A-Za-z0-9.]*)(?:(?:==?| +)([0-9]+(?:\.[0-9]+)*))?")
MAVEN_ENTRY = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_.-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*:[0-9][A-Za-z0-9._-]*\+?"
)
NPM_NAME = r"(?:@[A-Za-z0-9_~-][A-Za-z0-9_.~-]*/)?[A-Za-z0-9_~-][A-Za-z0-9_.~-]*"
"""An npm package's name, as exec reads one where npm's lock places it:
legacy names with capitals among them, and never `.` or `..`."""
NPM_ENTRY = re.compile(rf"({NPM_NAME})(?:@([0-9A-Za-z.^~<>=*+| -]*[0-9A-Za-z*]))?")


def _conda(value: object) -> str:
    if isinstance(value, str) and (found := CONDA_ENTRY.fullmatch(value)):
        name, version = found.groups()
        return name.lower() + (f"={version}" if version else "")
    raise ValueError(
        "A conda entry is a package name and optional version, such as libboost-headers=1.89"
    )


CondaText = Annotated[str, PlainValidator(_conda)]


def _r(value: str) -> str | None:
    if found := R_ENTRY.fullmatch(value):
        name, version = found.groups()
        return name.lower() + (f"={version}" if version else "")
    return None


def _maven(value: str) -> str | None:
    return value if MAVEN_ENTRY.fullmatch(value) and len(value) <= 300 else None


def _npm(value: str) -> str | None:
    if (found := NPM_ENTRY.fullmatch(value)) and len(found[1]) <= 214:
        return value
    return None


PACKAGES = {
    "pypi": (
        _requirement,
        "is not a package and version. Name a package from PyPI, such as numpy==2.3.*",
    ),
    "r": (
        _r,
        "is not an R package and version. Name a package from CRAN, such as testthat "
        "or ggplot2=3.5",
    ),
    "maven": (
        _maven,
        "is not a Maven package. Name its group, artifact and version, such as "
        "org.junit.jupiter:junit-jupiter:5.+",
    ),
    "npm": (
        _npm,
        "is not an npm package and version. Name a package from npm, such as jest@30 or chai",
    ),
}
"""How each source reads one `packages` entry, giving its usual form or None,
and what is said of an entry it cannot read."""


class LanguageEnvironment(Model):
    version: VersionText | None = None
    conda: list[CondaText] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list)


def _named(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


class Environment(RootModel[dict[str, LanguageEnvironment]]):
    """A task's `environment`: each language it names, with the version it
    picks from its list, its conda-forge packages and its own packages."""

    model_config = ConfigDict(frozen=True)
    root: dict[str, LanguageEnvironment] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def notebooks(cls, value: object) -> object:
        if isinstance(value, dict) and "ipynb" in value:
            raise ValueError("Notebooks use the python entry")
        return value

    @model_validator(mode="after")
    def agreement(self) -> Environment:
        problems = Problems()
        for name, entry in self.root.items():
            if name not in TOOLS:
                problems.add(
                    (name,), f"Unicon has no {name} toolchain. Name one of {joined(LANGUAGES)}."
                )
                continue
            offered = VERSIONS[name]
            if entry.version is not None and entry.version not in offered:
                problems.add(
                    (name, "version"),
                    f"{NAMES[name]} offers {' or '.join(offered)}. Name one of them, or none "
                    "for the newest.",
                )
            source = SOURCES.get(name)
            if source is None:
                if entry.packages:
                    problems.add(
                        (name, "packages"),
                        f"{NAMES[name]} takes no `packages` yet; use `conda` for conda-forge "
                        "packages",
                    )
                continue
            read, refusal = PACKAGES[source]
            for index, each in enumerate(entry.packages):
                if read(each) is None:
                    problems.add((name, "packages", index), f"{each} {refusal}")
        c, cpp = self.root.get("c"), self.root.get("cpp")
        if c and cpp and c.version and cpp.version and c.version != cpp.version:
            problems.add(
                ("cpp", "version"),
                f"C version {c.version} and C++ version {cpp.version} must agree",
            )
        for name, prefix in (("python", ""), ("r", "r-")):
            if (named := self.root.get(name)) is None:
                continue
            read = PACKAGES[SOURCES[name]][0]
            given = {
                _named(re.split(r"[^A-Za-z0-9_.-]", usual, maxsplit=1)[0])
                for each in named.packages
                if (usual := read(each)) is not None
            }
            conda = {_named(each.split("=", 1)[0]).removeprefix(prefix) for each in named.conda}
            if overlap := given & conda:
                problems.add(
                    (name,),
                    f"{', '.join(sorted(overlap))} is named in both "
                    f"{name}.packages and {name}.conda",
                )
        problems.raise_any()
        return self

    def requirements(self) -> dict[str, dict[str, object]]:
        """Each named language's entry in its usual form, as the lock records
        what was asked."""
        found: dict[str, dict[str, object]] = {}
        for name, entry in self.root.items():
            asked = entry.model_dump(exclude_none=True, exclude_defaults=True)
            if entry.packages:
                read = PACKAGES[SOURCES[name]][0]
                asked["packages"] = [read(each) for each in entry.packages]
            found[name] = asked
        return found

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


class MavenJar(Model):
    """One jar from Maven Central, placed as `file_name` in `/env/lib/java/`."""

    coord: str
    file_name: str
    url: str
    size: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class NpmPackage(Model):
    """One npm tarball, unpacked at `path` below `/env/lib`, as
    `node_modules/jest` or `node_modules/a/node_modules/b`."""

    path: str
    name: str
    version: str
    url: str
    size: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class LanguageLock(Model):
    asked: dict[str, object]
    picked: str
    fingerprint: str
    conda: list[CondaPiece] = Field(default_factory=list)
    pypi: list[Wheel] = Field(default_factory=list)
    maven: list[MavenJar] = Field(default_factory=list)
    npm: list[NpmPackage] = Field(default_factory=list)


class EnvironmentLock(Model):
    version: Literal[3] = 3
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


def pieces_fingerprint(
    conda: Sequence[CondaPiece],
    pypi: Sequence[Wheel] = (),
    maven: Sequence[MavenJar] = (),
    npm: Sequence[NpmPackage] = (),
) -> str:
    """A language folder's name: every piece's SHA-256, with each jar's file
    name and each npm package's place in the tree, since the same files laid
    out otherwise make another folder."""
    return fingerprint(
        [piece.sha256 for piece in conda]
        + [wheel.sha256 for wheel in pypi]
        + [f"{jar.sha256} {jar.file_name}" for jar in maven]
        + [f"{package.sha256} {package.path}" for package in npm]
    )
