"""Grading runs at the CI: registering a task once and starting runs as the
org account the caller hands in, then reading and cancelling runs as the CI's
administrator.
"""

from collections.abc import Mapping
from typing import Protocol

from forge.domain.grading import Run
from forge.domain.identity import AsOrgAccount
from forge.domain.ids import RunId, TaskId


class GradingPort(Protocol):
    async def register(self, as_: AsOrgAccount, task: TaskId) -> None:
        """Make the task known to the CI, as the task's org account, so it can
        be graded. Registering twice changes nothing. `Forbidden` when `as_`
        is another org's account.
        """
        ...

    async def start_run(
        self, as_: AsOrgAccount, task: TaskId, *, variables: Mapping[str, str], compute_label: str
    ) -> RunId:
        """Start a grading run of the task as its org account, pinned to the
        compute carrying the label. `Unavailable` when the CI answers without
        a run; `Forbidden` when `as_` is another org's account.
        """
        ...

    async def read_run(self, run: RunId) -> Run:
        """`NotFound` when there is no such run."""
        ...

    async def cancel_run(self, run: RunId) -> None: ...
