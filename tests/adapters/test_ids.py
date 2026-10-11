"""Every kind of name either implementation builds is parsed back, the ids
they hand out round-trip, and an id from elsewhere names nothing.
"""

import uuid

import pytest

from forge.adapters.ids import (
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
from forge.domain.ids import ContestId, TaskId, WorkspaceId
from forge.domain.names import EntrantOwner

ENTRANT = uuid.UUID("0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31")
OWNER = EntrantOwner(ENTRANT)
SEGMENT = "entrant.0199a2c16b7e7c3a9f105d2e4b8a6c31"


def test_contest_and_task_names_end_in_their_word() -> None:
    assert ContestRef("acme", "spring").repo == "spring.contest"
    assert TaskRef("acme", "spring", "sum").repo == "spring.sum.task"


def test_workspace_names_carry_the_owner_between_the_second_dot_and_the_word() -> None:
    entrant = WorkspaceRef("acme", "spring", OWNER)
    assert entrant.desk_repo == f"spring.{SEGMENT}.desk"
    assert WorkspaceRef("acme", "autumn", OWNER).desk_repo != entrant.desk_repo
    assert entrant.submission_repo("sum") == f"spring.sum.{SEGMENT}.sub"


def test_the_longest_name_a_workspace_makes_fits_the_forge() -> None:
    key = "a" * 26
    longest = WorkspaceRef("acme", key, EntrantOwner(uuid.UUID(int=2**128 - 1)))

    assert len(longest.submission_repo(key)) == 98
    assert len(longest.submission_repo(key)) <= 100


def test_ids_round_trip() -> None:
    contest = ContestRef("acme", "spring")
    task = TaskRef("acme", "spring", "sum")
    workspace = WorkspaceRef("acme", "spring", OWNER)
    assert parse_contest(contest.id) == contest
    assert parse_task(task.id) == task
    assert parse_workspace(workspace.id) == workspace
    assert parse_publication(publication_id(task, 3)) == (task, 3)
    assert parse_submission(submission_id(workspace, "sum", 2)) == (workspace, "sum", 2)
    assert parse_thread(thread_id("acme", "spring.contest", 9)) == ("acme", "spring.contest", 9)


def test_a_workspace_id_is_told_apart_from_a_task_id() -> None:
    assert is_workspace(WorkspaceRef("acme", "spring", OWNER).id)
    assert not is_workspace(TaskRef("acme", "spring", "sum").id)
    with pytest.raises(MalformedId):
        parse_workspace(WorkspaceId("acme/spring/sum"))


def test_a_workspace_owner_round_trips_through_its_segment() -> None:
    assert OWNER.segment == SEGMENT
    assert owner_from_segment(SEGMENT) == OWNER
    for segment in (
        f"entrant.{ENTRANT}",
        SEGMENT.upper(),
        SEGMENT[:-1],
        f"team.{ENTRANT}",
        "entrant.not-an-id",
        "ada",
        "u7",
    ):
        with pytest.raises(MalformedId):
            owner_from_segment(segment)


def test_a_place_is_located_at_its_repository() -> None:
    assert location("acme/spring") == ("acme", "spring.contest")
    assert location("acme/spring/sum") == ("acme", "spring.sum.task")
    assert location(f"acme/spring/@{SEGMENT}") == ("acme", f"spring.{SEGMENT}.desk")


def test_an_id_from_elsewhere_is_refused() -> None:
    with pytest.raises(MalformedId):
        parse_contest(ContestId("acme"))
    with pytest.raises(MalformedId):
        parse_task(TaskId("acme/spring"))


def test_a_task_is_read_back_from_its_repository() -> None:
    assert task_of_repo("acme", "spring.sum.task") == TaskRef("acme", "spring", "sum")
    for name in (
        "spring.contest",
        f"spring.sum.{SEGMENT}.sub",
        "sum.task",
        "spring.sum.task.x",
        ".task",
    ):
        with pytest.raises(MalformedId):
            task_of_repo("acme", name)
