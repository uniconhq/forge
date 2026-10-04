"""Grading runs at the CI: activating a task once, then starting runs as the
org account the caller hands in, cancelling runs as the CI's administrator,
and the two things the CI and
the platform say to each other about a run: the CI's signed question of
what a run is, and the answer with its steps. The CI admits only accounts
it was told about, so the org account's user there is made by the CI's
administrator and its credential is minted by signing the account in,
unattended, with its password at the host.

A run is described to the port as a `GradingRun`, in the platform's words;
the implementation writes it in the CI's: the variables a run is started
with, the steps of its answer, and what its envelope says of where it is.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from forge.domain.grading import CiAnswer, CiRequest, ConfigAsk, GradingRun, RunPlaces
from forge.domain.identity import AsOrgAccount
from forge.domain.ids import RunId, TaskId


class GradingPort(Protocol):
    async def activate(self, as_: AsOrgAccount, task: TaskId) -> None:
        """Switch the CI on for the task, as the task's org account, so it can
        be graded. Activating twice changes nothing. `Forbidden` when `as_`
        is another org's account.
        """
        ...

    async def deactivate(self, as_: AsOrgAccount, task: TaskId) -> None:
        """Switch the CI off for the task and have it forget the task, as the
        task's org account. A task the CI does not know changes nothing.
        `Forbidden` when `as_` is another org's account.
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
        answers without a run; `Unavailable` when no answer came back;
        `NotFound` when the task is not activated at the CI; `Forbidden` when
        `as_` is another org's account.
        """
        ...

    async def cancel_run(self, run: RunId) -> None:
        """Stop the run at the CI. `NotFound` when there is no such run."""
        ...

    async def read_config_request(self, request: CiRequest, *, now: datetime) -> ConfigAsk:
        """The CI's question of what a run is, once its signature is checked
        against the CI's own key and found fresh at `now`. `Forbidden` for a
        request the CI did not sign, one changed since, or a stale one;
        `Rejected` for a signed body that is not such a question.
        """
        ...

    def config_answer(
        self, run: GradingRun, ask: ConfigAsk, *, harness_image: str, clone_image: str
    ) -> CiAnswer:
        """The answer to `ask` for `run`: check the task out at the version its
        publication froze with its large files, check the submission out at
        its version, both with `clone_image` and the machine's shared store
        of large files, and run `harness_image` with the socket filter's
        socket and no credential, on a machine carrying the run's label. The
        same every time for the same run. No call is made.
        """
        ...

    def run_places(self, run: GradingRun) -> RunPlaces:
        """Where the run is at the forge and on the machine, which the CI's
        answer and the envelope are made from. No call is made.
        """
        ...

    async def create_ci_user(self, username: str) -> int:
        """Make the account's user at the CI, as the CI's administrator, and
        return its id there. A user that exists is found instead.
        """
        ...

    async def delete_ci_user(self, username: str) -> None:
        """Remove the account's user at the CI, as the CI's administrator. A
        user not there changes nothing.
        """
        ...

    async def mint_ci_token(self, username: str, forge_password: str) -> str:
        """Sign the account in at the CI through the host, unattended, with
        its password at the host, and mint the credential the CI takes from
        it afterwards. `Forbidden` when the host refuses the password.
        """
        ...
