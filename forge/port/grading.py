"""Grading runs at the CI: registering a task once, then starting, reading
and cancelling runs as the org account.
"""

from collections.abc import Mapping
from typing import Protocol

from forge.domain.grading import Run
from forge.domain.ids import RunId, TaskId


class GradingPort(Protocol):
    async def register(self, task: TaskId) -> None:
        """Make the task known to the CI, as the org account, so it can be
        graded. Registering twice changes nothing.
        """
        ...

    async def start_run(
        self, task: TaskId, *, variables: Mapping[str, str], compute_label: str
    ) -> RunId:
        """Start a grading run of the task as the org account, pinned to the
        compute carrying the label. `Unavailable` when the CI answers without
        a run.
        """
        ...

    async def read_run(self, run: RunId) -> Run:
        """`NotFound` when there is no such run."""
        ...

    async def cancel_run(self, run: RunId) -> None: ...
