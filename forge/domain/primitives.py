"""A primitive's declaration, its `primitive.yaml` (the runner's
`primitive.schema.json`, version 3): its name and version, the image it runs
from by digest and the program the container runs, whether it takes a batch
of inputs in one run, the container's limits for one run or one item of a
batch, the limits it raises from a numeric input, and its inputs and outputs
with their types. The compiler reads one for every `use:` a workflow names
and writes what it says into each step of the plan; the harness never reads
it.

`limits_from` raises a limit from an input, so the container never dies
before the primitive's own per-test limit does: `{time_ms: {input:
time_limit, scale: 1000, add: 2000}}` makes the step's `time_ms` at least
`time_limit * 1000 + 2000`.
"""

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, PlainValidator, model_validator

from forge.domain.yaml_models import (
    Handle,
    Model,
    Number,
    Problems,
    load_mapping,
    validate,
)

DECLARATION_FILE = "primitive.yaml"
SCHEMA_VERSION = 3

IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
LIMIT_NAMES = ("time_ms", "cpu_ms", "memory_mb", "pids", "output_mb")
BATCH_SCALED = ("time_ms", "cpu_ms")
"""The limits a batch step multiplies by its number of items; the other
three hold for each item as they are."""


class PortType(StrEnum):
    """The type of one input or output of a primitive. `outcome` is a verdict
    outcome, one of the list the runner's verdict schema fixes.
    """

    FILE = "file"
    FILES = "file[]"
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ENUM = "enum"
    OUTCOME = "outcome"


def _image(value: object) -> str:
    if not isinstance(value, str) or not IMAGE.match(value):
        raise ValueError("Must be an image by digest, such as ghcr.io/uniconhq/x@sha256:<64 hex>.")
    return value


Image = Annotated[str, PlainValidator(_image)]
"""A container image named by its content digest."""

Limit = Annotated[int, Field(strict=True, ge=1)]


class Port(Model):
    """One input or output: its type, the values an `enum` allows, and
    whether it may be left out.
    """

    type: PortType
    values: tuple[str, ...] | None = Field(default=None, min_length=1)
    optional: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def _check(self) -> Port:
        problems = Problems()
        if self.type is PortType.ENUM and self.values is None:
            problems.add(("values",), "An enum lists the values it allows.")
        if self.type is not PortType.ENUM and self.values is not None:
            problems.add(("values",), "Applies only to an enum.")
        problems.raise_any()
        return self


class Limits(Model):
    """What one run of the container may use: wall time and CPU time in
    milliseconds, memory in megabytes, processes, and output in megabytes.
    """

    time_ms: Limit
    cpu_ms: Limit
    memory_mb: Limit
    pids: Limit
    output_mb: Limit

    def as_mapping(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in LIMIT_NAMES}


class LimitFrom(Model):
    """A limit raised to `input * scale + add` when that is more."""

    input: Handle
    scale: Number = 1
    add: Number = 0


LimitName = Literal["time_ms", "cpu_ms", "memory_mb", "pids", "output_mb"]


class PrimitiveDeclaration(Model):
    """A `primitive.yaml`. `name` is `unicon/<name>`, the platform's own org
    being the only one that holds primitives; `schema_version` may be given
    and is then 3.
    """

    schema_version: Literal[3] | None = None
    name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*$")]
    version: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    image: Image
    entrypoint: tuple[Annotated[str, Field(min_length=1)], ...] = Field(min_length=1)
    batch: Annotated[bool, Field(strict=True)] = False
    limits: Limits
    limits_from: dict[LimitName, LimitFrom] = Field(default_factory=dict)
    inputs: dict[Handle, Port] = Field(default_factory=dict)
    outputs: dict[Handle, Port] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> PrimitiveDeclaration:
        problems = Problems()
        for limit, source in self.limits_from.items():
            given = self.inputs.get(source.input)
            if given is None or given.type is not PortType.NUMBER:
                problems.add(
                    ("limits_from", limit, "input"),
                    f"{source.input!r} is not a number input of the primitive.",
                )
        problems.raise_any()
        return self

    @property
    def short_name(self) -> str:
        """The name without its owner, as a plan names the step's primitive."""
        return self.name.partition("/")[2]


def parse_primitive(text: bytes | str) -> PrimitiveDeclaration:
    """The `primitive.yaml` in `text`. Raises `InvalidDefinition` listing every
    problem with its YAML path.
    """
    return validate(PrimitiveDeclaration, DECLARATION_FILE, load_mapping(DECLARATION_FILE, text))
