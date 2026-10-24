"""Making a task, listing a contest's tasks, where its files stand against
its publications, and the contest's tasks each with that and its timeline,
taking a task out of its contest and putting a withdrawn one back, for an
organiser the host's guard produced, and the id a task in a contest is
known by.
"""

from forge.domain.roles import task_id_of
from forge.services.taking_out import PutBack, TakeOut, put_back, take_out
from forge.services.tasks import (
    TaskStanding,
    TaskState,
    Timeline,
    create,
    list_tasks,
    standing,
    state,
)

__all__ = [
    "PutBack",
    "TakeOut",
    "TaskStanding",
    "TaskState",
    "Timeline",
    "create",
    "list_tasks",
    "put_back",
    "standing",
    "state",
    "take_out",
    "task_id_of",
]
