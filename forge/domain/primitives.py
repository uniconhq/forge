"""A primitive's declaration, its `primitive.yaml` (TASK-FORMAT.md section
1.4, the runner's `primitive.schema.json`, versions 5 and 6): the contract
version it speaks, the image it runs from by digest, whose own entrypoint is
the program a step runs, whether it takes a batch of tests in one run,
whether its container may reach the network, the container's six limits for
one run or one item of a batch, the limits it raises from a number input,
and its input and output ports. The file names neither the primitive nor
its version: the repo at the forge is the name and the tag the version. The
compiler reads one for every `use:` a workflow names and copies what the
plan needs of it into each step; the harness never reads it.

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

The `outcome` port may say, under `outcomes`, what each outcome the step can
report means, for the workflow editor's drawer: `accepted: the source
compiles`, `compile_error: it does not compile` (P2). The keys are the words
a step may report, and the port is the only one that takes it. Like a title
or a description, it changes nothing a plan holds.

`schema_version: 6`, 5 when left out, is the contract the stage 4 primitives
speak, and only it has these, each read and kept here; what they add to a
step's outputs is worked out per step, since it depends on what is wired in:

- a `named_files` input, with `runs`, whose `names`, when given, are the
  names its files must have;
- a `programs` input, named `programs`, declaring under `inputs` what each
  program it runs side by side takes, `program` among them, and under
  `outputs` what it gives of each; and a `links` input beside it, the pipes
  between them;
- `better`, `at_least` and `at_most` on a `number` output;
- `outputs_from`: `manifest`, the reports of the `program.yaml` given on the
  `manifest` input are outputs too, or `programs`, each program's outputs
  and reports are, as `<name>.<output>`;
- a limit from the `sum` or the `max` of a number input over the programs,
  `{sum: programs.memory_limit, add: 512}`, in place of `input`;
- `cache: true`, the primitive reads the task's files through the machine's
  cache.
"""

import math
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, PlainValidator, model_validator

from forge.domain.scoring import Better
from forge.domain.types import FILES, PORT_TYPES, VALUE_TYPES, Type, declared
from forge.domain.workflow_definition import Options
from forge.domain.yaml_models import (
    ANY,
    Handle,
    Line,
    Model,
    Number,
    Path,
    Problems,
    Retired,
    already_listed,
    exact_number,
    load_mapping,
    validate,
)

DECLARATION_FILE = "primitive.yaml"

IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
PORT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
FILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}")
"""A name a program sees a file under, the contract's `file_name`: letters,
digits, `_`, `.` and `-`, not starting with a `.`, so never `.`, `..` or a
path."""
PROGRAM_INPUT = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
"""`<port>.<input>`, the number input of a programs port a limit's `sum` or
`max` reads."""
PROGRAMS = "programs"
"""The name a programs port has, and the `outputs_from` that comes with it."""
MANIFEST = "manifest"
"""The file input a program's `program.yaml` is given on, under either
`outputs_from`."""
PROGRAM_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
"""A program's name in a programs value, the contract's `program_name`."""
WALL_CLOCK = "wall_clock"
"""What `stopped_by` says of the wall clock, so no program's name."""
MAX_PROGRAMS = 8
LINK = re.compile(f"({PROGRAM_NAME.pattern}) -> ({PROGRAM_NAME.pattern})")
"""One link of a links value, `<from> -> <to>`."""
MAX_LINKS = 56
LIMIT_NAMES = ("time_ms", "cpu_ms", "memory_mb", "pids", "output_mb", "gpus")
STEP_OUTCOMES = (
    "accepted",
    "wrong_answer",
    "time_limit",
    "memory_limit",
    "output_limit",
    "runtime_error",
    "compile_error",
)
"""The outcomes a step may report, in the runner's order: `skipped` is the
harness's word for a test it did not run, and a primitive that could not
work says so with `error`, which becomes a `system_error`."""
BATCH_SCALED = ("time_ms", "cpu_ms")
"""The limits a batch step sums over its items; the others hold for each
item as they are."""
RUNS_TYPES = FILES | {Type.NAMED_FILES}
"""The input types that say whether the primitive runs what arrives there."""
NEEDS_6 = "This needs schema_version: 6."

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


def _version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (5, 6):
        raise ValueError("This must be 5 or 6.")
    return value


Version = Annotated[int, PlainValidator(_version)]
"""The contract version a primitive speaks, 5 or 6."""


def _outcome_word(value: object) -> str:
    if not isinstance(value, str) or value not in STEP_OUTCOMES:
        raise ValueError(
            f"{value} is not an outcome a step can report. Use "
            f"{', '.join(STEP_OUTCOMES[:-1])} or {STEP_OUTCOMES[-1]}."
        )
    return value


OutcomeWord = Annotated[str, PlainValidator(_outcome_word)]
"""An outcome a step may report, one of `STEP_OUTCOMES`."""


def _file_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not value:
        raise ValueError("Give at least one name.")
    found: list[str] = []
    for name in value:
        if not isinstance(name, str) or not FILE_NAME.fullmatch(name):
            raise ValueError(
                f"{name!r} cannot be a file's name. Use at most 255 letters, digits, _, . "
                "and -, not starting with a ., so never a path."
            )
        if name in found:
            raise ValueError(already_listed(name))
        found.append(name)
    return tuple(found)


FileNames = Annotated[tuple[str, ...], PlainValidator(_file_names)]
"""The names a `named_files` port's files must have, each a `FILE_NAME`."""


def _program_input(value: object) -> str:
    if not isinstance(value, str) or not PROGRAM_INPUT.fullmatch(value):
        raise ValueError(
            "Give a programs port and one of its number inputs, such as programs.memory_limit."
        )
    return value


ProgramInput = Annotated[str, PlainValidator(_program_input)]
"""`<port>.<input>`, a programs port and one of its number inputs."""

Declared = Annotated["Port", BeforeValidator(declared)]


class Port(Model):
    """One input or output: its type, the options an enum allows, the names
    a `named_files` port's files must have, whether it may be left out, on
    an input whether the primitive runs what arrives there and whether it
    keeps it secret from the program it runs, on a `number` output which way
    is better and the bounds every value lies within, both inclusive, on a
    `programs` port the `inputs` each program takes and the `outputs` the
    primitive gives of each, a sentence for the workflow editor saying what
    it takes or gives back, and, on the `outcome` port, what each outcome
    the step can report means, in the order the file gives them.
    """

    type: Type
    options: Options | None = None
    names: FileNames | None = None
    optional: Flag = False
    runs: Flag | None = None
    secret: Flag = False
    better: Better | None = None
    at_least: Number | None = None
    at_most: Number | None = None
    inputs: dict[str, Declared] | None = None
    outputs: dict[str, Declared] | None = None
    description: Line | None = None
    outcomes: dict[OutcomeWord, Line] | None = None

    @model_validator(mode="after")
    def _check(self) -> Port:
        problems = Problems()
        if self.outcomes is not None and not self.outcomes:
            problems.add(
                ("outcomes",),
                "Give each outcome the step can report with a line saying when, such as "
                "accepted: the source compiles.",
            )
        if self.type is Type.ENUM and self.options is None:
            problems.add(("options",), "List the options to choose from.")
        if self.type is not Type.ENUM and self.options is not None:
            problems.add(("options",), "Only an enum has options. Remove them.")
        if self.type is not Type.NAMED_FILES and self.names is not None:
            problems.add(("names",), "Only a named_files port has names. Remove them.")
        for key in ("inputs", "outputs"):
            if self.type is Type.PROGRAMS and getattr(self, key) is None:
                problems.add((key,), f"A programs port declares its programs' {key}.")
            if self.type is not Type.PROGRAMS and getattr(self, key) is not None:
                problems.add((key,), "Only a programs port has this. Remove it.")
        for key in ("better", "at_least", "at_most"):
            if self.type is not Type.NUMBER and getattr(self, key) is not None:
                problems.add(
                    (key,), "Only a number says which way is better or its bounds. Remove this."
                )
        if self.at_least is not None and self.at_most is not None and self.at_least > self.at_most:
            problems.add(
                ("at_most",),
                f"at_least {self.at_least} is above at_most {self.at_most}, so no value fits.",
            )
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
    """A limit raised to `n * scale + add` when that is more, where `n` is
    one of three: the number `input`, or in contract 6 the `sum` or the
    `max` of a number input over every program of a programs port, each
    `<port>.<input>`.
    """

    input: Handle | None = None
    sum: ProgramInput | None = None
    max: ProgramInput | None = None
    scale: Number = 1
    add: Number = 0

    @property
    def program_input(self) -> str | None:
        """The input of each program a `sum` or `max` reads, none for an
        `input`.
        """
        over = self.sum if self.sum is not None else self.max
        return over.partition(".")[2] if over is not None else None

    @model_validator(mode="after")
    def _check(self) -> LimitFrom:
        problems = Problems()
        if [self.input, self.sum, self.max].count(None) != 2:
            problems.add((), "Give exactly one of input, sum and max.")
        if self.scale <= 0:
            problems.add(("scale",), "This must be more than 0.")
        if self.add < 0:
            problems.add(("add",), "This must be at least 0.")
        problems.raise_any()
        return self


LimitName = Literal["time_ms", "cpu_ms", "memory_mb", "pids", "output_mb", "gpus"]


class PrimitiveDeclaration(Model):
    """A `primitive.yaml`. `schema_version` is 5 when left out; `batch`,
    `network` and `cache` default to off, and `limits_from`, `outputs_from`
    and `inputs` to none. `title` and `description` are for the workflow
    editor, and change nothing a plan holds.
    """

    schema_version: Version = 5
    title: Line | None = None
    description: Line | None = None
    image: Image
    batch: Flag = False
    network: Flag = False
    cache: Flag = False
    limits: Limits
    limits_from: dict[LimitName, LimitFrom] = Field(default_factory=dict)
    outputs_from: Literal["manifest", "programs"] | None = None
    inputs: dict[str, Declared] = Field(default_factory=dict)
    outputs: dict[str, Declared] = Field(default_factory=dict)

    def item_limits(
        self,
        numbers: Mapping[str, int | Decimal],
        programs: Mapping[str, Mapping[str, int | Decimal]] | None = None,
    ) -> dict[str, int]:
        """One run's limits, or one batch item's, each raised by
        `limits_from` to `n * scale + add`, rounded up, when that is more:
        `n` the number `numbers` gives the `input`, or the sum or the
        largest of the number each of `programs` gives the input a `sum` or
        `max` names, `{<program>: {<input>: <number>}}`. A number not given
        raises nothing. A batch's time is the sum of its items', which the
        planner adds up (`BATCH_SCALED`).
        """
        limits = self.limits.as_mapping()
        for name, source in self.limits_from.items():
            key = source.program_input
            if source.input is not None:
                n = numbers.get(source.input)
            elif key is not None:
                given = [
                    exact_number(each[key]) for each in (programs or {}).values() if key in each
                ]
                if not given:
                    continue
                n = sum(given) if source.sum is not None else max(given)
            else:
                continue
            if n is None:
                continue
            wanted = exact_number(n) * source.scale + source.add
            limits[name] = max(limits[name], math.ceil(wanted))
        return limits

    @model_validator(mode="after")
    def _check(self) -> PrimitiveDeclaration:
        problems = Problems()
        six = self.schema_version == 6
        _input_problems(problems, ("inputs",), self.inputs, six=six, inner=False)
        _output_problems(problems, ("outputs",), self.outputs, six=six, inner=False)
        outcome = self.outputs.get("outcome")
        if outcome is None or outcome.type is not Type.OUTCOME:
            problems.add(
                ("outputs",), "Every primitive must have an output called outcome, of type outcome."
            )
        if outcome is not None and outcome.optional:
            problems.add(
                ("outputs", "outcome", "optional"),
                "The outcome cannot be optional: every step gives one.",
            )
        if not six:
            for key in ("cache", "outputs_from"):
                if key in self.model_fields_set:
                    problems.add((key,), NEEDS_6)
        programs = {name for name, port in self.inputs.items() if port.type is Type.PROGRAMS}
        for name in sorted(programs - {PROGRAMS}):
            problems.add(("inputs", name), "A programs port must be called programs.")
        if programs and self.outputs_from != PROGRAMS:
            problems.add(
                ("outputs_from",), "A primitive with a programs port needs outputs_from: programs."
            )
        for name, port in self.inputs.items():
            if port.type is Type.LINKS and not programs:
                problems.add(
                    ("inputs", name), "Links need a programs port whose programs they link."
                )
        manifest_at: tuple[Path, Mapping[str, Port]] | None = None
        if self.outputs_from == "manifest":
            manifest_at = (("inputs",), self.inputs)
        elif self.outputs_from == PROGRAMS:
            held = self.inputs.get(PROGRAMS)
            if held is None or held.type is not Type.PROGRAMS:
                problems.add(
                    ("outputs_from",),
                    "outputs_from: programs needs an input called programs, of type programs.",
                )
            else:
                manifest_at = (("inputs", PROGRAMS, "inputs"), held.inputs or {})
        if manifest_at is not None:
            where, ports = manifest_at
            manifest = ports.get("manifest")
            if manifest is None or manifest.type is not Type.FILE:
                problems.add(
                    (*where, "manifest"),
                    f"outputs_from: {self.outputs_from} needs a file input called manifest, "
                    "where the program.yaml is given.",
                )
        for limit, source in self.limits_from.items():
            if source.input is not None:
                given = self.inputs.get(source.input)
                if given is None or given.type is not Type.NUMBER:
                    problems.add(
                        ("limits_from", limit, "input"),
                        f"The primitive has no number input called {source.input!r}.",
                    )
            for over, named in (("sum", source.sum), ("max", source.max)):
                if named is None:
                    continue
                if not six:
                    problems.add(("limits_from", limit, over), NEEDS_6)
                    continue
                port_name, _, number = named.partition(".")
                over_port = self.inputs.get(port_name)
                if over_port is None or over_port.type is not Type.PROGRAMS:
                    problems.add(
                        ("limits_from", limit, over), f"{port_name} is not a programs port."
                    )
                    continue
                taken = (over_port.inputs or {}).get(number)
                if taken is None or taken.type is not Type.NUMBER:
                    problems.add(
                        ("limits_from", limit, over),
                        f"{port_name} has no number input called {number!r}.",
                    )
        problems.raise_any()
        return self


def _names(problems: Problems, where: Path, ports: Mapping[str, Port]) -> None:
    for name in ports:
        if not PORT_NAME.match(name):
            problems.add(
                (*where, name),
                "A port's name may use only lower case letters, digits and underscores (_), "
                "starting with a letter.",
            )


def _outcomes(problems: Problems, where: Path, ports: Mapping[str, Port]) -> None:
    for name, port in ports.items():
        if port.outcomes is not None and port.type is not Type.OUTCOME:
            problems.add(
                (*where, name, "outcomes"),
                "Only the outcome port says what each outcome means. Remove this.",
            )


def _input_problems(
    problems: Problems, where: Path, ports: Mapping[str, Port], *, six: bool, inner: bool
) -> None:
    """What is wrong with a declaration's input ports at `where`, or a
    programs port's (`inner`), which take neither programs nor links.
    """
    _names(problems, where, ports)
    _outcomes(problems, where, ports)
    for name, port in ports.items():
        at = (*where, name)
        if port.type in PORT_TYPES and not six:
            problems.add((*at, "type"), NEEDS_6)
        elif inner and port.type in (Type.PROGRAMS, Type.LINKS):
            problems.add((*at, "type"), "A program's input cannot be programs or links.")
        elif port.type not in VALUE_TYPES and port.type not in PORT_TYPES:
            problems.add(
                (*at, "type"),
                "An input must be text, a number, true or false, an enum, a file, a folder, "
                "named files, programs or links."
                if six
                else "An input must be text, a number, true or false, an enum, a file or a folder.",
            )
        if port.type in RUNS_TYPES and port.runs is None:
            problems.add(
                at,
                f"{name} is a {port.type} port. Say whether the primitive runs it as a "
                "program, with runs: true or runs: false.",
            )
        if port.type not in RUNS_TYPES and port.runs is not None:
            problems.add((*at, "runs"), "This applies only to a file, folder or named_files port.")
        for key in ("better", "at_least", "at_most"):
            if getattr(port, key) is not None:
                problems.add((*at, key), "This applies only to an output.")
        if port.type is Type.PROGRAMS:
            inputs, outputs = port.inputs or {}, port.outputs or {}
            _input_problems(problems, (*at, "inputs"), inputs, six=six, inner=True)
            _output_problems(problems, (*at, "outputs"), outputs, six=six, inner=True)
            program = inputs.get("program")
            if program is None or program.type is not Type.FILE or program.runs is not True:
                problems.add(
                    (*at, "inputs", "program"),
                    "A programs port's programs each take a program, a file input called "
                    "program with runs: true.",
                )


def _output_problems(
    problems: Problems, where: Path, ports: Mapping[str, Port], *, six: bool, inner: bool
) -> None:
    """What is wrong with a declaration's output ports at `where`, or what
    a programs port gives of each program (`inner`), which has no outcome.
    """
    _names(problems, where, ports)
    _outcomes(problems, where, ports)
    for name, port in ports.items():
        at = (*where, name)
        if port.runs is not None:
            problems.add((*at, "runs"), "This applies only to an input.")
        if port.secret:
            problems.add((*at, "secret"), "This applies only to an input.")
        if port.type in PORT_TYPES:
            problems.add(
                (*at, "type"),
                "An output must be text, a number, true or false, an enum, a file or a folder.",
            )
        elif port.type is Type.OUTCOME and (inner or name != "outcome"):
            problems.add(at, "Only the output called outcome can be of type outcome.")
        if not six:
            for key in ("better", "at_least", "at_most"):
                if getattr(port, key) is not None:
                    problems.add((*at, key), NEEDS_6)


def _values_key(value: object) -> bool:
    return isinstance(value, dict) and "values" in value


def _old_type(value: object) -> bool:
    found = value.get("type") if isinstance(value, dict) else value
    return found == "file[]"


RETIRED = (
    Retired(("name",), "A primitive's name is the name it was made under. Remove this."),
    Retired(("version",), "A primitive's version is set when the version is made. Remove this."),
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
