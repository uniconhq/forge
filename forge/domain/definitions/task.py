from datetime import timedelta
from typing import Annotated, Any

from pydantic import Field, model_validator

from forge.domain.definitions.basics import Count, Seconds
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


_TUPLE_KEYS = frozenset({"title", "max_size"})
TITLE_MOST = 60


def _title_problem(value: object) -> str | None:
    """What is wrong with a tuple's `title`, or none. The words for text
    that is not one line are the ones `Line` uses.
    """
    if not isinstance(value, str):
        return "This must be text."
    if not value.strip():
        return "This cannot be empty."
    if "\n" in value or "\r" in value:
        return "This must fit on one line."
    if len(value) > TITLE_MOST:
        return "A title is at most 60 characters. Make it shorter."
    return None


def _size_problem(value: object) -> str | None:
    """What is wrong with a tuple's `max_size`, in the words an entrant
    input's size uses, or none.
    """
    try:
        size = parse_size(value)
    except ValueError as exc:
        return str(exc)
    if size > FILE_CEILING:
        return "This must be at most 2GB, the largest file Unicon takes."
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
            if name in _TUPLE_KEYS:
                problems.add(
                    ("inputs", index),
                    f"{name} is a key a tuple may carry, so a repeated input can't be named "
                    f"{name}. Rename the input in the workflow.",
                )
            if name in seen:
                problems.add(("inputs", index), already_listed(name))
            seen.add(name)
        titles: set[str] = set()
        for index, row in enumerate(self.tuples):
            for path, message in self._row_problems(row):
                problems.add(("tuples", index, *path), message)
            if not isinstance(row, dict):
                continue
            if "title" in row:
                problem = _title_problem(row["title"])
                if problem is not None:
                    problems.add(("tuples", index, "title"), problem)
                elif row["title"] in titles:
                    problems.add(
                        ("tuples", index, "title"),
                        f"Another tuple of this axis is already called {row['title']}. "
                        "Choose another title.",
                    )
                else:
                    titles.add(row["title"])
            if "max_size" in row:
                holds = any(row.get(name) == ENTRANT for name in self.inputs)
                if not holds:
                    problems.add(
                        ("tuples", index, "max_size"),
                        "A tuple carries max_size only when it holds entrant, which is the "
                        "part that size limits. Remove it.",
                    )
                else:
                    problem = _size_problem(row["max_size"])
                    if problem is not None:
                        problems.add(("tuples", index, "max_size"), problem)
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
                if key in _TUPLE_KEYS:
                    continue
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

    @property
    def titles(self) -> list[str | None]:
        """Each tuple's title, none where it gives none. A title is not a
        value of the run.
        """
        found: list[str | None] = []
        for row in self.tuples:
            title = row.get("title") if isinstance(row, dict) else None
            found.append(title if isinstance(title, str) else None)
        return found

    @property
    def sizes(self) -> list[int | None]:
        """Each tuple's `max_size` in bytes, none where it gives none."""
        found: list[int | None] = []
        for row in self.tuples:
            if isinstance(row, dict) and "max_size" in row:
                found.append(parse_size(row["max_size"]))
            else:
                found.append(None)
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
    `verdict` every run must get, a number or text for each value named, or both.
    What each names is checked against the task at its save
    (`forge.domain.checks`).
    """

    submit: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(min_length=1)


class TaskDefinition(Model):
    """A `task.yaml` (SIMPLER-CORE.md 8 and 9): the workflow it runs, once
    per combination of `repeat`'s tuples, the inputs it gives that workflow
    or makes the entrant's, its `scoring`, which every task has, and the
    `checks` graded at its save. `inputs` defaults to none, and
    `submissions` to 50 at one every 30 seconds.
    """

    name: Line
    workflow: Ref
    inputs: dict[str, Any] = Field(default_factory=dict)
    repeat: list[Axis] = Field(default_factory=list)
    scoring: Scoring
    checks: list[Check] = Field(default_factory=list)
    submissions: Submissions = Submissions()
