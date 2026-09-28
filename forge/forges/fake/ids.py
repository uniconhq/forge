"""How the fake builds and reads the opaque ids it hands out. Its own shapes,
kept apart from the Forgejo implementation's on purpose.
"""

from forge.domain.ids import ContestId, SubmissionId, TaskId, WorkflowId, WorkspaceId
from forge.forges.fake.state import WORKSPACE_MARK


def contest(org: str, name: str) -> ContestId:
    return ContestId(f"{org}/{name}")


def contest_parts(value: ContestId) -> tuple[str, str]:
    org, name = value.split("/")
    return org, name


def contest_repo(name: str) -> str:
    return f"{name}.contest"


def task(contest_id: ContestId, name: str) -> TaskId:
    return TaskId(f"{contest_id}/{name}")


def task_parts(value: TaskId) -> tuple[str, str, str]:
    org, contest_name, name = value.split("/")
    return org, contest_name, name


def task_repo(contest_name: str, name: str) -> str:
    return f"{contest_name}.{name}.task"


def workspace(contest_id: ContestId, segment: str) -> WorkspaceId:
    return WorkspaceId(f"{contest_id}/{WORKSPACE_MARK}{segment}")


def workspace_parts(value: WorkspaceId) -> tuple[str, str, str]:
    org, contest_name, owner = value.split("/")
    return org, contest_name, owner.removeprefix(WORKSPACE_MARK)


def is_workspace(value: str) -> bool:
    parts = value.split("/")
    return len(parts) == 3 and parts[2].startswith(WORKSPACE_MARK)


def desk_repo(contest_name: str, segment: str) -> str:
    return f"{contest_name}.{segment}.desk"


def submission_repo(contest_name: str, task_name: str, segment: str) -> str:
    return f"{contest_name}.{task_name}.{segment}.sub"


def submission(workspace_id: WorkspaceId, task_name: str, number: int) -> SubmissionId:
    return SubmissionId(f"{workspace_id}/{task_name}#{number}")


def workflow(owner: str, name: str) -> WorkflowId:
    return WorkflowId(f"{owner}/{name}")


def workflow_parts(value: WorkflowId) -> tuple[str, str]:
    owner, name = value.split("/")
    return owner, name


def workflow_repo(name: str) -> str:
    return f"{name}.workflow"
