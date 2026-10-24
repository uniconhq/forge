import re
from datetime import timedelta
from typing import Annotated, Any

from pydantic import Field, PlainValidator, model_validator

from forge.domain.definitions.basics import Count, Seconds, Show
from forge.domain.definitions.classic import Group, Relative, _credit
from forge.domain.workflow_definition import Options, Ref
from forge.domain.yaml_models import Line, Model, Number, Problems, Size, already_listed, parse_size


class Rate(Model):
    """At most `count` submissions in any window of `per` seconds."""

    count: Count
    per: Seconds

    @property
    def window(self) -> timedelta:
        return timedelta(seconds=self.per)


DEFAULT_SUBMISSIONS = 50
DEFAULT_RATE = Rate(count=1, per=30)


class Submissions(Model):
    """What a submit is counted against: at most `max` submissions, 50 by
    default, and at most one every 30 seconds unless `rate` says otherwise.
    """

    max: Count = DEFAULT_SUBMISSIONS
    rate: Rate = DEFAULT_RATE


DEFAULT_MAX_SIZE = parse_size("10MB")
FILE_CEILING = parse_size("2GB")
"""The most any one file input may total, whatever a task allows. It is the
forge's own limit on an object in its large-file store (Forgejo: `[server]
LFS_MAX_FILE_SIZE`), which refuses a larger one itself, so a task allowing
more would only promise what the forge then takes back."""
SUBMISSION_CEILING = parse_size("2GB")
"""The most any submission may be, whatever a task allows. No submission
passes through the platform's memory any more, so what this bounds is the
disk one person's submissions take in the store every contest shares; it is
also what the open-upload allowance is counted against (`domain.uploads`).
Raising it costs disk and nothing else."""

NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class Form(Model):
    """The form details of an input the contestant gives, all optional: its
    label; a default for text, a number, true or false and an enum; the
    least and the most a number may be; the options an enum offers; and the
    most a file or folder input's files may total.
    """

    label: Line | None = None
    default: Any = None
    min: Number | None = None
    max: Number | None = None
    options: Options | None = None
    max_size: Size | None = None


ENTRANT = "entrant"
"""What makes an input the entrant's in a run task's `inputs`, alone or as
the key of its form details, and what stands for the entrant's value in a
tuple of `repeat`."""


def _row_value_problem(value: object) -> str | None:
    if isinstance(value, dict) and "secret" in value:
        return "A secret is given once, in inputs, never in a tuple, so a run's key never shows it."
    if value is None or isinstance(value, list | dict):
        return (
            "Give text, a number, true or false, a file or folder in the task, or entrant, "
            "not a list, a set of settings or nothing."
        )
    return None


class Axis(Model):
    """One axis of a run task's `repeat` (SIMPLER-CORE.md 8.2): the workflow
    inputs that move together, and one row per tuple, written as a list in
    the order of `inputs`, as the value alone when the axis repeats one
    input, or as a mapping of each input to its value, so no two get
    swapped. A value may be `entrant`: the entrant gives that input once for
    the tuple.
    """

    inputs: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    tuples: list[Any] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Axis:
        problems = Problems()
        seen: set[str] = set()
        for index, name in enumerate(self.inputs):
            if name in seen:
                problems.add(("inputs", index), already_listed(name))
            seen.add(name)
        for index, row in enumerate(self.tuples):
            for path, message in self._row_problems(row):
                problems.add(("tuples", index, *path), message)
        problems.raise_any()
        return self

    def _row_problems(self, row: object) -> list[tuple[tuple[str | int, ...], str]]:
        found: list[tuple[tuple[str | int, ...], str]] = []
        count = len(self.inputs)
        if isinstance(row, dict):
            for name in self.inputs:
                if name not in row:
                    found.append(((), f"This tuple gives no value for {name}."))
            for key, value in row.items():
                if key not in self.inputs:
                    found.append(
                        (
                            (str(key),),
                            f"{key} is not one of this axis's inputs, which are "
                            f"{', '.join(self.inputs)}.",
                        )
                    )
                elif (problem := _row_value_problem(value)) is not None:
                    found.append(((str(key),), problem))
            return found
        if isinstance(row, list):
            if len(row) != count:
                found.append(
                    (
                        (),
                        f"This tuple has {len(row)} {'value' if len(row) == 1 else 'values'}, "
                        f"and the axis repeats {', '.join(self.inputs)}, so give one value "
                        "for each, in that order.",
                    )
                )
                return found
            for index, value in enumerate(row):
                if (problem := _row_value_problem(value)) is not None:
                    found.append(((index,), problem))
            return found
        if count != 1:
            return [
                (
                    (),
                    f"The axis repeats {', '.join(self.inputs)}, so give each tuple as a list "
                    "in that order, or as a mapping of each input to its value.",
                )
            ]
        problem = _row_value_problem(row)
        return [((), problem)] if problem is not None else []

    @property
    def rows(self) -> list[tuple[object, ...]]:
        """Each tuple as its values in the order of `inputs`."""
        found: list[tuple[object, ...]] = []
        for row in self.tuples:
            if isinstance(row, dict):
                found.append(tuple(row[name] for name in self.inputs))
            elif isinstance(row, list):
                found.append(tuple(row))
            else:
                found.append((row,))
        return found


class ShowTimes(Model):
    """A run task's `show` (SIMPLER-CORE.md 9.4): the values, groups and
    task files entrants see as soon as a grading ends, at the reveal, and
    once staff release the task's results.
    """

    now: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    after_close: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    on_release: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)

    def lists(self) -> dict[str, list[str]]:
        """Each time's entries, earliest first."""
        return {"now": self.now, "after_close": self.after_close, "on_release": self.on_release}


class Scoring(Model):
    """A run task's `scoring` (SIMPLER-CORE.md 9): its groups of runs, each
    picked by repeated input values; its values, each a tree of blocks
    (`forge.domain.blocks`); and what entrants see when. What each reads is
    checked against the task's runs and the workflow's columns at the save
    (`forge.domain.run_task`).
    """

    groups: dict[str, dict[str, Any]] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)
    show: ShowTimes = ShowTimes()


class Check(Model):
    """One of a run task's `checks` (SIMPLER-CORE.md 9.6): a made-up
    submission, `submit`, giving each entrant input by its name, a task
    path for a file or folder, a list of them in tuple order for one given
    in parts, a value otherwise; and what it must get, `expect`: a
    `verdict` every run must get, a number for each value named, or both.
    What each names is checked against the task at its save
    (`forge.domain.checks`).
    """

    submit: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(min_length=1)


class TaskDefinition(Model):
    """A `task.yaml`, in one of two formats, as its workflow's: a workflow
    with a test block is graded test by test, under `test_groups` and
    `credit`; a run workflow (SIMPLER-CORE.md 8 and 9) runs once per
    combination of `repeat`'s tuples, is scored by `scoring`, which such a
    task always has (`runs`), and may list `checks`, graded at its save.
    `inputs` defaults to none, `credit` to an accepted test earning 1, and
    `submissions` to 50 at one every 30 seconds.
    """

    name: Line
    workflow: Ref
    inputs: dict[str, Any] = Field(default_factory=dict)
    credit: Annotated[str | Relative, PlainValidator(_credit)] | None = None
    test_groups: dict[str, Group] = Field(default_factory=dict, min_length=1)
    repeat: list[Axis] = Field(default_factory=list)
    scoring: Scoring | None = None
    checks: list[Check] = Field(default_factory=list)
    submissions: Submissions = Submissions()

    @property
    def runs(self) -> bool:
        """Whether the task is in the run format, its workflow a run workflow."""
        return self.scoring is not None

    @model_validator(mode="after")
    def _check(self) -> TaskDefinition:
        problems = Problems()
        for name, group in self.test_groups.items():
            for path, message in self._group_problems(name, group):
                problems.add(("test_groups", name, *path), message)
        if self.credit is not None and not self.gives_points:
            problems.add(
                ("credit",),
                "No test group gives points, so what an accepted test earns changes nothing. "
                "Give a test group points, or let an accepted test earn 1.",
            )
        problems.raise_any()
        return self

    def _group_problems(self, name: str, group: Group) -> list[tuple[tuple[str, ...], str]]:
        """T1, T2 as far as the file says, and T6."""
        found: list[tuple[tuple[str, ...], str]] = []
        if not NAME.match(name):
            found.append(
                (
                    (),
                    "A test group's name may use only letters, digits, hyphens (-) and "
                    "underscores (_).",
                )
            )
        if group.pass_at is not None:
            if group.pass_ is None:
                found.append(
                    (
                        ("pass_at",),
                        'This applies only when the group gives points under "at least a share '
                        'of its tests must pass". Give those points, or leave this empty.',
                    )
                )
            if not 0 < group.pass_at <= 1:
                found.append(
                    (
                        ("pass_at",),
                        "This must be more than 0 and at most 1, such as 0.5 for half the tests.",
                    )
                )
        if group.worst is not None and self.credit is None:
            found.append(
                (
                    ("worst",),
                    "Each test here either passes or fails, so the lowest test score is the "
                    'same as "every test must pass". Give these points there instead.',
                )
            )
        if group.test_weights is not None:
            for test, weight in group.test_weights.items():
                if not NAME.match(test):
                    found.append(
                        (
                            ("test_weights", test),
                            "A test's name may use only letters, digits, hyphens (-) and "
                            "underscores (_).",
                        )
                    )
                elif weight <= 0:
                    found.append(
                        (
                            ("test_weights", test),
                            f"{name}/{test} has a weight of {weight}, and a weight must be "
                            "more than 0. Put a test that should count for nothing in a test "
                            "group that gives no points.",
                        )
                    )
            reads = group.each is not None or (
                group.pass_ is not None and group.pass_at is not None and group.pass_at < 1
            )
            if not reads:
                found.append(
                    (
                        ("test_weights",),
                        f"The test weights on {name} change nothing. They count only "
                        "towards points for each test passed, or when the share of tests "
                        "that must pass is below 1.",
                    )
                )
        if group.show is Show.VERDICT:
            problem = self._verdict_problem(name, group)
            if problem is not None:
                found.append((("show",), problem))
        return found

    def _verdict_problem(self, name: str, group: Group) -> str | None:
        """T6: a verdict shows only all-or-nothing points."""
        if group.each is not None:
            return (
                f"{name} shows entrants only its outcome until the reveal, but points for "
                "each test passed would tell them how many of its tests passed. Give its "
                'points under "every test must pass" instead, or change "When entrants see '
                'results".'
            )
        if group.worst is not None:
            return (
                f"{name} shows entrants only its outcome until the reveal, but points for "
                "the lowest test score would tell them how its worst test did. Give its "
                'points under "every test must pass" instead, or change "When entrants see '
                'results".'
            )
        if isinstance(self.credit, Relative):
            return (
                "This test group shows entrants only its outcome until the reveal, but its "
                "points would change as other entrants do better on its hidden tests. Show "
                "its results to entrants at once or only at the reveal."
            )
        return None

    @property
    def gives_points(self) -> bool:
        """Whether some group carries a rule weight, `W > 0`."""
        return any(group.weight > 0 for group in self.test_groups.values())
