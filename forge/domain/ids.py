"""Identifiers. Rows the package owns are keyed by UUID v7, which sorts by
creation time. Everything the forge holds is referred to by an opaque id the
port hands out: the package stores and compares those ids and never reads
inside one.
"""

import uuid
from typing import NewType

OrgName = NewType("OrgName", str)
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
