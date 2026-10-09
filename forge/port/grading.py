"""Grading runs at the CI: setting an org up there and keeping what it holds
fresh, activating a task once, then starting runs as the org account the
caller hands in, finding where a run is and cancelling it, and, for a CI
that asks the platform what a run is as it starts one, the answer. What the
org account holds at the CI is a `CiState` the implementation alone reads;
the platform stores it, encrypted, and locks it while it is refreshed.

A run is described to the port as a `GradingRun`, in the platform's words,
with a `RunSpec` of what it runs; the implementation writes them in the
CI's words, and says where the run's checkouts are (`run_places`).
"""

from datetime import datetime
from typing import Protocol

from forge.domain.grading import (
    GradingRun,
    InboundAnswer,
    InboundRequest,
    RunLookup,
    RunPlaces,
    RunSpec,
    RunState,
)
from forge.domain.identity import AsOrgAccount, CiState, OrgAccountRef
from forge.domain.ids import OrgId, RunId, TaskId


class GradingPort(Protocol):
    async def set_up_org(self, org: OrgId, account: OrgAccountRef) -> CiState:
        """Set the org up at the CI for `account`, its service account at the
        forge, and give back what the account holds there. Setting up again
        finds what was made before.
        """
        ...

    async def tear_down_org(self, org: OrgId, state: CiState) -> None:
        """Remove what the org was given at the CI. `state` may be empty, for
        a set-up that stopped partway; what the set-up makes is removed
        either way, and what is not there changes nothing.
        """
        ...

    def needs_refresh(self, state: CiState, now: datetime) -> bool:
        """Whether `state` is to be refreshed before it is used at `now`. No
        call is made.
        """
        ...

    async def refresh(self, org: OrgId, state: CiState) -> CiState:
        """What the org account holds at the CI once refreshed: before it
        goes stale, or after the CI refused it.
        """
        ...

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

    async def start_run(self, as_: AsOrgAccount, run: GradingRun, spec: RunSpec) -> RunId:
        """Start `run` at the CI as the task's org account, with its variables,
        pinned to the machines carrying its label, to run what `spec` says.
        `Rejected` when the CI
        answers without a run; `Unavailable` when no answer came back;
        `NotFound` when the task is not activated at the CI; `Forbidden` when
        `as_` is another org's account.
        """
        ...

    async def cancel_run(self, run: RunId) -> None:
        """Stop the run at the CI. `NotFound` when there is no such run."""
        ...

    async def run_state(self, run: RunId) -> RunState:
        """Where the CI has the run, as the CI's administrator, told by the
        queue it is in rather than its status, which is the same for a run
        waiting for a machine and a run the CI dropped. A run the CI does not
        know is `lost`. The implementation may answer from a view of the
        queue a few seconds old.
        """
        ...

    async def answer(
        self, request: InboundRequest, lookup: RunLookup, *, now: datetime
    ) -> InboundAnswer:
        """For a CI that asks the platform what a run is as it starts one: the
        answer to `request`, once it is found to be the CI's own and fresh at
        `now`, for the run `lookup` gives, and only when that run was started
        with exactly the variables the implementation starts it with. The
        answer checks the task out at the version its publication froze with
        its large files and the submission at its version, both with the
        spec's clone image, and runs the spec's harness image as the
        runner's machine contract says, on a machine carrying the run's
        label; the same every time for the same run. `Forbidden` for a
        request the CI did not sign, one changed since or a stale one;
        `Rejected` for one that is no such question, or names other
        variables; whatever `lookup` raises; and `NotFound` from a CI that is
        handed every run whole and never asks.
        """
        ...

    def run_places(self, run: GradingRun) -> RunPlaces:
        """Where the run is at the forge and on the machine, which the CI's
        answer and the envelope are made from. No call is made.
        """
        ...
