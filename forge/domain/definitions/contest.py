from datetime import datetime
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import Field, PlainValidator, model_validator

from forge.domain.definitions.basics import ContestVisibility, State, TaskHandle
from forge.domain.definitions.entry_ways import Entrants, Registration
from forge.domain.definitions.leaderboard_settings import Leaderboard
from forge.domain.yaml_models import AwareTime, Line, Model, Problems, aware_time, fail


def _hold_end(value: object) -> datetime | Literal["staff"]:
    if value == "staff":
        return "staff"
    try:
        return aware_time(value)
    except ValueError as wrong:
        raise ValueError(f"{wrong} Or give staff, for a hold staff end themselves.") from None


class Hold(Model):
    """The contest's hold, the freeze (SIMPLER-CORE.md 10.2 rule 10): from
    `from`, a time, until `until`, a time or `staff`, who end it. That
    `from` lies inside the contest, and is not set to a time passed, is the
    save's (`forge.domain.board_checks.hold_problems`)."""

    from_: AwareTime = Field(alias="from")
    until: Annotated[datetime | Literal["staff"], PlainValidator(_hold_end)]

    @model_validator(mode="after")
    def _check(self) -> Hold:
        if not isinstance(self.until, str) and self.until <= self.from_:
            fail([(("until",), "The hold must end after it starts.")])
        return self


class ContestTask(Model):
    """A task's whole timeline in the contest: when it is released, falls
    due and closes, and how many of its submissions a row may mark. Every
    time but `due` defaults to the contest's own. A `withdrawn` task stays in the list, keeping its
    place and so its letter, but entrants no longer see it or submit to it
    and no leaderboard counts it; its submissions stay for the organisers,
    and taking the key out puts it back as it was (#450).
    """

    id: TaskHandle
    release_at: AwareTime | None = None
    due: AwareTime | None = None
    closes: AwareTime | None = None
    marks: Annotated[int, Field(strict=True, ge=1, le=10)] | None = None
    withdrawn: Annotated[bool, Field(strict=True)] = False


_TIMES = {
    "start": "The contest's start",
    "release_at": "The release time",
    "due": "The due time",
    "closes": "The closing time",
    "end": "The contest's end",
}
"""A task's times as a person names them, each starting a sentence."""


class ContestDefinition(Model):
    """A `contest.yaml`. `registration` defaults to open, anyone signing up
    and an organiser approving each, `entrants` to one person each,
    `leaderboards` and `tasks` to none, and there is no `hold`. Boards are
    keyed by a name tasks and boards share, so a column reads `a.b`
    knowing which `a` is.
    """

    name: Line
    description: str | None = None
    start: AwareTime
    end: AwareTime
    state: State
    visibility: ContestVisibility
    registration: Registration = Registration()
    entrants: Entrants = Entrants()
    leaderboards: dict[TaskHandle, Leaderboard] = Field(default_factory=dict)
    hold: Hold | None = None
    tasks: tuple[ContestTask, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> ContestDefinition:
        problems = Problems()
        if self.end <= self.start:
            problems.add(("end",), "The end must be after the start.")
        opens, closes = self.registration.opens, self.registration.closes
        if opens is not None and closes is not None and closes <= opens:
            problems.add(("registration", "closes"), "Registration must close after it opens.")
        problems.duplicates([task.id for task in self.tasks], ("tasks",))
        known = {task.id for task in self.tasks}
        for key in self.leaderboards:
            if key in known:
                problems.add(
                    ("leaderboards", key),
                    f"{key} is a task of the contest, and tasks and leaderboards share one set "
                    "of names, so a column can read either. Name the leaderboard otherwise.",
                )
        for index, entry in enumerate(self.tasks):
            for path, message in self._entry_problems(entry):
                problems.add(("tasks", index, *path), message)
        problems.raise_any()
        return self

    def _entry_problems(self, entry: ContestTask) -> list[tuple[tuple[str, ...], str]]:
        """C1 as far as the file alone says: the four times in order. Marks
        only under a board reading the task's `marked` is the board check's
        (`board_checks`).
        """
        found: list[tuple[tuple[str, ...], str]] = []
        # An absent release_at or closes is read at its default and named as
        # the contest's start or end, so a problem is never at a key the
        # entry does not write.
        times = [
            ("start", self.start),
            ("release_at" if entry.release_at is not None else "start", self.release_of(entry)),
            ("due", entry.due),
            ("closes" if entry.closes is not None else "end", self.closes_of(entry)),
            ("end", self.end),
        ]
        chain = [(key, at) for key, at in times if at is not None]
        for (before, earlier), (key, later) in pairwise(chain):
            if later >= earlier:
                continue
            if key == "end":
                found.append(
                    ((before,), f"{_TIMES[before]} is after the contest's end. Make it earlier.")
                )
            elif before == "start":
                found.append(
                    ((key,), f"{_TIMES[key]} is before the contest's start. Make it later.")
                )
            else:
                found.append(
                    (
                        (key,),
                        f"{_TIMES[key]} is before {_TIMES[before].lower()}. A task is released, "
                        "falls due and closes in that order, within the contest.",
                    )
                )
        return found

    def entry(self, task: str) -> ContestTask | None:
        """The task's entry in `tasks`, or none when the contest lists it not."""
        return next((entry for entry in self.tasks if entry.id == task), None)

    def release_of(self, entry: ContestTask) -> datetime:
        return entry.release_at if entry.release_at is not None else self.start

    def closes_of(self, entry: ContestTask) -> datetime:
        return entry.closes if entry.closes is not None else self.end

    def label_of(self, task: str) -> str | None:
        """The task's label, its position as a letter, A, B, ..., Z, AA, ...;
        none when the contest does not list it.
        """
        for index, entry in enumerate(self.tasks):
            if entry.id == task:
                return letters(index)
        return None


def letters(index: int) -> str:
    """The label of the entry at `index`: A, B, ..., Z, AA, AB, ..."""
    label = ""
    count = index + 1
    while count:
        count, rest = divmod(count - 1, 26)
        label = chr(ord("A") + rest) + label
    return label
