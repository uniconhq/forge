"""A run task's checks (SIMPLER-CORE.md 9.6, phase 5.4): made-up
submissions graded at the task's save, each with what it must get.

A check's `submit` gives every input the entrant gives, by the same name: a
file or a folder by its path in the task, an input given in parts as a list
of one per part in tuple order, and any other its value. An input the
entrant may leave to its form's default may be left out, and takes it.
`expect` is a `verdict` every run must get, a number for each of the
scoring's values named, or both. Each is checked at the save
(`check_checks`): an input the entrant does not give, a path the task does
not hold, a value of the wrong type, and a value or verdict that does not
exist are refused, each at its path in `task.yaml`, as is a value that
reads a mark staff enter, directly or through other values, since a
check's made-up submission never has one.

The save grades a check through copies of the steps graded per submission
(`copy_of`), given the check's files and values where the entrant's would
be, in the save's own pipeline (`forge.domain.run_plan`), and the check's
values are worked out from what its runs got as any submission's are, with
`seconds_late` and `seconds` 0 (`forge.domain.run_results.check_failing`).
A check keeps in the save row's result the runs worth reading: none once
it passed, at most `RUNS_KEPT` of a failed one (`keep_runs`), so the
result stays within its bound, and the save refuses checks that could
take more than `CHECKS_ROOM` of it (`check_room`). Nothing an entrant
reads names a check, and a file only a check names is never shown
(`check_only`).
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol

from forge.domain import exact_json
from forge.domain.blocks.catalogue import ENTERED
from forge.domain.blocks.evaluate import using
from forge.domain.blocks.numbers import exact
from forge.domain.content import has_path, names_path
from forge.domain.definitions import Check, Form, TaskDefinition
from forge.domain.plan_basics import RESULT_MAX
from forge.domain.primitives import STEP_OUTCOMES
from forge.domain.types import FILES, Type
from forge.domain.yaml_models import Path, Problem, is_number, joined, path_text

VERDICT = "verdict"
"""The key of `expect` that names the verdict every run must get."""
COPY_MOST = 40
"""The longest a copy's step id may be, as a step's name at the door is."""
MOST_FAILING = 20
"""The most sentences a check's result keeps of what it did not get."""
RUNS_KEPT = 20
"""The most runs a failed check keeps in its save row's result; a check
that passed keeps none."""
CHECKS_ROOM = RESULT_MAX // 2
"""The most of a save row's result its checks may take, the rest left to
the save's own runs."""


class EntrantGiven(Protocol):
    """What a check needs to know of an entrant input: its type, the
    workflow's options for an enum, its form and its parts."""

    @property
    def type(self) -> Type: ...

    @property
    def options(self) -> tuple[str, ...] | None: ...

    @property
    def form(self) -> Form: ...

    @property
    def parts(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class Given:
    """One entrant input as a check gives it: the task paths of a file or
    folder input, one per part for an input given in parts, else its
    value, or one per part."""

    paths: tuple[str, ...] = ()
    values: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskCheck:
    """A check as its save checked it: its place in `checks`, from 1, what
    it gives each entrant input, the verdict every run must get, if any,
    and the number each value named must be."""

    index: int
    given: Mapping[str, Given]
    verdict: str | None
    values: Mapping[str, Fraction]

    def part(self, name: str, number: int | None) -> object:
        """What the check gives `name`, or its part `number`, from 1: a
        path for a file or folder, else the value."""
        given = self.given[name]
        at = 0 if number is None else number - 1
        return given.paths[at] if given.paths else given.values[at]


def copy_of(index: int, step: str) -> str:
    """The id of check `index`'s copy of the step `step`."""
    return f"c{index}-{step}"


def check_checks(
    checks: Sequence[Check],
    entrant: Mapping[str, EntrantGiven],
    paths: Collection[str],
    values: Mapping[str, object],
) -> tuple[list[Problem], tuple[TaskCheck, ...]]:
    """Every problem with a task's `checks` against its `entrant` inputs,
    the task's files `paths` and its scoring's `values`, each tree by its
    name, and each check as read when there is none."""
    problems: list[Problem] = []
    found: list[TaskCheck] = []
    marked = using(values, ENTERED) if checks else frozenset()
    for index, check in enumerate(checks):
        at: Path = ("checks", index)
        before = len(problems)
        given: dict[str, Given] = {}
        for name in sorted(set(check.submit) - set(entrant)):
            problems.append(
                Problem(
                    path=path_text((*at, "submit", name)),
                    message=f"The entrant gives no {name}, so a check cannot. A check gives "
                    f"what the entrant does: {joined(sorted(entrant))}.",
                )
            )
        for name, wanted in entrant.items():
            if name not in check.submit:
                default = wanted.form.default
                if wanted.type in FILES or wanted.parts is not None or default is None:
                    problems.append(
                        Problem(
                            path=path_text((*at, "submit")),
                            message=f"This check gives no {name}, which the entrant must give. "
                            "Give it.",
                        )
                    )
                else:
                    given[name] = Given(values=(default,))
                continue
            read = _given(check.submit[name], wanted, paths)
            if isinstance(read, str):
                problems.append(Problem(path=path_text((*at, "submit", name)), message=read))
            else:
                given[name] = read
        verdict, numbers = _expected(check.expect, values, marked, at, problems)
        if len(problems) == before:
            found.append(TaskCheck(index + 1, given, verdict, numbers))
    return problems, tuple(found)


def _given(raw: object, wanted: EntrantGiven, paths: Collection[str]) -> Given | str:
    """What a check gives one entrant input, or why it is refused."""
    raws = raw if wanted.parts is not None else [raw]
    if wanted.parts is not None and (not isinstance(raw, list) or len(raw) != wanted.parts):
        return (
            f"This input is given in {wanted.parts} parts, one per tuple, so give a list "
            f"of {wanted.parts}, in tuple order."
        )
    assert isinstance(raws, list)
    read: list[object] = []
    for part in raws:
        value, problem = _one(part, wanted, paths)
        if problem is not None:
            return problem
        read.append(value)
    if wanted.type in FILES:
        return Given(paths=tuple(str(each) for each in read))
    return Given(values=tuple(read))


def _one(raw: object, wanted: EntrantGiven, paths: Collection[str]) -> tuple[object, str | None]:
    """One part of what a check gives, the path or the value, or why it is
    refused."""
    kind, form = wanted.type, wanted.form
    if kind is Type.FILE:
        if not isinstance(raw, str) or not raw or raw.endswith("/"):
            return None, "This input takes one file: give its path in the task."
        if raw not in paths:
            return None, f"There is no file {raw} in the task."
        return raw, None
    if kind is Type.FOLDER:
        if not isinstance(raw, str) or not raw.strip("/"):
            return None, "This input takes a folder: give its path in the task."
        folder = raw if raw.endswith("/") else f"{raw}/"
        if not has_path(paths, folder):
            return None, f"There is no folder {folder} in the task, or it holds no file."
        return folder, None
    if kind is Type.NUMBER:
        number = exact(raw) if is_number(raw) and not isinstance(raw, bool) else None
        if number is None:
            return None, "This input takes a number."
        low, high = exact(form.min), exact(form.max)
        if low is not None and number < low:
            return None, f"This is below the least the entrant may give, {form.min}."
        if high is not None and number > high:
            return None, f"This is above the most the entrant may give, {form.max}."
        return raw, None
    if kind is Type.BOOLEAN:
        return (raw, None) if isinstance(raw, bool) else (None, "This input takes true or false.")
    if not isinstance(raw, str):
        return None, "This input takes text."
    options = form.options or wanted.options
    if kind is Type.ENUM and options is not None and raw not in options:
        return None, f"This input takes one of {joined(list(options))}."
    return raw, None


def _expected(
    expect: Mapping[str, object],
    values: Collection[str],
    marked: Collection[str],
    at: Path,
    problems: list[Problem],
) -> tuple[str | None, dict[str, Fraction]]:
    """The verdict and the numbers a check expects, each problem added,
    `marked` the values reading a mark staff enter."""
    verdict: str | None = None
    numbers: dict[str, Fraction] = {}
    for name, raw in expect.items():
        where = path_text((*at, "expect", name))
        if name == VERDICT:
            if not isinstance(raw, str) or raw not in STEP_OUTCOMES:
                problems.append(
                    Problem(
                        path=where,
                        message=f"A verdict is one of {joined(list(STEP_OUTCOMES))}.",
                    )
                )
            else:
                verdict = raw
            continue
        if name not in values:
            listed = joined(sorted(values)) if values else "none"
            problems.append(
                Problem(
                    path=where,
                    message=f"There is no value {name} in the task's scoring, so a check "
                    f"cannot expect it. Its values are {listed}, and verdict names the "
                    "verdict every run must get.",
                )
            )
            continue
        if name in marked:
            problems.append(
                Problem(
                    path=where,
                    message=f"{name} reads a mark staff enter, directly or through other "
                    "values, and a check's made-up submission is never marked, so a check "
                    "cannot expect it. Expect a value that reads no mark.",
                )
            )
            continue
        number = exact(raw) if is_number(raw) and not isinstance(raw, bool) else None
        if number is None:
            problems.append(Problem(path=where, message=f"Give the number {name} must be."))
            continue
        numbers[name] = number
    return verdict, numbers


def check_paths(checks: Sequence[TaskCheck]) -> frozenset[str]:
    """Every task path a check gives, a folder ending in `/`."""
    return frozenset(
        path for check in checks for given in check.given.values() for path in given.paths
    )


def check_only(definition: TaskDefinition, path: str) -> bool:
    """Whether only a check of the task names the file at `path`: some
    check gives a file or folder that holds it, and no setting, tuple or
    value's program of the task names it. Such a file is never shown."""
    given = [
        part
        for check in definition.checks
        for raw in check.submit.values()
        for part in (raw if isinstance(raw, list) else [raw])
        if isinstance(part, str) and part
    ]
    if not any(names_path(each, path) or names_path(f"{each}/", path) for each in given):
        return False
    return not any(names_path(entry, path) for entry in _task_named(definition))


def _task_named(definition: TaskDefinition) -> list[str]:
    """Every text the task's settings, tuples and programs give, each of
    which may name a task file."""
    found = [value for value in definition.inputs.values() if isinstance(value, str)]
    for axis in definition.repeat:
        for row in axis.tuples:
            cells = (
                row.values() if isinstance(row, dict) else row if isinstance(row, list) else [row]
            )
            found += [cell for cell in cells if isinstance(cell, str)]
    scoring = definition.scoring
    if scoring is not None:
        found += _texts(scoring.values)
    return found


def _texts(node: object) -> list[str]:
    """Every text in a value's tree, a program's folder among them."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, Mapping):
        return [text for each in node.values() for text in _texts(each)]
    if isinstance(node, list):
        return [text for each in node for text in _texts(each)]
    return []


def check_room(keys: Sequence[Mapping[str, object]], reports: int, steps: int) -> int:
    """The most bytes one check may keep in its save row's result, for a
    task whose runs have `keys`, whose plan names `reports` and whose
    graded steps number `steps`: its runs kept, each at most the widest
    key with every report, text and staff item, then its steps done once
    and its sentences."""
    widest = max((len(exact_json.dumps(dict(key))) for key in keys), default=0)
    entry = 512 + widest + 160 * reports + 256 * steps
    return min(len(keys), RUNS_KEPT) * entry + 2048 + 256 * steps + 320 * MOST_FAILING


def keep_runs(entry: dict[str, object], verdict: str | None) -> None:
    """Leave in a check's `entry` of its save row's result the runs worth
    reading: none once it passed, else at most `RUNS_KEPT`, those that did
    not get `verdict`, or were not accepted when it expects none, first,
    kept in the task's order; and only the staff items of a run kept or a
    step done once."""
    runs = entry.get("runs")
    if not isinstance(runs, list):
        return
    if entry.get("passed") is True:
        kept: list[object] = []
    else:
        wanted = verdict or "accepted"
        missed = [
            place
            for place, run in enumerate(runs)
            if not isinstance(run, Mapping) or run.get("verdict") != wanted
        ]
        rest = [place for place in range(len(runs)) if place not in set(missed)]
        kept = [runs[place] for place in sorted([*missed, *rest][:RUNS_KEPT])]
    entry["runs"] = kept
    ids = {run.get("id") for run in kept if isinstance(run, Mapping)}
    staff = entry.get("staff")
    if isinstance(staff, list):
        entry["staff"] = [
            item
            for item in staff
            if not isinstance(item, Mapping) or item.get("run") is None or item.get("run") in ids
        ]


def failing_words(run: str, got: str, wanted: str) -> str:
    """The sentence for a run that got another verdict than expected."""
    return f"The run {run} got {got}, not {wanted}."


__all__ = [
    "CHECKS_ROOM",
    "COPY_MOST",
    "MOST_FAILING",
    "RUNS_KEPT",
    "VERDICT",
    "Given",
    "TaskCheck",
    "check_checks",
    "check_only",
    "check_paths",
    "check_room",
    "copy_of",
    "failing_words",
    "keep_runs",
]
