"""The save of a task, which publishes it when it is valid, the publications
it has, what a save comes back as, and the inputs and test fields of the
workflow the task names, which its form is built from.
"""

from forge.domain.publications import Publication
from forge.services.publications import (
    DeclaredField,
    DeclaredInput,
    Draft,
    Published,
    WorkflowForm,
    list,
    save,
    workflow_form,
)

__all__ = [
    "DeclaredField",
    "DeclaredInput",
    "Draft",
    "Publication",
    "Published",
    "WorkflowForm",
    "list",
    "save",
    "workflow_form",
]
