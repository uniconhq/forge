"""Every kind of name either implementation builds is parsed back, the ids
they hand out round-trip, and an id from elsewhere names nothing.
"""

import uuid

import pytest

from forge.domain.ids import ContestId, TaskId, WorkspaceId
from forge.domain.names import TeamOwner, UserOwner
from forge.forges.ids import (
    ContestRef,
    MalformedId,
    TaskRef,
    WorkspaceRef,
    is_workspace,
    location,
    owner_from_segment,
    parse_contest,
    parse_publication,
    parse_submission,
    parse_task,
    parse_thread,
    parse_workspace,
    publication_id,
    submission_id,
    task_of_repo,
    thread_id,
)

TEAM = uuid.UUID("0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31")


def test_contest_and_task_names_end_in_their_word() -> None:
    assert ContestRef("acme", "spring").repo == "spring.contest"
    assert TaskRef("acme", "spring", "sum").repo == "spring.sum.task"


def test_workspace_names_carry_the_owner_between_the_second_dot_and_the_word() -> None:
    person = WorkspaceRef("acme", "spring", UserOwner("Ada.Lovelace"))
    assert person.desk_repo == "spring.ada.lovelace.desk"
    assert WorkspaceRef("acme", "autumn", UserOwner("ada")).desk_repo != person.desk_repo
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


def test_a_workspace_owner_round_trips_through_its_segment() -> None:
    team = TeamOwner(TEAM)
    assert owner_from_segment(team.segment) == team
    assert owner_from_segment(UserOwner("Ada.Lovelace").segment) == UserOwner("ada.lovelace")
    with pytest.raises(MalformedId):
        owner_from_segment("team.not-a-uuid")


def test_a_place_is_located_at_its_repository() -> None:
    assert location("acme/spring") == ("acme", "spring.contest")
    assert location("acme/spring/sum") == ("acme", "spring.sum.task")
    assert location("acme/spring/@ada") == ("acme", "spring.ada.desk")


def test_an_id_from_elsewhere_is_refused() -> None:
    with pytest.raises(MalformedId):
        parse_contest(ContestId("acme"))
    with pytest.raises(MalformedId):
        parse_task(TaskId("acme/spring"))


def test_a_task_is_read_back_from_its_repository() -> None:
    assert task_of_repo("acme", "spring.sum.task") == TaskRef("acme", "spring", "sum")
    for name in ("spring.contest", "spring.sum.bob.sub", "sum.task", "spring.sum.task.x", ".task"):
        with pytest.raises(MalformedId):
            task_of_repo("acme", name)
