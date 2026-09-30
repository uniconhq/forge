"""Grading runs at the CI: activating a task once, then starting runs as the
org account the caller hands in, finding one already started, and reading
and cancelling runs as the CI's administrator. The CI admits only accounts
it was told about, so the org account's user there is made by the CI's
administrator and its credential is minted by signing the account in,
unattended, with its password at the host.

A run is described to the port as a `GradingRun`, in the platform's words;
the implementation writes it in the CI's: the variables a run is started
with.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from forge.domain.grading import GradingRun, Run
from forge.domain.identity import AsOrgAccount
from forge.domain.ids import RunId, TaskId


class GradingPort(Protocol):
    async def activate(self, as_: AsOrgAccount, task: TaskId) -> None:
        """Switch the CI on for the task, as the task's org account, so it can
        be graded. Activating twice changes nothing. `Forbidden` when `as_`
        is another org's account.
        """
        ...

    def run_variables(self, run: GradingRun) -> Mapping[str, str]:
        """The variables `run` is started with, the same every time for the
        same run. No call is made.
        """
        ...

    async def start_run(self, as_: AsOrgAccount, run: GradingRun) -> RunId:
        """Start `run` at the CI as the task's org account, with its variables,
        pinned to the machines carrying its label. `Rejected` when the CI
        answers without a run, which runs nothing, though the CI may keep a
        run that ended at once; `Unavailable` when no answer came back, which
        may have started one; `NotFound` when the
        task is not activated at the CI; `Forbidden` when `as_` is another
        org's account.
        """
        ...

    async def find_run(self, as_: AsOrgAccount, run: GradingRun, *, since: datetime) -> Run | None:
        """The newest run of the task started since `since` with `run`'s
        grading id, with where it stands, or none, as the task's org account.
        This is how a start whose answer was lost is found before another is
        sent.
        """
        ...

    async def read_run(self, run: RunId) -> Run:
        """`NotFound` when there is no such run."""
        ...

    async def cancel_run(self, run: RunId) -> None:
        """Stop the run at the CI. `NotFound` when there is no such run."""
        ...

    async def create_ci_user(self, username: str) -> int:
        """Make the account's user at the CI, as the CI's administrator, and
        return its id there. A user that exists is found instead.
        """
        ...

    async def mint_ci_token(self, username: str, forge_password: str) -> str:
        """Sign the account in at the CI through the host, unattended, with
        its password at the host, and mint the credential the CI takes from
        it afterwards. `Forbidden` when the host refuses the password.
        """
        ...

    async def ci_user_is_alive(self, as_: AsOrgAccount) -> bool:
        """Whether the CI still answers the org account under its credential.
        The call itself is what keeps the account's copy at the CI fresh.
        """
        ...
