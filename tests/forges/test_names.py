"""Every kind of name the Forgejo implementation builds is parsed back, and
the ids it hands out round-trip.
"""

import uuid

import pytest

from forge.domain.ids import ContestId, TaskId, WorkspaceId
from forge.domain.names import TeamOwner, UserOwner
from forge.forges.forgejo.names import (
    ContestRef,
    MalformedId,
    TaskRef,
    WorkspaceRef,
    is_workspace,
    parse_contest,
    parse_publication,
    parse_submission,
    parse_task,
    parse_thread,
    parse_workspace,
    publication_id,
    submission_id,
    thread_id,
)

TEAM = uuid.UUID("0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31")


def test_contest_and_task_names_end_in_their_word() -> None:
    assert ContestRef("acme", "spring").repo == "spring.contest"
    assert TaskRef("acme", "spring", "sum").repo == "spring.sum.task"


def test_workspace_names_carry_the_owner_between_the_second_dot_and_the_word() -> None:
    person = WorkspaceRef("acme", "spring", UserOwner("Ada.Lovelace"))
    assert person.desk_repo == "ada.lovelace.desk"
    assert person.submission_repo("sum") == "spring.sum.ada.lovelace.sub"
    team = WorkspaceRef("acme", "spring", TeamOwner(TEAM))
    assert team.submission_repo("sum") == f"spring.sum.team.{TEAM}.sub"


def test_ids_round_trip() -> None:
    contest = ContestRef("acme", "spring")
    task = TaskRef("acme", "spring", "sum")
    workspace = WorkspaceRef("acme", "spring", UserOwner("ada"))
    assert parse_contest(contest.id) == contest
    assert parse_task(task.id) == task
    assert parse_workspace(workspace.id) == workspace
    assert parse_publication(publication_id(task, 3)) == (task, 3)
    assert parse_submission(submission_id(workspace, "sum", 2)) == (workspace, "sum", 2)
    assert parse_thread(thread_id("acme", "spring.contest", 9)) == ("acme", "spring.contest", 9)


def test_a_workspace_id_is_told_apart_from_a_task_id() -> None:
    assert is_workspace(WorkspaceRef("acme", "spring", UserOwner("ada")).id)
    assert not is_workspace(TaskRef("acme", "spring", "sum").id)
    with pytest.raises(MalformedId):
        parse_workspace(WorkspaceId("acme/spring/sum"))


def test_an_id_from_elsewhere_is_refused() -> None:
    with pytest.raises(MalformedId):
        parse_contest(ContestId("acme"))
    with pytest.raises(MalformedId):
        parse_task(TaskId("acme/spring"))
