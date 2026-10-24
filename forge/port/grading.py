"""Grading runs at the CI: setting an org up there and keeping what it holds
fresh, activating the org's dispatch place once, then starting grading runs
on it as the org account the caller hands in, finding where a run is and
cancelling it, and, for a CI that asks the platform what a run is as it
starts one, the answer. What the org account holds at the CI is a `CiState`
the implementation alone reads; the platform stores it, encrypted, and
locks it while it is refreshed.

A grading's run is described to the port as a `PipelineRun`, in the
platform's words, with a `PipelineSpec` of its steps (phase 4.9); the
implementation writes them in the CI's words.
"""

from datetime import datetime
from typing import Protocol

from forge.domain.grading import (
    InboundAnswer,
    InboundRequest,
    PipelineLookup,
    PipelineRun,
    RunState,
)
from forge.domain.identity import AsOrgAccount, CiState, OrgAccountRef
from forge.domain.ids import OrgId, RunId


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

    async def activate_dispatch(self, as_: AsOrgAccount, org: OrgId) -> bool:
        """Switch the CI on for the org's dispatch place, which the content
        port made, as the org's account, so a run task's grading runs can
        start on it, and say whether the CI did not know it before.
        Activating twice changes nothing. `Forbidden` when `as_` is another
        org's account.
        """
        ...

    async def start_pipeline(self, as_: AsOrgAccount, run: PipelineRun) -> RunId:
        """Start a run task's grading run on the org's dispatch place as
        the org's account, with its variables. The steps are not sent: a CI
        that asks is answered from the platform's records. `Rejected` when
        the CI answers without a run; `Unavailable` when no answer came
        back; `NotFound` when the dispatch place is not activated;
        `Forbidden` when `as_` is another org's account.
        """
        ...

    async def cancel_run(self, run: RunId) -> None:
        """Stop the run at the CI. `NotFound` when there is no such run."""
        ...

    async def run_state(self, run: RunId) -> RunState:
        """Where the CI has the run: waiting for a machine, taken by one,
        finished, or `lost`, a run the CI holds no more though it was never
        finished, or does not know. The implementation may answer from a view
        a few seconds old.
        """
        ...

    async def answer(
        self, request: InboundRequest, lookup: PipelineLookup, *, now: datetime
    ) -> InboundAnswer:
        """For a CI that asks the platform what a run is as it starts one:
        the answer to `request`, once it is found to be the CI's own and
        fresh at `now`, for the grading run on an org's dispatch place
        `lookup` gives, and only when it was started with exactly the
        variables the implementation starts it with. The answer is its
        steps: the submission checked out by the spec's clone image unless
        it is a save, then each step with its settings, and the report's
        last, on a machine carrying the run's labels; the same every time
        for the same run. `Forbidden` for a request the CI did not sign, one
        changed since or a stale one; `Rejected` for one that is no such
        question, or names other variables; whatever `lookup` raises; and
        `NotFound` from a CI that is handed every run whole and never asks.
        """
        ...
