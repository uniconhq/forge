"""Grading runs and the agents that take them, as the port describes them."""

from dataclasses import dataclass
from enum import StrEnum

from forge.domain.ids import AgentId, RunId


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Run:
    id: RunId
    status: RunStatus


@dataclass(frozen=True, slots=True)
class Enrolment:
    """A newly enrolled agent and the token it identifies itself with. The
    token exists only in this value.
    """

    agent: AgentId
    token: str
