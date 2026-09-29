"""Released, visible and open, and who sees a contest at all, worked out from
the settings and the clock.
"""

from datetime import UTC, datetime, timedelta

import pytest

from forge.domain.definitions import ContestDefinition, TaskDefinition, parse_contest, parse_task
from forge.domain.release import (
    Closed,
    Openness,
    contest_visible_to,
    is_running,
    openness,
    released,
    visible,
)

START = datetime(2026, 6, 1, 9, tzinfo=UTC)
END = START + timedelta(hours=5)
DURING = START + timedelta(hours=1)
AFTER = END + timedelta(minutes=1)


def contest(
    state: str = "published", *, closed: bool = False, visibility: str = "public"
) -> ContestDefinition:
    return parse_contest(
        f"name: C\nstart: {START.isoformat()}\nend: {END.isoformat()}\nstate: {state}\n"
        f"submissions_closed: {str(closed).lower()}\nvisibility: {visibility}\n"
    )


def task(*, release_at: datetime | None = None, hidden: bool = False) -> TaskDefinition:
    text = f"name: T\nworkflow: unicon/classic@v1\nhidden: {str(hidden).lower()}\n"
    if release_at is not None:
        text += f"release_at: {release_at.isoformat()}\n"
    return parse_task(text)


def test_a_task_is_released_when_all_four_parts_hold() -> None:
    assert released(contest(), task(), START)
    assert released(contest(), task(release_at=DURING), DURING)


@pytest.mark.parametrize(
    ("the_contest", "the_task", "now"),
    [
        (contest("draft"), task(), DURING),
        (contest(), task(), START - timedelta(seconds=1)),
        (contest(), task(release_at=DURING), DURING - timedelta(seconds=1)),
        (contest(), task(hidden=True), DURING),
    ],
    ids=["contest-draft", "before-start", "before-release-at", "hidden"],
)
def test_a_task_is_not_released_with_any_part_missing(
    the_contest: ContestDefinition, the_task: TaskDefinition, now: datetime
) -> None:
    assert not released(the_contest, the_task, now)
    assert not visible(the_contest, the_task, now)
    assert openness(the_contest, the_task, now) == Openness(False, Closed.NOT_RELEASED)


def test_a_released_task_stays_visible_after_the_end_until_archived() -> None:
    assert visible(contest(), task(), AFTER)
    assert released(contest("archived"), task(), AFTER)
    assert not visible(contest("archived"), task(), AFTER)


def test_a_visible_task_is_open_until_the_end() -> None:
    assert openness(contest(), task(), DURING) == Openness(True, None)
    assert openness(contest(), task(), END - timedelta(seconds=1)).open
    assert openness(contest(), task(), END) == Openness(False, Closed.ENDED)


def test_an_archived_contest_takes_submissions_as_archived() -> None:
    assert openness(contest("archived"), task(), DURING) == Openness(False, Closed.ARCHIVED)


def test_the_closed_switch_stops_submissions_within_the_dates() -> None:
    assert openness(contest(closed=True), task(), DURING) == Openness(
        False, Closed.SUBMISSIONS_CLOSED
    )


def test_an_extension_covering_the_end_keeps_the_task_open_for_that_contestant() -> None:
    assert openness(contest(), task(), AFTER, extension=timedelta(minutes=30)).open
    assert openness(contest(), task(), AFTER, extension=timedelta(seconds=30)) == Openness(
        False, Closed.ENDED
    )
    assert openness(contest(), task(), AFTER) == Openness(False, Closed.ENDED)


def test_the_first_reason_that_applies_is_given() -> None:
    assert openness(contest("draft", closed=True), task(), AFTER).reason is Closed.NOT_RELEASED
    assert openness(contest("archived", closed=True), task(), AFTER).reason is Closed.ARCHIVED
    assert openness(contest(closed=True), task(), AFTER).reason is Closed.ENDED


READERS = {
    "visitor": {"has_session": False, "is_contestant": False, "is_organiser": False},
    "signed-in": {"has_session": True, "is_contestant": False, "is_organiser": False},
    "contestant": {"has_session": True, "is_contestant": True, "is_organiser": False},
    "organiser": {"has_session": True, "is_contestant": False, "is_organiser": True},
}


@pytest.mark.parametrize(
    ("state", "visibility", "sees"),
    [
        ("published", "public", {"visitor", "signed-in", "contestant", "organiser"}),
        ("published", "signed-in", {"signed-in", "contestant", "organiser"}),
        ("published", "hidden", {"contestant", "organiser"}),
        ("archived", "public", {"organiser"}),
        ("draft", "public", {"organiser"}),
    ],
)
def test_a_contest_is_seen_by_exactly_the_readers_its_visibility_names(
    state: str, visibility: str, sees: set[str]
) -> None:
    the_contest = contest(state, visibility=visibility)
    assert {
        reader for reader, flags in READERS.items() if contest_visible_to(the_contest, **flags)
    } == sees


def test_a_contest_is_running_while_published_between_its_dates() -> None:
    assert is_running(contest(), START)
    assert is_running(contest(), DURING)
    assert not is_running(contest(), END)
    assert not is_running(contest(), START - timedelta(seconds=1))
    assert not is_running(contest("draft"), DURING)
    assert not is_running(contest("archived"), DURING)
