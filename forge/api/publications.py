"""The save of a task, which publishes it when it is valid, the publications
it has, what a save comes back as and what it did, and the inputs of the
workflow the task names, which its form is built from.
"""

from forge.domain.publication_record import CheckRun, FailedCheck, Publication
from forge.domain.save_effect import SaveEffect
from forge.services.task_save import (
    DeclaredInput,
    Draft,
    Preparing,
    Published,
    WorkflowForm,
    list_publications,
    save,
    workflow_form,
)
from forge.services.task_save.preparing import Preparation
from forge.services.task_save.preparing import read as preparation

__all__ = [
    "CheckRun",
    "DeclaredInput",
    "Draft",
    "FailedCheck",
    "Preparation",
    "Preparing",
    "Publication",
    "Published",
    "SaveEffect",
    "WorkflowForm",
    "list_publications",
    "preparation",
    "save",
    "workflow_form",
]
