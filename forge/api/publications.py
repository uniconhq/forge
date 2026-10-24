"""The save of a task, which publishes it when it is valid, the publications
it has, what a save comes back as and what it did, and the inputs of the
workflow the task names, which its form is built from.
"""

from forge.domain.publication_record import CheckRun, FailedCheck, Publication
from forge.domain.save_effect import SaveEffect
from forge.services.task_save import (
    DeclaredInput,
    Draft,
    Published,
    WorkflowForm,
    list_publications,
    save,
    workflow_form,
)

__all__ = [
    "CheckRun",
    "DeclaredInput",
    "Draft",
    "FailedCheck",
    "Publication",
    "Published",
    "SaveEffect",
    "WorkflowForm",
    "list_publications",
    "save",
    "workflow_form",
]
