"""A primitive's declaration, its `primitive.yaml` (TASK-FORMAT.md section
1.4, the runner's `primitive.schema.json`, version 5): the image it runs
from by digest, whose own entrypoint is the program a step runs, whether it
takes a batch of tests in one run, whether its container may reach the
network, the container's six limits for one run or one item of a batch, the
limits it raises from a number input, and its input and output ports. The
file names neither the primitive nor its version: the repo at the forge is
the name and the tag the version. The compiler reads one for every `use:` a
workflow names and copies what the plan needs of it into each step; the
harness never reads it.

`limits_from` raises a limit from an input, so the container never dies
before the primitive's own per-test limit does: `{time_ms: {input:
time_limit, scale: 1000, add: 2000}}` makes the step's `time_ms` at least
`time_limit * 1000 + 2000`.

Every `file` or `folder` input port says `runs: true` or `runs: false`,
whether the primitive runs what arrives there as a program, and a port
without it is refused (P1): a default of `false` would fail open, a
forgotten mark silently unsealing every task that uses the primitive.
`secret: true` on an input port says the image keeps what arrives there from
any program it runs from another port.
"""

import re
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, PlainValidator, model_validator

from forge.domain.types import FILES, VALUE_TYPES, Type, declared
from forge.domain.workflow_definition import Options
from forge.domain.yaml_models import (
    ANY,
    Handle,
    Line,
    Model,
    Number,
    Problems,
    Retired,
    load_mapping,
    validate,
)

DECLARATION_FILE = "primitive.yaml"

IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
PORT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
LIMIT_NAMES = ("time_ms", "cpu_ms", "memory_mb", "pids", "output_mb", "gpus")
BATCH_SCALED = ("time_ms", "cpu_ms")
"""The limits a batch step sums over its items; the others hold for each
item as they are."""

Flag = Annotated[bool, Field(strict=True)]


def _image(value: object) -> str:
    if not isinstance(value, str) or not IMAGE.match(value):
        raise ValueError(
            "Give the image by its digest, such as ghcr.io/uniconhq/x@sha256:<64 hex digits>."
        )
    return value


Image = Annotated[str, PlainValidator(_image)]
"""A container image named by its content digest."""

Limit = Annotated[int, Field(strict=True, ge=1)]


class Port(Model):
    """One input or output: its type, the options an enum allows, whether
    it may be left out, on an input whether the primitive runs what arrives
    there and whether it keeps it secret from the program it runs, and a
    sentence for the workflow editor saying what it takes or gives back.
    """

    type: Type
    options: Options | None = None
    optional: Flag = False
    runs: Flag | None = None
    secret: Flag = False
    description: Line | None = None

    @model_validator(mode="after")
    def _check(self) -> Port:
        problems = Problems()
        if self.type is Type.ENUM and self.options is None:
            problems.add(("options",), "List the options to choose from.")
        if self.type is not Type.ENUM and self.options is not None:
            problems.add(("options",), "Only an enum has options. Remove them.")
        problems.raise_any()
        return self


class Limits(Model):
    """What one run of the container may use: wall time and CPU time in
    milliseconds, memory in megabytes, processes, output in megabytes, and
    GPU devices.
    """

    time_ms: Limit
    cpu_ms: Limit
    memory_mb: Limit
    pids: Limit
    output_mb: Limit
    gpus: Annotated[int, Field(strict=True, ge=0)]

    def as_mapping(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in LIMIT_NAMES}


class LimitFrom(Model):
    """A limit raised to `input * scale + add` when that is more."""

    input: Handle
    scale: Number = 1
    add: Number = 0

    @model_validator(mode="after")
    def _check(self) -> LimitFrom:
        problems = Problems()
        if self.scale <= 0:
            problems.add(("scale",), "This must be more than 0.")
        if self.add < 0:
            problems.add(("add",), "This must be at least 0.")
        problems.raise_any()
        return self


LimitName = Literal["time_ms", "cpu_ms", "memory_mb", "pids", "output_mb", "gpus"]
Declared = Annotated[Port, BeforeValidator(declared)]


class PrimitiveDeclaration(Model):
    """A `primitive.yaml`. `batch` and `network` default to off, and
    `limits_from` and `inputs` to none. `title` and `description` are for
    the workflow editor, and change nothing a plan holds.
    """

    title: Line | None = None
    description: Line | None = None
    image: Image
    batch: Flag = False
    network: Flag = False
    limits: Limits
    limits_from: dict[LimitName, LimitFrom] = Field(default_factory=dict)
    inputs: dict[str, Declared] = Field(default_factory=dict)
    outputs: dict[str, Declared] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> PrimitiveDeclaration:
        problems = Problems()
        for side, ports in (("inputs", self.inputs), ("outputs", self.outputs)):
            for name in ports:
                if not PORT_NAME.match(name):
                    problems.add(
                        (side, name),
                        "A port's name may use only lower case letters, digits and "
                        "underscores (_), starting with a letter.",
                    )
        for name, port in self.inputs.items():
            if port.type not in VALUE_TYPES:
                problems.add(
                    ("inputs", name, "type"),
                    "An input must be text, a number, true or false, an enum, a file or a folder.",
                )
            if port.type in FILES and port.runs is None:
                problems.add(
                    ("inputs", name),
                    f"{name} is a {port.type} port. Say whether the primitive runs it as a "
                    "program, with runs: true or runs: false.",
                )
            if port.type not in FILES and port.runs is not None:
                problems.add(
                    ("inputs", name, "runs"), "This applies only to a file or folder port."
                )
        for name, port in self.outputs.items():
            if port.runs is not None:
                problems.add(("outputs", name, "runs"), "This applies only to an input.")
            if port.secret:
                problems.add(("outputs", name, "secret"), "This applies only to an input.")
        outcome = self.outputs.get("outcome")
        if outcome is None or outcome.type is not Type.OUTCOME:
            problems.add(
                ("outputs",), "Every primitive must have an output called outcome, of type outcome."
            )
        for name, port in self.outputs.items():
            if port.type is Type.OUTCOME and name != "outcome":
                problems.add(
                    ("outputs", name), "Only the output called outcome can be of type outcome."
                )
        if outcome is not None and outcome.optional:
            problems.add(
                ("outputs", "outcome", "optional"),
                "The outcome cannot be optional: every step gives one.",
            )
        for limit, source in self.limits_from.items():
            given = self.inputs.get(source.input)
            if given is None or given.type is not Type.NUMBER:
                problems.add(
                    ("limits_from", limit, "input"),
                    f"The primitive has no number input called {source.input!r}.",
                )
        problems.raise_any()
        return self


def _values_key(value: object) -> bool:
    return isinstance(value, dict) and "values" in value


def _old_type(value: object) -> bool:
    found = value.get("type") if isinstance(value, dict) else value
    return found == "file[]"


RETIRED = (
    Retired(("name",), "A primitive's name is the name it was made under. Remove this."),
    Retired(("version",), "A primitive's version is set when the version is made. Remove this."),
    Retired(("schema_version",), "This setting is no longer used. Remove this."),
    Retired(("inputs", ANY), "values is now called options.", _values_key),
    Retired(("outputs", ANY), "values is now called options.", _values_key),
    Retired(("inputs", ANY), "The type file[] is now called folder.", _old_type),
    Retired(("outputs", ANY), "The type file[] is now called folder.", _old_type),
)


def parse_primitive(text: bytes | str) -> PrimitiveDeclaration:
    """The `primitive.yaml` in `text`. Raises `InvalidDefinition` listing every
    problem with its YAML path.
    """
    return validate(
        PrimitiveDeclaration, DECLARATION_FILE, load_mapping(DECLARATION_FILE, text), RETIRED
    )
