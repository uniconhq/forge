import re
from collections.abc import Mapping
from dataclasses import dataclass

from forge.domain.definitions import AFTER_DEADLINE, AFTER_RELEASE, ALWAYS
from forge.domain.plan_basics import PLANS_FOLDER

RUNS_PATH = f"{PLANS_FOLDER}runs.json"
"""Where a task's publication keeps its runs and what its next version
reads back: each key's id, the entrant's inputs and the self-reported
columns."""
MOST_RUNS = 1000
"""The most runs a task can have: every combination of its tuples, each run
graded for each submission."""
RUN_ID = re.compile(r"[A-Za-z0-9_-]+")
"""What a run's id is made of, as a runs file holds it and a path names it."""
OPENNESS: Mapping[str, int] = {AFTER_RELEASE: 0, AFTER_DEADLINE: 1, ALWAYS: 2}
"""How soon each moment of `show` shows an entry, latest first: what a
release shows is shown at no set time."""
RELEASED_OPENNESS: Mapping[str, int] = {AFTER_DEADLINE: 0, AFTER_RELEASE: 1, ALWAYS: 2}
"""The same while the task's results are released and its deadline has not
passed: what the release shows is shown now, and `after_deadline` not yet."""
SHOWN_WORDS = {
    ALWAYS: "as soon as a grading ends",
    AFTER_DEADLINE: "after the deadline",
    AFTER_RELEASE: "once results are released",
}
SECRETS_LATER = (
    "Secrets come with org secrets, which Unicon does not hold yet, so a task cannot give "
    "one. Give this input a value instead."
)


@dataclass(frozen=True, slots=True)
class Part:
    """An entrant input's value in a tuple: the tuple's position, from 1."""

    number: int

    def __str__(self) -> str:
        return f"Part {self.number}"
