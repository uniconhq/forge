from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from forge.domain import exact_json
from forge.domain.blocks.catalogue import EXEC, SHOWN_FACTS
from forge.domain.blocks.check import ValuesCheck
from forge.domain.blocks.numbers import Better
from forge.domain.checks import TaskCheck
from forge.domain.definitions import Form, ShowTimes
from forge.domain.primitives import Port
from forge.domain.program_manifest import PROGRAM_FILE
from forge.domain.run_task.records import RUNS_PATH, SHOWN_WORDS
from forge.domain.run_task.show_rules import canonical, shown_times
from forge.domain.run_task.task_helpers import (
    _flag,
    _key_read,
    _parts,
    _run_id,
    _text,
    _value_words,
    key_json,
)
from forge.domain.types import Type
from forge.domain.yaml_models import Problem, joined


@dataclass(frozen=True, slots=True)
class Run:
    """One run: its id, its key, each repeated input's value, a `Part` where
    the entrant fills it, in the order the axes give them, and whether it
    is early before any release.
    """

    id: str
    key: Mapping[str, object]
    early: bool

    @property
    def canonical(self) -> str:
        """The key as one text, the same for the same key in any version."""
        return canonical(self.key)

    @property
    def label(self) -> str:
        """The key in words, "input parts/1.in, route Part 1"."""
        return ", ".join(f"{name} {_value_words(value)}" for name, value in self.key.items())


@dataclass(frozen=True, slots=True)
class EntrantInput:
    """An input the entrant gives: its type, its options on an enum, its form
    details, and how many parts it is given in, none when given once.
    """

    id: str
    type: Type
    options: tuple[str, ...] | None
    form: Form
    parts: int | None = None


@dataclass(frozen=True, slots=True)
class RunsFile:
    """`plans/runs.json`: the id of every key the task has had, its runs as
    the version saved them, its entrant inputs' types and parts, the
    columns self-reported with the entrant inputs that make them so,
    whether `compiled`, or a value reading it, is shown `now`, and the
    values that read a self-reported column, directly or through others,
    as the save checked them (`RunTask.self_reported_values`), which a
    result marks;
    and the runs each group or value listed under `now` or `on_release`
    reads (`RunTask.runs_read`), which a later save may not shrink once
    shown (SIMPLER-CORE.md 16); and of those values, the ones `show`
    lists, which a save that shows another asks about
    (`self_reported_question`). A file written before `reads` was kept
    reads as none, and one written before `self_reported_shown` was as
    showing none.
    """

    ids: Mapping[str, str]
    runs: tuple[Run, ...]
    entrant: Mapping[str, tuple[Type, int | None]]
    self_reported: Mapping[str, frozenset[str]]
    compile_early: bool
    self_reported_values: frozenset[str] = frozenset()
    reads: Mapping[str, frozenset[str]] = field(default_factory=dict)
    self_reported_shown: frozenset[str] = frozenset()

    def to_bytes(self) -> bytes:
        document = {
            "ids": dict(sorted(self.ids.items())),
            "runs": [
                {"id": run.id, "key": key_json(run.key), "early": run.early} for run in self.runs
            ],
            "entrant": {
                name: {"type": kind.value, "parts": parts}
                for name, (kind, parts) in sorted(self.entrant.items())
            },
            "self_reported": {
                name: sorted(inputs) for name, inputs in sorted(self.self_reported.items())
            },
            "compile_early": self.compile_early,
            "self_reported_values": sorted(self.self_reported_values),
            "reads": {name: sorted(runs) for name, runs in sorted(self.reads.items())},
            "self_reported_shown": sorted(self.self_reported_shown),
        }
        return (exact_json.dumps(document, indent=2, sort_keys=True) + "\n").encode()

    @classmethod
    def from_bytes(cls, content: bytes) -> RunsFile:
        """The file in `content`; `ValueError` for one that does not read,
        or whose ids are not each a run id (`RUN_ID`) given to one key, run
        ids listed twice, or an entrant input's type or parts that no save
        writes.
        """
        try:
            document = exact_json.loads(content)
            found = cls(
                {str(key): _run_id(value) for key, value in document["ids"].items()},
                tuple(
                    Run(_run_id(run["id"]), _key_read(run["key"]), _flag(run["early"]))
                    for run in document["runs"]
                ),
                {
                    str(name): (Type(each["type"]), _parts(each["parts"]))
                    for name, each in document["entrant"].items()
                },
                {
                    str(name): frozenset(_text(each) for each in inputs)
                    for name, inputs in document["self_reported"].items()
                },
                _flag(document["compile_early"]),
                frozenset(_text(each) for each in document.get("self_reported_values", ())),
                {
                    str(name): frozenset(_run_id(each) for each in runs)
                    for name, runs in document.get("reads", {}).items()
                },
                frozenset(_text(each) for each in document.get("self_reported_shown", ())),
            )
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise ValueError(f"{RUNS_PATH} does not read: {error}") from None
        if len(set(found.ids.values())) != len(found.ids):
            raise ValueError(f"{RUNS_PATH} does not read: two keys have one id")
        if len({run.id for run in found.runs}) != len(found.runs):
            raise ValueError(f"{RUNS_PATH} does not read: a run is listed twice")
        return found


@dataclass(frozen=True, slots=True)
class ProgramValue:
    """A value its `exec` block's program works out (SIMPLER-CORE.md 14):
    the value, the folder the program and its `program.yaml` are in, the
    language, the group whose runs it reads, its limits in seconds and
    megabytes, and the one number report its `program.yaml` declares.
    """

    value: str
    folder: str
    language: str
    group: str
    time_limit: int | Decimal
    memory_limit: int | Decimal
    report: str
    better: Better

    @property
    def manifest(self) -> str:
        return f"{self.folder}{PROGRAM_FILE}"


@dataclass(frozen=True, slots=True)
class RunTask:
    """A run task as its save checked it: its runs in order, each group's
    runs by id, `all` among them, what each value reads, `show`, the
    entrant's inputs, the settings the task gives once, the columns
    self-reported with the entrant inputs that make them so, the id of
    every key it has had, each step's outputs a manifest the task gives
    declares (`_Binder.manifest_outputs`), each value's program, and its
    checks (`forge.domain.checks`).
    """

    runs: tuple[Run, ...]
    groups: Mapping[str, tuple[str, ...]]
    values: ValuesCheck
    show: ShowTimes
    entrant: Mapping[str, EntrantInput]
    settings: Mapping[str, object]
    self_reported: Mapping[str, frozenset[str]]
    ids: Mapping[str, str] = field(default_factory=dict)
    manifest_outputs: Mapping[str, Mapping[str, Port]] = field(default_factory=dict)
    programs: Mapping[str, ProgramValue] = field(default_factory=dict)
    checks: tuple[TaskCheck, ...] = ()

    def shown(self, *, released: bool = False) -> list[str]:
        """The entries shown during the contest: `now`'s, and `on_release`'s
        once the task's results are released.
        """
        return [*self.show.now, *(self.show.on_release if released else ())]

    def early(self, *, released: bool = False) -> frozenset[str]:
        """The ids of the early runs: those a group shown during the contest
        holds, or a value shown then reads, directly or through others.
        """
        groups: set[str] = set()
        for entry in self.shown(released=released):
            if entry in self.groups:
                groups.add(entry)
            elif entry in self.values.reads:
                groups |= self.values.through(entry).groups
        return frozenset(run for group in groups for run in self.groups.get(group, ()))

    def compile_early(self, *, released: bool = False) -> bool:
        """Whether `compiled` is shown during the contest, or a value shown
        then reads it, which makes the compile early even when no run is.
        """
        return any(self.reads_compiled(entry) for entry in self.shown(released=released))

    def reads_compiled(self, entry: str) -> bool:
        """Whether the `show` entry `entry` is `compiled`, or a value that
        reads it, directly or through others."""
        return entry in SHOWN_FACTS or (
            entry in self.values.reads and "compiled" in self.values.through(entry).facts
        )

    def early_programs(self, *, released: bool = False) -> frozenset[str]:
        """The values whose `exec` program runs in the early pipeline: each
        shown during the contest, or read by a value shown then, directly or
        through others.
        """
        read = {
            name
            for entry in self.shown(released=released)
            if entry in self.values.reads
            for name in self.values.through(entry).values
        }
        return frozenset(name for name in self.programs if name in read)

    def self_reported_values(self) -> frozenset[str]:
        """The values that read a self-reported column, directly or through
        other values, a program reading their runs whole among them.
        """
        return frozenset(
            name
            for name in self.values.reads
            if any(column in self.self_reported for _, column in self.values.through(name).columns)
            or (EXEC in self.values.through(name).blocks and bool(self.self_reported))
        )

    def runs_of(self, name: str) -> frozenset[str] | None:
        """The ids of the runs a group holds or a value reads, directly or
        through others, none for a name that is neither.
        """
        if name in self.groups:
            return frozenset(self.groups[name])
        if name in self.values.reads:
            through = self.values.through(name).groups
            return frozenset(run for group in through for run in self.groups.get(group, ()))
        return None

    def release_notes(self) -> list[str]:
        """For each group listed sooner than `on_release` that holds runs a
        name under `on_release` reads, that those runs show before a
        release does.
        """
        listed = shown_times(self.show)
        found: list[str] = []
        for name in self.show.on_release:
            read = self.runs_of(name)
            if not read:
                continue
            for group, time in listed.items():
                if time != "on_release" and group in self.groups and read & set(self.groups[group]):
                    found.append(
                        f"{group}, shown {SHOWN_WORDS[time]}, holds runs {name} reads, so "
                        f"entrants see those runs before a release shows {name}."
                    )
        return found

    def runs_read(self) -> dict[str, frozenset[str]]:
        """The runs each group or value listed under `now` or `on_release`
        reads, the names a release may show during the contest.
        """
        found: dict[str, frozenset[str]] = {}
        for entry in self.shown(released=True):
            runs = self.runs_of(entry)
            if runs is not None:
                found[entry] = runs
        return found

    def self_reported_shown(self) -> frozenset[str]:
        """The values reading a self-reported column that `show` lists, at
        any time."""
        listed = {entry for entries in self.show.lists().values() for entry in entries}
        return frozenset(name for name in self.self_reported_values() if name in listed)

    def runs_file(self) -> RunsFile:
        early = self.early()
        return RunsFile(
            self.ids,
            tuple(Run(run.id, run.key, run.id in early) for run in self.runs),
            {name: (each.type, each.parts) for name, each in self.entrant.items()},
            self.self_reported,
            self.compile_early(),
            self.self_reported_values(),
            self.runs_read(),
            self.self_reported_shown(),
        )


@dataclass(frozen=True, slots=True)
class RunTaskCheck:
    """Every problem a run task is refused for, each at its path in
    `task.yaml`, and the task as checked when there is none. There is
    always one or the other: a task whenever no problem is found.
    """

    problems: list[Problem]
    task: RunTask | None


def self_reported_question(task: RunTask, previous: RunsFile | None) -> str | None:
    """What a save asks before it publishes `task` when a column the entrant
    could set is new, or set through other inputs than under `previous`, the
    latest publication's runs file (#504), or when a value reading such a
    column is new or newly listed in `show` (stage 4 open 74), so it asks
    once per version that changes one; none otherwise.
    """
    then = previous.self_reported if previous is not None else {}
    moved = sorted(name for name, inputs in task.self_reported.items() if then.get(name) != inputs)
    values = sorted(task.self_reported_values())
    if previous is not None and not moved:
        fresh = sorted(
            (task.self_reported_values() - previous.self_reported_values)
            | (task.self_reported_shown() - previous.self_reported_shown)
        )
        if not fresh:
            return None
        columns = sorted(task.self_reported)
        inputs = sorted({each for name in columns for each in task.self_reported[name]})
        one = len(fresh) == 1
        return (
            f"{joined(fresh)} {'reads' if one else 'read'} {joined(columns)}, which what "
            f"the entrant gives as {joined(inputs)} could set, and {'is' if one else 'are'} "
            f"new or newly shown, so {'it is' if one else 'they are'} self-reported: entrants "
            "could give themselves any result there. Confirm the task means it."
        )
    if not moved:
        return None
    inputs = sorted({each for name in moved for each in task.self_reported[name]})
    one = len(moved) == 1
    said = (
        f"What the entrant gives as {joined(inputs)} could set {joined(moved)}, so "
        f"{'it is' if one else 'they are'} self-reported"
    )
    if values:
        said += (
            f", as {'is' if len(values) == 1 else 'are'} {joined(values)}, reading "
            f"{'it' if one else 'them'}"
        )
    return f"{said}: entrants could give themselves any result there. Confirm the task means it."
