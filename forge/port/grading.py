"""Grading runs at the CI: registering a task once and starting runs as the
org account the caller hands in, then reading and cancelling runs as the CI's
administrator. The CI admits only accounts it was told about, so the org
account's user there is made by the CI's administrator and its credential is
minted by signing the account in, unattended, with its password at the host.
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
