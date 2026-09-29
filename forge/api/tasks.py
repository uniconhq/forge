"""Making a task, listing a contest's tasks, following how far making one has
got, and where its files stand against its publications, for an organiser
the host's guard produced, and the id a task in a contest is known by.
"""

from forge.domain.roles import task_id_of
from forge.services.provisioning import Record
from forge.services.tasks import TaskState, create, list, state, status

__all__ = ["Record", "TaskState", "create", "list", "state", "status", "task_id_of"]
