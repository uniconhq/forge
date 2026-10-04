"""Identifiers. Rows the package owns are keyed by UUID v7, which sorts by
creation time. Everything the forge holds is referred to by an opaque id the
port hands out: the package stores and compares those ids and never reads
inside one.

An org, a contest and a task are each filed under a key that never changes
(`forge.domain.keys`), and their ids are built from keys, never names: an
org's id is its key, a contest's `<org>/<contest>` and a task's
`<org>/<contest>/<task>`, each part a key. The names people give them are
labels, read from the `names` table.
"""

import uuid
from typing import NewType

OrgId = NewType("OrgId", str)
ContestId = NewType("ContestId", str)
TaskId = NewType("TaskId", str)
WorkspaceId = NewType("WorkspaceId", str)
VersionId = NewType("VersionId", str)
PublicationId = NewType("PublicationId", str)
SubmissionId = NewType("SubmissionId", str)
ThreadId = NewType("ThreadId", str)
WorkflowId = NewType("WorkflowId", str)
PrimitiveId = NewType("PrimitiveId", str)
RunId = NewType("RunId", str)
AgentId = NewType("AgentId", str)


def new_id() -> uuid.UUID:
    return uuid.uuid7()
