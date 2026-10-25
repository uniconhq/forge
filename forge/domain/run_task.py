"""A run task checked against its run workflow (SIMPLER-CORE.md 8 and 9), as
its save checks it, and what the check learns that the planner and the
pages read after it.

The task fills each workflow input once: in `inputs`, as a setting or as
the entrant's (`code: entrant`, or `{entrant: <form details>}`), or by one
axis of `repeat`. The runs are every combination of the axes' tuples, the
first axis outermost, each keyed by its repeated values, where the entrant
fills one its tuple's position, "Part 1" (`Part`). Each run has an opaque id,
`r<n>`, kept with its key across the task's versions and a new key taking
the next number, read from the latest publication's `plans/runs.json`
(`RunsFile`), so adding tuples renumbers nothing.

Checked here (8.5, 9.5): every input filled at most once and each one not
optional exactly once; at least one the entrant's; each setting of its
input's type, a file or folder there, an enum within its options; no two
runs with one key; an input raising a limit, or a manifest the save reads,
the task's; a secret given only whole to a secret port, and only once the
org holds secrets, which none does yet; a program the entrant controls
given a manifest only where its primitive can mark its reports as the
entrant's; a judge's time limit longer than its peers'; each step within
the machine; each report of a manifest the task gives against that file;
once anyone has submitted, the entrant's inputs and their types as they
were. Then `scoring`: each group picks runs by values of repeated inputs the
task fills and holds at least one; group and value names one set, none a
path in the task; each value's blocks (`forge.domain.blocks.check`); and
each `show` entry naming a group, a value, a file or a folder, at one time
only.

A run is early when a group shown `now` holds it, or a value shown `now`
reads a group holding it, directly or through other values (11.3); a value
shown `now` reading `compiled` makes the compile early even when no run is.
`on_release` is late until a release, which the forge has no action for
yet, so `RunTask.early` takes whether the task is released. A report is
self-reported when an entrant input reaches the program that wrote it, and
the run's `verdict` and `passed` when one reaches a program given a
manifest (#504, open 46).
"""

import itertools
import math
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from forge.domain import exact_json
from forge.domain.blocks.catalogue import ALL
from forge.domain.blocks.check import ValuesCheck, check_names, check_values, same_value
from forge.domain.content import names_path, path_problem
from forge.domain.definitions import (
    ENTRANT,
    TASK_FILE,
    Form,
    Scoring,
    ShowTimes,
    TaskDefinition,
    form_problems,
)
from forge.domain.grading import Machine
from forge.domain.plans import (
    PLANS_FOLDER,
    SECRET_NAME,
    a_type,
    has_path,
    is_reserved,
    spelled,
)
from forge.domain.primitives import PROGRAMS, Port
from forge.domain.program_manifest import ProgramManifest, parse_program
from forge.domain.run_workflow import (
    InputManifest,
    Program,
    ResolvedReport,
    ResolvedStep,
    RunWorkflowCheck,
)
from forge.domain.types import Type
from forge.domain.workflow_definition import (
    RUN_NAMES,
    BadReference,
    RunInput,
    RunWorkflow,
    whole_reference,
)
from forge.domain.yaml_models import InvalidDefinition, Path, Problem, is_number, path_text

RUNS_PATH = f"{PLANS_FOLDER}runs.json"
"""Where a run task's publication keeps its runs and what its next version
reads back: each key's id, the entrant's inputs and the self-reported
columns."""
MOST_RUNS = 1000
"""The most runs a task can have: every combination of its tuples, each run
graded for each submission."""
RUN_ID = re.compile(r"[A-Za-z0-9_-]+")
"""What a run's id is made of, as a runs file holds it and a path names it."""
TIMES = ("now", "after_close", "on_release")
OPENNESS = {"on_release": 0, "after_close": 1, "now": 2}
"""How soon each time of `show` shows an entry, latest first: what a
release shows is shown at no set time."""
SHOWN_WORDS = {
    "now": "as soon as a grading ends",
    "after_close": "at the reveal",
    "on_release": "once results are released",
}
SECRETS_LATER = (
    "Secrets come with org secrets, which Unicon does not hold yet, so a task cannot give "
    "one. Give this input a value instead."
)
VERDICT_WORDS = (
    "verdict is words, and blocks read numbers. Read passed, 1 when the run is accepted."
)


@dataclass(frozen=True, slots=True)
class Part:
    """An entrant input's value in a tuple: the tuple's position, from 1."""

    number: int

    def __str__(self) -> str:
        return f"Part {self.number}"


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
    columns self-reported with the entrant inputs that make them so, and
    whether a value shown `now` reads `compiled`.
    """

    ids: Mapping[str, str]
    runs: tuple[Run, ...]
    entrant: Mapping[str, tuple[Type, int | None]]
    self_reported: Mapping[str, frozenset[str]]
    compile_early: bool

    def to_bytes(self) -> bytes:
        document = {
            "ids": dict(sorted(self.ids.items())),
            "runs": [
                {"id": run.id, "key": _key_json(run.key), "early": run.early} for run in self.runs
            ],
            "entrant": {
                name: {"type": kind.value, "parts": parts}
                for name, (kind, parts) in sorted(self.entrant.items())
            },
            "self_reported": {
                name: sorted(inputs) for name, inputs in sorted(self.self_reported.items())
            },
            "compile_early": self.compile_early,
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
            )
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise ValueError(f"{RUNS_PATH} does not read: {error}") from None
        if len(set(found.ids.values())) != len(found.ids):
            raise ValueError(f"{RUNS_PATH} does not read: two keys have one id")
        if len({run.id for run in found.runs}) != len(found.runs):
            raise ValueError(f"{RUNS_PATH} does not read: a run is listed twice")
        return found


@dataclass(frozen=True, slots=True)
class RunTask:
    """A run task as its save checked it: its runs in order, each group's
    runs by id, `all` among them, what each value reads, `show`, the
    entrant's inputs, the settings the task gives once, the columns
    self-reported with the entrant inputs that make them so, and the id of
    every key it has had.
    """

    runs: tuple[Run, ...]
    groups: Mapping[str, tuple[str, ...]]
    values: ValuesCheck
    show: ShowTimes
    entrant: Mapping[str, EntrantInput]
    settings: Mapping[str, object]
    self_reported: Mapping[str, frozenset[str]]
    ids: Mapping[str, str] = field(default_factory=dict)

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
        """Whether a value shown during the contest reads `compiled`, which
        makes the compile early even when no run is.
        """
        return any(
            "compiled" in self.values.through(entry).facts
            for entry in self.shown(released=released)
            if entry in self.values.reads
        )

    def self_reported_values(self) -> frozenset[str]:
        """The values that read a self-reported column, directly or through
        other values.
        """
        return frozenset(
            name
            for name in self.values.reads
            if any(column in self.self_reported for _, column in self.values.through(name).columns)
        )

    def runs_file(self) -> RunsFile:
        early = self.early()
        return RunsFile(
            self.ids,
            tuple(Run(run.id, run.key, run.id in early) for run in self.runs),
            {name: (each.type, each.parts) for name, each in self.entrant.items()},
            self.self_reported,
            self.compile_early(),
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
    latest publication's runs file (#504), so it asks once per version that
    changes one; none otherwise.
    """
    then = previous.self_reported if previous is not None else {}
    moved = sorted(name for name, inputs in task.self_reported.items() if then.get(name) != inputs)
    if not moved:
        return None
    inputs = sorted({each for name in moved for each in task.self_reported[name]})
    values = sorted(task.self_reported_values())
    one = len(moved) == 1
    said = (
        f"What the entrant gives as {_joined(inputs)} could set {_joined(moved)}, so "
        f"{'it is' if one else 'they are'} self-reported"
    )
    if values:
        said += (
            f", as {'is' if len(values) == 1 else 'are'} {_joined(values)}, reading "
            f"{'it' if one else 'them'}"
        )
    return f"{said}: entrants could give themselves any result there. Confirm the task means it."


def _joined(items: Sequence[str]) -> str:
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def canonical(key: Mapping[str, object]) -> str:
    """A run's key as one text, the same for the same key in any version: a
    number by its amount, so `1` and `1.0` are one key, as a group's pick
    reads them (`same_value`).
    """
    amounts = {
        name: Decimal(spelled(value)) if is_number(value) and not isinstance(value, bool) else value
        for name, value in key.items()
    }
    return exact_json.dumps(_key_json(amounts), sort_keys=True)


def show_names(scoring: Scoring) -> set[str]:
    """The entries of `scoring`'s `show` that name a group or a value, `all`
    among them, rather than a task file."""
    return {ALL, *scoring.groups, *scoring.values}


def file_time(scoring: Scoring, path: str) -> str | None:
    """When a task file is first shown: the earliest time of `scoring`'s
    `show` whose list names it or a folder it is in, or none when no entry
    does. An entry naming a group or a value names no file.
    """
    names = show_names(scoring)
    for time, entries in scoring.show.lists().items():
        if any(entry not in names and names_path(entry, path) for entry in entries):
            return time
    return None


def _under_sooner(entry: str, time: str, show: ShowTimes) -> str | None:
    """Why `entry`, listed under `time`, cannot be: a folder it is in is
    listed under a sooner time, which shows it then, before `time` says.
    A file listed sooner than its folder is shown sooner, as written.
    """
    for folder_time, entries in show.lists().items():
        if OPENNESS[folder_time] <= OPENNESS[time]:
            continue
        for folder in entries:
            if folder.endswith("/") and folder != entry and entry.startswith(folder):
                return (
                    f"{entry} is in {folder}, which is shown {SHOWN_WORDS[folder_time]}, so "
                    f"{entry} is shown then too. Remove it here, or list {folder} later."
                )
    return None


def shown_times(show: ShowTimes) -> dict[str, str]:
    """Each entry of `show` with its time."""
    return {entry: time for time, entries in show.lists().items() for entry in entries}


@dataclass(frozen=True, slots=True)
class Shown:
    """What a `show` lists, each with its time: the groups and values by
    name, and each task file by path, with the entry that lists it, a
    folder's entry standing for every file under it.
    """

    names: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, tuple[str, str]] = field(default_factory=dict)


def shown_under(scoring: Scoring, paths: Collection[str]) -> Shown:
    """What `scoring`'s `show` lists, its files those of `paths`, the task's
    files as the same save left them.
    """
    names = show_names(scoring)
    files: dict[str, tuple[str, str]] = {}
    for path in paths:
        for time, entries in scoring.show.lists().items():
            entry = next(
                (entry for entry in entries if entry not in names and names_path(entry, path)),
                None,
            )
            if entry is not None:
                files[path] = (time, entry)
                break
    listed = shown_times(scoring.show)
    return Shown({entry: time for entry, time in listed.items() if entry in names}, files)


def most_shown(shown: Iterable[Shown]) -> Shown:
    """Each name and file any of `shown` lists, with the soonest time one
    shows it.
    """
    names: dict[str, str] = {}
    files: dict[str, tuple[str, str]] = {}
    for each in shown:
        for name, time in each.names.items():
            was = names.get(name)
            if was is None or OPENNESS[time] > OPENNESS[was]:
                names[name] = time
        for path, listed in each.files.items():
            had = files.get(path)
            if had is None or OPENNESS[listed[0]] > OPENNESS[had[0]]:
                files[path] = listed
    return Shown(names, files)


def hidden_again(before: Shown, scoring: Scoring, paths: Collection[str]) -> list[Problem]:
    """A problem at `scoring`'s `show` for each name or file `before` holds,
    with the soonest time it was shown under a publication that graded a
    submission, that `show` drops or moves later (SIMPLER-CORE.md 9.4 and
    16); `paths` are the task's files as the save leaves them, and a file
    no longer among them has nothing left to show. A file is compared by
    path, whichever entry lists it, and reported by the entry that showed it.
    """
    now = shown_under(scoring, paths)
    problems: list[Problem] = []
    for name, then in before.names.items():
        time = now.names.get(name)
        if time is None or OPENNESS[time] < OPENNESS[then]:
            problems.append(_hidden(name, then, time))
    reported: set[str] = set()
    for path, (then, entry) in sorted(before.files.items()):
        if path not in paths or entry in reported:
            continue
        time = now.files[path][0] if path in now.files else None
        if time is None or OPENNESS[time] < OPENNESS[then]:
            reported.add(entry)
            problems.append(_hidden(entry, then, time))
    return problems


def _hidden(entry: str, then: str, time: str | None) -> Problem:
    return Problem(
        path=path_text(("scoring", "show", time)) if time else "scoring.show",
        message=f"Entrants have been shown {entry} {SHOWN_WORDS[then]}, and what they have "
        "been shown cannot be hidden again. List it as soon as before.",
    )


def manifest_inputs(checked: RunWorkflowCheck) -> set[str]:
    """Every workflow input wired into a manifest port, whose `program.yaml`
    the task's save reads.
    """
    return {
        program.manifest.input
        for step in checked.steps.values()
        for program in (step.program, *step.programs.values())
        if isinstance(program.manifest, InputManifest)
    }


def manifest_files(definition: TaskDefinition, checked: RunWorkflowCheck) -> set[str]:
    """The task files the task fills a manifest input with, which the save
    reads as `program.yaml` files before it checks the task.
    """
    wanted = manifest_inputs(checked)
    found: set[str] = set()
    for name in wanted:
        given = definition.inputs.get(name)
        if isinstance(given, str) and given != ENTRANT:
            found.add(given)
    for axis in definition.repeat:
        for index, name in enumerate(axis.inputs):
            if name in wanted:
                for row in axis.rows:
                    value = row[index] if index < len(row) else None
                    if isinstance(value, str) and value != ENTRANT:
                        found.add(value)
    return found


def check_run_task(
    definition: TaskDefinition,
    workflow: RunWorkflow,
    checked: RunWorkflowCheck,
    paths: Collection[str],
    manifests: Mapping[str, bytes],
    *,
    previous: RunsFile | None,
    submitted: bool,
    secrets: Collection[str],
    machine: Machine,
) -> RunTaskCheck:
    """Check the run task `definition` against its workflow, as
    `check_run_workflow` resolved it at its version, `paths` the task's
    files and `manifests` the content of each task file a manifest input is
    filled with (`manifest_files`) that is there. `previous` is the latest
    publication's runs file, `submitted` whether anyone has submitted to
    the task, and `secrets` the org's.
    """
    binder = _Binder(definition, workflow, checked, paths, secrets, machine)
    binder.bind()
    runs = binder.make_runs(previous)
    groups = binder.groups(runs)
    values = binder.scoring(runs, manifests)
    binder.show()
    binder.limits(runs)
    binder.manifests(runs, manifests)
    marks = binder.self_reported()
    if previous is not None and submitted:
        binder.entrant_kept(previous)
    if binder.problems:
        return RunTaskCheck(binder.problems, None)
    assert values is not None, "a task with no scoring is refused at `scoring`"
    task = RunTask(
        tuple(runs),
        groups,
        values,
        definition.scoring.show if definition.scoring is not None else ShowTimes(),
        binder.entrant,
        binder.settings,
        marks,
        binder.ids,
    )
    early = task.early()
    task = RunTask(
        tuple(Run(run.id, run.key, run.id in early) for run in runs),
        task.groups,
        task.values,
        task.show,
        task.entrant,
        task.settings,
        task.self_reported,
        task.ids,
    )
    return RunTaskCheck([], task)


@dataclass(frozen=True, slots=True)
class _Secret:
    name: str


@dataclass
class _Axis:
    """An axis as bound: its inputs that read, by their column, and its rows."""

    index: int
    columns: dict[int, str]
    rows: list[tuple[object, ...]]
    given_as: list[object]


class _Binder:
    def __init__(
        self,
        definition: TaskDefinition,
        workflow: RunWorkflow,
        checked: RunWorkflowCheck,
        paths: Collection[str],
        secrets: Collection[str],
        machine: Machine,
    ) -> None:
        self.task = definition
        self.workflow = workflow
        self.checked = checked
        self.paths = paths
        self.secrets = secrets
        self.machine = machine
        self.problems: list[Problem] = []
        self.settings: dict[str, object] = {}
        self.entrant: dict[str, EntrantInput] = {}
        self.where: dict[str, Path] = {}
        """Where in `task.yaml` each workflow input is filled."""
        self.repeated: dict[str, list[object]] = {}
        """Each repeated input the task fills, with its values in the order
        its axis gives them."""
        self.axes: list[_Axis] = []
        self.ids: dict[str, str] = {}

    def refuse(self, at: Path, message: str) -> None:
        self.problems.append(Problem(path=path_text(at), message=message))

    # 8.1: inputs

    def bind(self) -> None:
        declared = self.workflow.inputs
        for name, given in self.task.inputs.items():
            at: Path = ("inputs", name)
            wanted = declared.get(name)
            if wanted is None:
                self.refuse(
                    at, f"The workflow {self.task.workflow} has no input called {name}. Remove it."
                )
                continue
            self.where[name] = at
            form = _entrant_form(given)
            if isinstance(form, dict):
                self._entrant(at, name, wanted, form, None)
                continue
            if form is not None:
                self.refuse(
                    (*at, ENTRANT),
                    "Give the form details as settings, such as {entrant: {label: Your "
                    "solution}}, or write entrant alone.",
                )
                continue
            problem = self._setting_problem(wanted, given)
            if problem is not None:
                self.refuse(at, problem)
            elif isinstance(given, dict):
                self.settings[name] = _Secret(str(given["secret"]))
            else:
                self.settings[name] = given
        for index, axis in enumerate(self.task.repeat):
            self._axis(index, axis.inputs, axis.rows, axis.tuples)
        for name, wanted in declared.items():
            if name not in self.where and not wanted.optional:
                self.refuse(
                    ("inputs", name),
                    f"Give this input {_described(wanted.type)}, make it the entrant's with "
                    f"{name}: entrant, or repeat it.",
                )
        if not self.entrant and not self.problems:
            self.refuse(
                ("inputs",),
                "No input is the entrant's, so there is nothing to submit. Make one the "
                "entrant's, such as code: entrant.",
            )

    def _entrant(
        self, at: Path, name: str, wanted: RunInput, form: Mapping[str, Any], parts: int | None
    ) -> None:
        problems = form_problems(form, wanted.type.value, wanted.options)
        for path, message in problems:
            self.refuse((*at, ENTRANT, *path), message)
        details = Form() if problems else Form.model_validate(form)
        self.entrant[name] = EntrantInput(name, wanted.type, wanted.options, details, parts)

    def _setting_problem(self, wanted: RunInput, given: object) -> str | None:
        if isinstance(given, dict) and "secret" in given:
            secret = given.get("secret")
            if (
                set(given) != {"secret"}
                or not isinstance(secret, str)
                or not SECRET_NAME.match(secret)
            ):
                return "Give a secret as {secret: <name>}, a name the org holds it under."
            if wanted.type is not Type.TEXT:
                return (
                    f"Only a text input can be a secret, and this one takes {a_type(wanted.type)}."
                )
            if secret not in self.secrets:
                return SECRETS_LATER
            return None
        return _value_problem(wanted, given, self.paths)

    def _axis(
        self,
        index: int,
        names: Sequence[str],
        rows: list[tuple[object, ...]],
        given_as: list[object],
    ) -> None:
        declared = self.workflow.inputs
        columns: dict[int, str] = {}
        for column, name in enumerate(names):
            at: Path = ("repeat", index, "inputs", column)
            wanted = declared.get(name)
            if wanted is None:
                self.refuse(at, f"The workflow {self.task.workflow} has no input called {name}.")
                continue
            if name in self.where:
                self.refuse(
                    at,
                    f"{name} is already filled at {path_text(self.where[name])}, and each "
                    "input is filled once: in inputs, or by one axis.",
                )
                continue
            self.where[name] = at
            columns[column] = name
            values = [row[column] for row in rows]
            by_entrant = [value == ENTRANT for value in values]
            if all(by_entrant):
                self._entrant(at, name, wanted, {}, len(rows))
                continue
            if any(by_entrant):
                first = by_entrant.index(not by_entrant[0])
                self.refuse(
                    _cell(index, first, column, name, given_as[first]),
                    f"Either every tuple gives entrant for {name}, or none does: a column is "
                    "filled by the entrant or by the task.",
                )
                continue
            for row, value in enumerate(values):
                problem = _value_problem(wanted, value, self.paths)
                if problem is not None:
                    self.refuse(_cell(index, row, column, name, given_as[row]), problem)
            self.repeated[name] = values
        self.axes.append(_Axis(index, columns, rows, given_as))

    # 8.2: the runs

    def make_runs(self, previous: RunsFile | None) -> list[Run]:
        """Every combination of the axes' tuples, the first axis outermost,
        each with its id; none while an axis is refused.
        """
        if self.problems:
            return []
        keyed: list[list[dict[str, object]]] = []
        for axis in self.axes:
            rows: list[dict[str, object]] = []
            seen: dict[str, int] = {}
            for number, row in enumerate(axis.rows):
                cells = {
                    name: Part(number + 1) if row[column] == ENTRANT else row[column]
                    for column, name in axis.columns.items()
                }
                text = canonical(cells)
                if text in seen:
                    self.refuse(
                        ("repeat", axis.index, "tuples", number),
                        f"This tuple is the same as tuple {seen[text] + 1}, so their runs would "
                        "share a key. List each tuple once.",
                    )
                seen.setdefault(text, number)
                rows.append(cells)
            keyed.append(rows)
        if self.problems:
            return []
        count = math.prod(len(rows) for rows in keyed)
        if count > MOST_RUNS:
            self.refuse(
                ("repeat",),
                f"Every combination of these tuples is {count} runs, and a task can have at most "
                f"{MOST_RUNS}. Give fewer tuples.",
            )
            return []
        ids = dict(previous.ids) if previous is not None else {}
        taken = [int(each[1:]) for each in ids.values() if each[1:].isdigit()]
        following = max(taken, default=0) + 1
        runs: list[Run] = []
        for combination in itertools.product(*keyed):
            key: dict[str, object] = {}
            for part in combination:
                key.update(part)
            text = canonical(key)
            if text not in ids:
                ids[text] = f"r{following}"
                following += 1
            runs.append(Run(ids[text], key, False))
        self.ids = ids
        return runs

    # 9.1: groups

    def groups(self, runs: Sequence[Run]) -> dict[str, tuple[str, ...]]:
        found: dict[str, tuple[str, ...]] = {ALL: tuple(run.id for run in runs)}
        scoring = self.task.scoring
        if scoring is None:
            return found
        for name, picks in scoring.groups.items():
            at: Path = ("scoring", "groups", name)
            if not picks:
                self.refuse(
                    at,
                    "Pick the group's runs by at least one repeated input, such as "
                    "input: [tests/1.in].",
                )
                continue
            wanted: dict[str, list[object]] = {}
            for key, given in picks.items():
                values = given if isinstance(given, list) else [given]
                problem = self._pick_problem(key, values)
                if problem is not None:
                    self.refuse((*at, key), problem)
                    continue
                wanted[key] = values
            if len(wanted) != len(picks):
                continue
            held = tuple(
                run.id
                for run in runs
                if all(
                    any(same_value(run.key[key], value) for value in values)
                    for key, values in wanted.items()
                )
            )
            if not held and runs:
                self.refuse(at, "This group holds no run: no run has all of these values.")
            found[name] = held
        return found

    def _pick_problem(self, key: str, values: list[object]) -> str | None:
        if key in self.entrant:
            return (
                f"{key} is filled by the entrant, and only what the task fills picks a "
                "group's runs."
            )
        if key not in self.repeated:
            if key in self.workflow.inputs:
                return (
                    f"{key} is not repeated, so every run has the same {key}, and it picks "
                    "nothing. Pick by a repeated input."
                )
            return f"There is no repeated input called {key}."
        if not values:
            return "Give at least one value."
        for value in values:
            if value is None or isinstance(value, list | dict):
                return "Give each value as text, a number or true or false."
        return None

    # 9.2 and 9.5: names and values

    def scoring(self, runs: Sequence[Run], manifests: Mapping[str, bytes]) -> ValuesCheck | None:
        scoring = self.task.scoring
        if scoring is None:
            self.refuse(("scoring",), "This is required.")
            return None
        for path, message in check_names(scoring.groups, scoring.values):
            self.refuse(("scoring", *path), message)
        for kind, names in (("groups", scoring.groups), ("values", scoring.values)):
            for name in names:
                if name in self.paths or has_path(self.paths, f"{name}/"):
                    self.refuse(
                        ("scoring", kind, name),
                        f"{name} is also a path in the task, so show could not tell them "
                        "apart. Choose another name.",
                    )
        if ALL in self.paths or has_path(self.paths, f"{ALL}/"):
            self.refuse(
                ("scoring",),
                f"{ALL} is a path in the task, and the name of the group of every run, so "
                "show could not tell them apart. Rename it in the task's files.",
            )
        found = check_values(
            scoring.values,
            groups=[*scoring.groups],
            columns=self._columns(manifests),
            keys=self.repeated,
        )
        for path, message in found.problems:
            self.refuse(("scoring", *path), message)
        return found

    def _columns(self, manifests: Mapping[str, bytes]) -> dict[str, str | None]:
        """Each column of a run, none for a number a fold may read and the
        reason for any other.
        """
        columns: dict[str, str | None] = {}
        for name in [*self.repeated, *(name for name, each in self.entrant.items() if each.parts)]:
            kind = self.workflow.inputs[name].type
            columns[name] = (
                None
                if kind is Type.NUMBER
                else f"{name} is {a_type(kind)}, and blocks read numbers only."
            )
        for name, report in self.checked.reports.items():
            port = report.port or self._deferred_port(report, manifests)
            if port is None or port.type is Type.NUMBER:
                columns[name] = None
            else:
                columns[name] = f"{name} is text, and blocks read numbers only."
        columns["verdict"] = VERDICT_WORDS
        columns["passed"] = None
        return columns

    def _deferred_port(self, report: ResolvedReport, manifests: Mapping[str, bytes]) -> Port | None:
        """The type a report of a manifest the task gives has, in every file
        the task fills it with that reads; refused where two of those files
        disagree, since one column holds the report of every run.
        """
        program = report.made_by
        if program is None or not isinstance(program.manifest, InputManifest):
            return None
        name = program.manifest.input
        types: dict[str, str] = {}
        for path in self._filled_with(name):
            read = _read_manifest(path, manifests, program)
            if read is not None and report.output in read.reports:
                types.setdefault(read.reports[report.output].type, path)
        if len(types) > 1:
            (one, first), (other, second) = sorted(types.items(), key=lambda each: each[1])[:2]
            self.refuse(
                self.where.get(name, ("inputs", name)),
                f"{first} reports {report.output} as {a_type(Type(one))} and {second} as "
                f"{a_type(Type(other))}, but "
                f"{report.name} is one column of every run. Give it one type in every file.",
            )
        if not types:
            return None
        return Port.model_validate({"type": min(types, key=lambda each: types[each])})

    # 9.4: show

    def show(self) -> None:
        scoring = self.task.scoring
        if scoring is None:
            return
        names = show_names(scoring)
        seen: dict[str, str] = {}
        for time, entries in scoring.show.lists().items():
            for index, entry in enumerate(entries):
                at: Path = ("scoring", "show", time, index)
                if entry in seen:
                    self.refuse(
                        at,
                        f"{entry} is already listed under {seen[entry]}. An entry is shown at "
                        "one time: list it once."
                        if seen[entry] != time
                        else f"{entry} is already listed above. Remove this one.",
                    )
                    continue
                seen[entry] = time
                if entry in names:
                    continue
                problem = self._shown_path_problem(entry)
                if problem is not None:
                    self.refuse(at, problem)
                    continue
                problem = _under_sooner(entry, time, scoring.show)
                if problem is not None:
                    self.refuse(at, problem)

    def _shown_path_problem(self, entry: str) -> str | None:
        folder = entry.endswith("/")
        problem = path_problem(entry, folder=folder)
        if problem is not None or not (folder or "/" in entry or entry in self.paths):
            return (
                f"{entry} names no group, value, file or folder of the task. Give a group or "
                "a value by its name, a file by its path, or a folder with a / at its end."
            )
        if is_reserved(entry):
            return f"Unicon writes the files in {PLANS_FOLDER} itself, and serves none of them."
        if entry == TASK_FILE:
            return (
                f"{TASK_FILE} names the task's hidden files and gives its settings, so it is "
                "never served. Remove it."
            )
        if not has_path(self.paths, entry):
            return (
                f"The task has no files in {entry}." if folder else f"The task has no file {entry}."
            )
        return None

    # 8.1, kept: limits, secrets, manifests

    def limits(self, runs: Sequence[Run]) -> None:
        for name, uses in self.checked.uses.items():
            at = self.where.get(name)
            if at is None:
                continue
            if name in self.entrant and any(use.raises_limit for use in uses):
                self.refuse(
                    at, "This value raises a limit, so the task must give it, not the entrant."
                )
            if isinstance(self.settings.get(name), _Secret):
                for use in uses:
                    if not use.whole:
                        self.refuse(
                            at,
                            f"{name} is a secret, and the workflow writes it into text at "
                            f"{use.path}. Give a secret whole, to a port of its own.",
                        )
                    elif not use.port.secret or use.port.type is not Type.TEXT:
                        self.refuse(
                            at,
                            f"{name} is a secret, and the workflow gives it at {use.path}, to a "
                            "port that could pass it on to the entrant's program.",
                        )
        if self.problems:
            return
        for order in self.checked.time_limits:
            self._time_order(
                order.longer,
                order.longer_limit,
                order.shorter,
                order.shorter_limit,
                order.step,
                runs,
            )
        for step in self.workflow.steps:
            resolved = self.checked.steps.get(step.id)
            if resolved is not None:
                self._fit(resolved, step.with_, runs)

    def _time_order(
        self,
        longer: str,
        longer_limit: object,
        shorter: str,
        shorter_limit: object,
        step: str,
        runs: Sequence[Run],
    ) -> None:
        for run in runs:
            first, second = self._limit(longer_limit, run), self._limit(shorter_limit, run)
            if first is None or second is None or first[0] > second[0]:
                continue
            blamed = next((origin for origin in (first[1], second[1]) if origin is not None), None)
            at = self.where.get(blamed, ("inputs",)) if blamed is not None else ("inputs",)
            self.refuse(
                at,
                f"In the step {step}, {longer} is given a manifest, so it judges, and needs a "
                f"longer time limit than {shorter}: {spelled(first[0])} is not more than "
                f"{spelled(second[0])}. Each program's wall clock is twice its time limit and "
                f"a second, and {shorter}'s should run out first.",
            )
            return

    def _limit(self, limit: object, run: Run) -> tuple[int | Decimal, str | None] | None:
        """A time limit a `TimeLimitOrder` holds, a number or an input's id,
        as the number it is in `run`, and the input it comes from.
        """
        if isinstance(limit, str):
            return self._input_number(limit, run)
        return self._number(limit, run)

    def _number(self, raw: object, run: Run) -> tuple[int | Decimal, str | None] | None:
        """A number a port is given in `run`, and the input it comes from;
        none when it is not a number known at the save.
        """
        if is_number(raw):
            assert isinstance(raw, int | Decimal)
            return raw, None
        if not isinstance(raw, str):
            return None
        try:
            found = whole_reference(raw, runs=True)
        except BadReference:
            return None
        if found is None or found.kind != "inputs":
            return None
        return self._input_number(found.name, run)

    def _input_number(self, name: str, run: Run) -> tuple[int | Decimal, str | None] | None:
        value = run.key.get(name, self.settings.get(name))
        if not is_number(value):
            return None
        assert isinstance(value, int | Decimal)
        return value, name

    def _fit(self, step: ResolvedStep, given: Mapping[str, Any], runs: Sequence[Run]) -> None:
        declaration = step.declaration
        if declaration.network and not self.machine.network:
            self.refuse(
                ("workflow",),
                f"The step {step.id} uses {step.use}, which needs the network, but no machine "
                "this task may run on gives a step the network.",
            )
        told: set[str] = set()
        for run in runs:
            numbers: dict[str, int | Decimal] = {}
            origins: list[tuple[str, int | Decimal]] = []
            for port, raw in given.items():
                found = self._number(raw, run) if port != PROGRAMS else None
                if found is not None:
                    numbers[port] = found[0]
                    if found[1] is not None:
                        origins.append((found[1], found[0]))
            programs: dict[str, dict[str, int | Decimal]] = {}
            inner = given.get(PROGRAMS)
            if isinstance(inner, dict):
                for name, values in inner.items():
                    if not isinstance(values, dict):
                        continue
                    for port, raw in values.items():
                        found = self._number(raw, run)
                        if found is not None:
                            programs.setdefault(str(name), {})[port] = found[0]
                            if found[1] is not None:
                                origins.append((found[1], found[0]))
            limits = declaration.item_limits(numbers, programs)
            for limit, most, unit in (
                ("memory_mb", self.machine.memory_mb, "MB of memory"),
                ("gpus", self.machine.gpus, "GPUs"),
            ):
                wanted = limits[limit]
                if wanted <= most or limit in told:
                    continue
                told.add(limit)
                said = (
                    f"the step {step.id} needs {wanted} {unit}, but no machine this task may "
                    f"run on has more than {most}."
                )
                if origins:
                    name, value = max(origins, key=lambda origin: origin[1])
                    self.refuse(self.where[name], f"With {name} at {spelled(value)}, {said}")
                else:
                    self.refuse(("workflow",), f"As {self.task.workflow} sets it, {said}")

    def manifests(self, runs: Sequence[Run], manifests: Mapping[str, bytes]) -> None:
        """A manifest the task gives: never the entrant's, each file there
        and reading, and each report the workflow reads of it declared, of
        a type its port takes.
        """
        entrant = sorted(manifest_inputs(self.checked) & set(self.entrant))
        for name in entrant:
            self.refuse(
                self.where[name],
                f"{name} is a program's manifest, which the save reads to check what reads "
                "its reports, so the task must give it, not the entrant.",
            )
        if entrant:
            return
        for deferred in self.checked.deferred:
            step = self.checked.steps.get(deferred.step)
            if step is None:
                continue
            program = step.programs.get(deferred.program) if deferred.program else step.program
            if program is None or not isinstance(program.manifest, InputManifest):
                continue
            name = program.manifest.input
            for path in self._filled_with(name):
                read = _read_manifest(path, manifests, program)
                at = self.where.get(name, ("inputs", name))
                if read is None:
                    self._unread(at, path, manifests, program)
                    continue
                report = read.reports.get(deferred.report)
                if report is None:
                    self.refuse(
                        at,
                        f"{path} reports no {deferred.report}, which the workflow reads at "
                        f"{deferred.path}.",
                    )
                    continue
                optional = report.optional or bool(step.programs)
                if deferred.port is None:
                    if deferred.unit and report.type != "number":
                        self.refuse(
                            at,
                            f"{path} reports {deferred.report} as text, and the workflow "
                            f"gives it a unit at {deferred.path}, which only a number takes.",
                        )
                    continue
                wanted = deferred.port.type
                if wanted is Type.NUMBER and report.type != "number":
                    self.refuse(
                        at,
                        f"{path} reports {deferred.report} as text, and the workflow gives it at "
                        f"{deferred.path} to a port that takes a number.",
                    )
                elif optional and not deferred.port.optional:
                    self.refuse(
                        at,
                        f"{path} reports {deferred.report} as optional, so it may be missing, "
                        f"and the workflow gives it at {deferred.path} to a port that must have "
                        "it.",
                    )

    def _unread(
        self, at: Path, path: str, manifests: Mapping[str, bytes], program: Program
    ) -> None:
        text = manifests.get(path)
        if text is None:
            self.refuse(at, f"{path} is not a file of the task, so its manifest cannot be read.")
            return
        assert program.manifest is not None
        try:
            parse_program(text, path, own_names=program.manifest.own_names)
        except InvalidDefinition as invalid:
            for problem in invalid.errors:
                where = f", at {problem['path']}" if problem["path"] else ""
                self.refuse(at, f"{path}{where}: {problem['message']}")

    def _filled_with(self, name: str) -> list[str]:
        """The task files an input is filled with, once or in each tuple."""
        value = self.settings.get(name)
        if isinstance(value, str):
            return [value]
        return sorted({each for each in self.repeated.get(name, []) if isinstance(each, str)})

    # #504: what the entrant could set

    def self_reported(self) -> dict[str, frozenset[str]]:
        """Each column the entrant could set, with the entrant inputs that
        let them; and a refusal where an entrant input reaches a program
        given a manifest that its primitive cannot mark as the entrant's.
        """
        entrant = frozenset(self.entrant)
        for step in self.checked.steps.values():
            for program in (step.program, *step.programs.values()):
                reached = program.controlled_by & entrant
                if program.manifest is None or program.self_reportable or not reached:
                    continue
                name = sorted(reached)[0]
                which = f"the program {program.name} of " if program.name else ""
                self.refuse(
                    self.where[name],
                    f"{name} is the entrant's, and reaches {which}the step {step.id}, which is "
                    f"given a manifest. {step.use} cannot mark what such a program reports as "
                    f"the entrant's own, so the task must give {name} itself.",
                )
        marks: dict[str, frozenset[str]] = {}
        for name, report in self.checked.reports.items():
            if reached := report.self_reported_by & entrant:
                marks[name] = reached
        if reached := self.checked.verdict_self_reported_by & entrant:
            for name in RUN_NAMES:
                marks[name] = reached
        for name, each in self.entrant.items():
            # A number given in parts is a column of its own, which the
            # entrant sets outright.
            if each.parts and each.type is Type.NUMBER:
                marks[name] = frozenset({name})
        return marks

    # 8.1, kept: the entrant's inputs once anyone has submitted

    def entrant_kept(self, previous: RunsFile) -> None:
        now = {name: (each.type, each.parts) for name, each in self.entrant.items()}
        for name in sorted(previous.entrant.keys() | now.keys()):
            then, given = previous.entrant.get(name), now.get(name)
            if then == given:
                continue
            at = self.where.get(name, ("inputs",))
            if then is None:
                said = f"{name} was not the entrant's"
            elif given is None:
                said = f"{name} was the entrant's"
            elif then[0] != given[0]:
                said = f"{name} was {a_type(then[0])}"
            else:
                said = (
                    f"{name} was given once"
                    if then[1] is None
                    else f"{name} was given in {then[1]} parts"
                )
                if given[1] is not None and then[1] is not None:
                    continue  # more or fewer parts keeps each part's layout
            self.refuse(
                at,
                f"{said}, and entrants have submitted, so which inputs are theirs, and of what "
                "type, cannot change.",
            )


def _entrant_form(given: object) -> object:
    """The form details of an input made the entrant's, `{}` for none, or
    none for a setting.
    """
    if given == ENTRANT:
        return {}
    if isinstance(given, dict) and set(given) == {ENTRANT}:
        return {} if given[ENTRANT] is None else given[ENTRANT]
    return None


def _value_problem(wanted: RunInput, given: object, paths: Collection[str]) -> str | None:
    match wanted.type:
        case Type.TEXT:
            return None if isinstance(given, str) else "This must be text."
        case Type.NUMBER:
            return None if is_number(given) else "This must be a number."
        case Type.BOOLEAN:
            return None if isinstance(given, bool) else "This must be true or false."
        case Type.ENUM:
            options = wanted.options or ()
            return None if given in options else f"This must be one of {', '.join(options)}."
        case Type.FILE | Type.FOLDER:
            folder = wanted.type is Type.FOLDER
            problem = path_problem(given, folder=folder)
            if problem is not None:
                return problem
            assert isinstance(given, str)
            if not has_path(paths, given):
                return (
                    f"The task has no files in {given}. Add them, or give another folder."
                    if folder
                    else f"The task has no file {given}. Add it, or give another file."
                )
            return None
    return "This is not a value this input takes."


def _read_manifest(
    path: str, manifests: Mapping[str, bytes], program: Program
) -> ProgramManifest | None:
    text = manifests.get(path)
    if text is None or program.manifest is None:
        return None
    try:
        return parse_program(text, path, own_names=program.manifest.own_names)
    except InvalidDefinition:
        return None


def _cell(index: int, row: int, column: int, name: str, given: object) -> Path:
    """Where one value of a tuple is written."""
    at: Path = ("repeat", index, "tuples", row)
    if isinstance(given, list):
        return (*at, column)
    if isinstance(given, dict):
        return (*at, name)
    return at


def _described(kind: Type) -> str:
    return {
        Type.TEXT: "some text",
        Type.NUMBER: "a number",
        Type.BOOLEAN: "true or false",
        Type.ENUM: "one of its options",
        Type.FILE: "a file of the task",
        Type.FOLDER: "a folder of the task",
    }.get(kind, "a value")


def _value_words(value: object) -> str:
    return str(value) if isinstance(value, Part) else spelled(value)


def _key_json(key: Mapping[str, object]) -> dict[str, object]:
    return {
        name: {"part": value.number} if isinstance(value, Part) else value
        for name, value in key.items()
    }


def _run_id(value: object) -> str:
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise ValueError(f"{value!r} is no run id")
    return value


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{value!r} is not true or false")
    return value


def _parts(value: object) -> int | None:
    if value is None or (isinstance(value, int) and not isinstance(value, bool) and value > 0):
        return value
    raise ValueError(f"{value!r} is no number of parts")


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not text")
    return value


def _key_read(document: Mapping[str, Any]) -> dict[str, object]:
    return {
        str(name): Part(int(value["part"])) if isinstance(value, dict) else value
        for name, value in document.items()
    }


__all__ = [
    "MOST_RUNS",
    "RUNS_PATH",
    "EntrantInput",
    "Part",
    "Run",
    "RunTask",
    "RunTaskCheck",
    "RunsFile",
    "Shown",
    "canonical",
    "check_run_task",
    "file_time",
    "hidden_again",
    "manifest_files",
    "show_names",
    "shown_times",
    "shown_under",
]
