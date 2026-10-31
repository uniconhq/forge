"""A primitive's declaration, its `primitive.yaml` (the runner's
`primitive.schema.json`, version 6): the contract version it speaks, 6, the
image it runs from by digest, whether it takes many runs in one container,
whether its container may reach the network, the container's six limits for
one run or one item of a batch, the limits it raises from a number input,
and its input and output ports. The file names neither the primitive nor
its version: the repo at the forge is the name and the tag the version. The
planner reads one for every `use:` a workflow names and copies what the
plan needs of it into each step.

`limits_from` raises a limit from an input, so the container never dies
before the primitive's own per-run limit does: `{time_ms: {input:
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

A batch primitive may say what it gives the same for every item, from work
it does once before them, such as compiling the source it was given: an
output with `once: true`, the outcome port with `once: [compile_error]`,
each of those among its `outcomes` when it lists them, and the time that
work takes as `limits.once_time_ms`, which the planner adds to the step's
time once, where the rest is summed over its items.

`schema_version: 6` is required, the one contract a primitive speaks. Each
of these is read and kept here; what they add to a step's outputs is worked
out per step, since it depends on what is wired in:

- a `named_files` input, with `runs`, whose `names`, when given, are the
  names its files must have;
- a `programs` input, named `programs`, declaring under `inputs` what each
  program it runs side by side takes, `source` among them, the folder it
  compiles and runs, and under `outputs` what it gives of each; and a
  `links` input beside it, the pipes between them;
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
from collections.abc import Collection, Mapping
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, PlainValidator, ValidationError, model_validator

from forge.domain.blocks.numbers import Better
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
    fail,
    load_mapping,
    problems_of,
    validate,
)

DECLARATION_FILE = "primitive.yaml"

IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
PORT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
FORM_AT_TOP = ("outputs_from", "limits", "limits_from", "inputs", "outputs")
"""The keys a form holds. On a declaration with forms they are refused at the top."""
FILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}")
"""A name a program sees a file under, the contract's `file_name`: letters,
digits, `_`, `.` and `-`, not starting with a `.`, so never `.`, `..` or a
path."""
PROGRAM_INPUT = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
"""`<port>.<input>`, the number input of a programs port a limit's `sum` or
`max` reads."""
PROGRAMS = "programs"
"""The name a programs port has, and the `outputs_from` that comes with it."""
SOURCE = "source"
"""The folder a programs port's every program is compiled and run from."""
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
"""The outcomes a step may report, in the runner's order: `skipped` is
Unicon's word for a run it did not run, and a primitive that could not
work says so with `error`, which becomes a `system_error`."""
BATCH_SCALED = ("time_ms", "cpu_ms")
"""The limits a batch step sums over its items; the others hold for each
item as they are."""
RUNS_TYPES = FILES | {Type.NAMED_FILES}
"""The input types that say whether the primitive runs what arrives there."""

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
    if isinstance(value, bool) or not isinstance(value, int) or value != 6:
        raise ValueError("This must be 6.")
    return value


Version = Annotated[int, PlainValidator(_version)]
"""The contract version a primitive speaks, 6."""


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
    the step can report means, in the order the file gives them. `once` on
    an output says a batch step gives it the same for every item, and on
    the `outcome` port lists the outcomes it gives that way.
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
    once: Flag | tuple[OutcomeWord, ...] | None = None

    @property
    def once_outcomes(self) -> tuple[str, ...]:
        """The outcomes the `outcome` port says a batch step gives the same
        for every item, none on any other port."""
        return self.once if isinstance(self.once, tuple) else ()

    @model_validator(mode="after")
    def _check(self) -> Port:
        problems = Problems()
        if isinstance(self.once, tuple):
            if self.type is not Type.OUTCOME:
                problems.add(("once",), "Only the outcome port lists outcomes here. Say true.")
            elif not self.once:
                problems.add(("once",), "List at least one outcome, such as compile_error.")
            for index, outcome in enumerate(self.once):
                if outcome in self.once[:index]:
                    problems.add(("once", index), already_listed(outcome))
                elif self.outcomes is not None and outcome not in self.outcomes:
                    problems.add(("once", index), f"{outcome} is not among the port's outcomes.")
        elif self.once is not None and self.type is Type.OUTCOME:
            problems.add(
                ("once",), "List the outcomes the step gives once, such as [compile_error]."
            )
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
    GPU devices; and the wall time a batch step takes once before its
    items, none when it does no such work.
    """

    time_ms: Limit
    cpu_ms: Limit
    memory_mb: Limit
    pids: Limit
    output_mb: Limit
    gpus: Annotated[int, Field(strict=True, ge=0)]
    once_time_ms: Limit | None = None

    def as_mapping(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in LIMIT_NAMES}


class _Raise(Model):
    """How a limit is raised from a number `n`: to `n * scale + add` when
    that is more.
    """

    scale: Number = 1
    add: Number = 0

    @model_validator(mode="after")
    def _check(self) -> Self:
        problems = Problems()
        if self.scale <= 0:
            problems.add(("scale",), "This must be more than 0.")
        if self.add < 0:
            problems.add(("add",), "This must be at least 0.")
        problems.raise_any()
        return self


class FromInput(_Raise):
    """A limit raised from the number `input`."""

    input: Handle


class FromSum(_Raise):
    """A limit raised from the sum of a number input over
    every program of a programs port, `<port>.<input>`.
    """

    sum: ProgramInput

    @property
    def program_input(self) -> str:
        """The input of each program the sum reads."""
        return self.sum.partition(".")[2]


class FromMax(_Raise):
    """A limit raised from the largest of a number input
    over every program of a programs port, `<port>.<input>`.
    """

    max: ProgramInput

    @property
    def program_input(self) -> str:
        """The input of each program the largest is taken over."""
        return self.max.partition(".")[2]


_SOURCES: dict[str, type[FromInput | FromSum | FromMax]] = {
    "input": FromInput,
    "sum": FromSum,
    "max": FromMax,
}


def _limit_from(value: object) -> FromInput | FromSum | FromMax:
    if isinstance(value, FromInput | FromSum | FromMax):
        return value
    if not isinstance(value, dict):
        raise ValueError("This must be a set of named settings, not a single value.")
    given = [key for key in _SOURCES if key in value]
    if len(given) != 1:
        raise ValueError("Give exactly one of input, sum and max.")
    try:
        return _SOURCES[given[0]].model_validate(value)
    except ValidationError as error:
        fail(
            [
                (tuple(problem["path"].split(".")) if problem["path"] else (), problem["message"])
                for problem in problems_of(error)
            ]
        )


LimitFrom = Annotated[FromInput | FromSum | FromMax, PlainValidator(_limit_from)]
"""Where a limit is raised from: exactly one of a number input, or the sum
or the largest of a number input over every program of a programs port.
"""


LimitName = Literal["time_ms", "cpu_ms", "memory_mb", "pids", "output_mb", "gpus"]


class PrimitiveForm(Model):
    """One way to use a primitive that declares several: the ports and limits
    that differ, and a description for the editor. Not a declaration: a
    declaration needs an image, and its check wants an outcome at the top.
    """

    description: Line | None = None
    outputs_from: Literal["manifest", "programs"] | None = None
    limits: Limits
    limits_from: dict[LimitName, LimitFrom] = Field(default_factory=dict)
    inputs: dict[str, Declared] = Field(default_factory=dict)
    outputs: dict[str, Declared] = Field(default_factory=dict)


class PrimitiveDeclaration(Model):
    """A `primitive.yaml`. `schema_version` is required; `batch`, `network`
    and `cache` default to off, and `limits_from`, `outputs_from`
    and `inputs` to none. `title` and `description` are for the workflow
    editor, and change nothing a plan holds.
    """

    schema_version: Version
    title: Line | None = None
    description: Line | None = None
    image: Image
    batch: Flag = False
    network: Flag = False
    cache: Flag = False
    forms: dict[str, PrimitiveForm] = Field(default_factory=dict)
    limits: Limits | None = None
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
        assert self.limits is not None
        limits = self.limits.as_mapping()
        for name, source in self.limits_from.items():
            if isinstance(source, FromInput):
                n = numbers.get(source.input)
            else:
                key = source.program_input
                given = [
                    exact_number(each[key]) for each in (programs or {}).values() if key in each
                ]
                if not given:
                    continue
                n = sum(given) if isinstance(source, FromSum) else max(given)
            if n is None:
                continue
            wanted = exact_number(n) * source.scale + source.add
            limits[name] = max(limits[name], math.ceil(wanted))
        return limits

    @model_validator(mode="after")
    def _check(self) -> PrimitiveDeclaration:
        if self.forms:
            return self._check_forms()
        problems = Problems()
        if self.limits is None:
            problems.add(("limits",), "This is required.")
            problems.raise_any()
            return self
        _input_problems(problems, ("inputs",), self.inputs, inner=False)
        _output_problems(problems, ("outputs",), self.outputs, inner=False)
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
            if isinstance(source, FromInput):
                given = self.inputs.get(source.input)
                if given is None or given.type is not Type.NUMBER:
                    problems.add(
                        ("limits_from", limit, "input"),
                        f"The primitive has no number input called {source.input!r}.",
                    )
            else:
                over, named = (
                    ("sum", source.sum) if isinstance(source, FromSum) else ("max", source.max)
                )
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

    def _check_forms(self) -> PrimitiveDeclaration:
        """Each form, checked as the shared keys plus that form's keys, and
        the rule that no step's inputs fit two forms (`_fitting_both`).
        """
        problems = Problems()
        if len(self.forms) > 4:
            problems.add(("forms",), "A primitive has at most 4 forms.")
        for key in FORM_AT_TOP:
            if key in self.model_fields_set:
                problems.add((key,), "A primitive with forms gives this on each form, not here.")
        for name, form in self.forms.items():
            if not PORT_NAME.match(name):
                problems.add(
                    ("forms", name),
                    "A form's name may use only lower case letters, digits and underscores (_), "
                    "starting with a letter.",
                )
            self._check_form(problems, name, form)
        self._fitting_both(problems)
        problems.raise_any()
        return self

    def _check_form(self, problems: Problems, name: str, form: PrimitiveForm) -> None:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "image": self.image,
            "limits": form.limits,
            "limits_from": form.limits_from,
            "inputs": form.inputs,
            "outputs": form.outputs,
        }
        description = form.description if form.description is not None else self.description
        if self.title is not None:
            payload["title"] = self.title
        if description is not None:
            payload["description"] = description
        if self.batch:
            payload["batch"] = self.batch
        if self.network:
            payload["network"] = self.network
        if "cache" in self.model_fields_set:
            payload["cache"] = self.cache
        if form.outputs_from is not None:
            payload["outputs_from"] = form.outputs_from
        try:
            PrimitiveDeclaration.model_validate(payload)
        except ValidationError as error:
            for problem in problems_of(error):
                tail = tuple(part for part in problem["path"].split(".") if part)
                problems.add(("forms", name, *tail), problem["message"])

    def _fitting_both(self, problems: Problems) -> None:
        """Each two forms some step's inputs fit both of, as `for_step`
        picks a form: a step fits a form when it gives every input the form
        requires and none it does not declare, so two forms clash when each
        declares every input either requires. A form whose inputs are a part
        of another's, as `build` is of `one`, is told apart by an input the
        other requires.
        """
        names = list(self.forms)
        for at, first in enumerate(names):
            for second in names[at + 1 :]:
                one, other = self.forms[first], self.forms[second]
                given = _required(one) | _required(other)
                if given <= set(one.inputs) & set(other.inputs):
                    inputs = ", ".join(sorted(given)) or "no inputs"
                    problems.add(
                        ("forms", second),
                        f"A step giving {inputs} fits both {first} and {second}. Give one of "
                        "them a required input the other does not declare.",
                    )

    def for_step(self, given: Collection[str], use: str) -> PrimitiveDeclaration | str:
        """This declaration when it lists no forms, else the one form whose
        required inputs are all in `given` and whose inputs include all of
        `given`, with that form's keys at the top and no forms. The words
        when none fits or more than one does. `use` is only for those words.
        """
        if not self.forms:
            return self
        wanted = set(given)
        fits = [
            form
            for form in self.forms.values()
            if _required(form) <= wanted and wanted <= set(form.inputs)
        ]
        if len(fits) == 1:
            form = fits[0]
            return self.model_copy(
                update={
                    "description": (
                        form.description if form.description is not None else self.description
                    ),
                    "outputs_from": form.outputs_from,
                    "limits": form.limits,
                    "limits_from": form.limits_from,
                    "inputs": form.inputs,
                    "outputs": form.outputs,
                    "forms": {},
                }
            )
        which = "fit no form" if not fits else "fit more than one form"
        listed = ", ".join(
            f"{name} ({', '.join(key for key, port in form.inputs.items() if not port.optional)})"
            for name, form in self.forms.items()
        )
        return (
            f"The step's inputs {which} of {use}. "
            "Give every required input of one form and no input it lacks: "
            f"{listed}."
        )


def _required(form: PrimitiveForm) -> set[str]:
    """The inputs a form requires: every one it declares not optional."""
    return {key for key, port in form.inputs.items() if not port.optional}


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
    problems: Problems, where: Path, ports: Mapping[str, Port], *, inner: bool
) -> None:
    """What is wrong with a declaration's input ports at `where`, or a
    programs port's (`inner`), which take neither programs nor links.
    """
    _names(problems, where, ports)
    _outcomes(problems, where, ports)
    for name, port in ports.items():
        at = (*where, name)
        if inner and port.type in (Type.PROGRAMS, Type.LINKS):
            problems.add((*at, "type"), "A program's input cannot be programs or links.")
        elif port.type not in VALUE_TYPES and port.type not in PORT_TYPES:
            problems.add(
                (*at, "type"),
                "An input must be text, a number, true or false, an enum, a file, a folder, "
                "named files, programs or links.",
            )
        if port.type in RUNS_TYPES and port.runs is None:
            problems.add(
                at,
                f"{name} is a {port.type} port. Say whether the primitive runs it as a "
                "program, with runs: true or runs: false.",
            )
        if port.type not in RUNS_TYPES and port.runs is not None:
            problems.add((*at, "runs"), "This applies only to a file, folder or named_files port.")
        for key in ("better", "at_least", "at_most", "once"):
            if getattr(port, key) is not None:
                problems.add((*at, key), "This applies only to an output.")
        if port.type is Type.PROGRAMS:
            inputs, outputs = port.inputs or {}, port.outputs or {}
            _input_problems(problems, (*at, "inputs"), inputs, inner=True)
            _output_problems(problems, (*at, "outputs"), outputs, inner=True)
            source = inputs.get(SOURCE)
            if source is None or source.type is not Type.FOLDER or source.runs is not True:
                problems.add(
                    (*at, "inputs", SOURCE),
                    "A programs port's programs each take a source, a folder input called "
                    "source with runs: true.",
                )


def _output_problems(
    problems: Problems, where: Path, ports: Mapping[str, Port], *, inner: bool
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
