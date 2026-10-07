"""Released, visible and open, late and revealed, and who sees a contest at
all, worked out from `contest.yaml`, a row's extension and the clock. A
task's timeline is its entry in the contest's `tasks`.
"""

from datetime import UTC, datetime, timedelta

import pytest

from forge.domain.definitions import ContestDefinition, parse_contest
from forge.domain.release import (
    NONE,
    Closed,
    Extension,
    Openness,
    close_of,
    contest_visible_to,
    due_of,
    has_started,
    is_running,
    late_days,
    openness,
    released,
    reveal_of,
    visible,
)

START = datetime(2026, 6, 1, 9, tzinfo=UTC)
END = START + timedelta(hours=5)
DURING = START + timedelta(hours=1)
AFTER = END + timedelta(minutes=1)
LATER = START + timedelta(hours=2)
SECOND = timedelta(seconds=1)


def contest(
    state: str = "published", *, visibility: str = "everyone", tasks: str = "[{id: a}]"
) -> ContestDefinition:
    return parse_contest(
        f"name: C\nstart: {START.isoformat()}\nend: {END.isoformat()}\nstate: {state}\n"
        f"visibility: {visibility}\ntasks: {tasks}\n"
    )


TIMELINE = contest(
    tasks=(
        "[{id: a}, "
        f"{{id: b, release_at: {LATER.isoformat()}, due: {(END - timedelta(hours=1)).isoformat()}, "
        f"closes: {(END - timedelta(minutes=30)).isoformat()}}}]"
    )
)


def test_a_listed_task_is_released_at_its_entrys_release_at() -> None:
    assert released(TIMELINE, "a", START)
    assert not released(TIMELINE, "b", LATER - SECOND)
    assert released(TIMELINE, "b", LATER)
    assert openness(TIMELINE, "b", LATER - SECOND) == Openness(False, Closed.NOT_RELEASED)


def test_a_task_the_contest_does_not_list_is_never_released() -> None:
    assert not released(TIMELINE, "c", DURING)
    assert not visible(TIMELINE, "c", DURING)
    assert openness(TIMELINE, "c", DURING) == Openness(False, Closed.NOT_RELEASED)


@pytest.mark.parametrize(
    ("the_contest", "now"),
    [(contest("draft"), DURING), (contest(), START - SECOND)],
    ids=["contest-draft", "before-start"],
)
def test_a_task_is_not_released_before_its_contest_is_published_and_started(
    the_contest: ContestDefinition, now: datetime
) -> None:
    assert not released(the_contest, "a", now)
    assert not visible(the_contest, "a", now)
    assert openness(the_contest, "a", now) == Openness(False, Closed.NOT_RELEASED)


def test_a_released_task_stays_visible_after_the_end_until_archived() -> None:
    assert visible(contest(), "a", AFTER)
    assert released(contest("archived"), "a", AFTER)
    assert not visible(contest("archived"), "a", AFTER)
    assert openness(contest("archived"), "a", DURING) == Openness(False, Closed.ARCHIVED)


def test_a_task_is_open_until_its_own_close() -> None:
    assert openness(TIMELINE, "a", END - SECOND) == Openness(True, None)
    assert openness(TIMELINE, "a", END) == Openness(False, Closed.CLOSED)
    closes = END - timedelta(minutes=30)
    assert openness(TIMELINE, "b", closes - SECOND).open
    assert openness(TIMELINE, "b", closes) == Openness(False, Closed.CLOSED)


def test_an_extension_on_some_tasks_moves_the_rows_close_on_those_only() -> None:
    extension = Extension(timedelta(hours=1), frozenset({"b"}))
    closes = END - timedelta(minutes=30)

    assert openness(TIMELINE, "b", closes + timedelta(minutes=59), extension).open
    assert openness(TIMELINE, "b", closes + timedelta(hours=1), extension).reason is Closed.CLOSED
    assert openness(TIMELINE, "a", END, extension) == Openness(False, Closed.CLOSED)
    assert openness(TIMELINE, "a", END) == Openness(False, Closed.CLOSED)
    assert extension.on("a") == timedelta(0)
    assert extension.on("b") == timedelta(hours=1)


def test_an_extension_on_every_task_moves_every_close() -> None:
    everything = Extension(timedelta(minutes=30))

    assert openness(TIMELINE, "a", AFTER, everything).open
    assert close_of(TIMELINE, TIMELINE.tasks[0], everything) == END + timedelta(minutes=30)
    assert close_of(TIMELINE, TIMELINE.tasks[0], NONE) == END


def test_the_first_reason_that_applies_is_given() -> None:
    assert openness(contest("draft"), "a", AFTER).reason is Closed.NOT_RELEASED
    assert openness(contest("archived"), "a", AFTER).reason is Closed.ARCHIVED
    assert openness(contest(), "a", AFTER).reason is Closed.CLOSED


def test_a_rows_due_is_the_entrys_due_with_its_extension() -> None:
    a, b = TIMELINE.tasks
    due = END - timedelta(hours=1)

    assert due_of(TIMELINE, a, Extension(timedelta(hours=1))) is None
    assert due_of(TIMELINE, b, NONE) == due
    assert due_of(TIMELINE, b, Extension(timedelta(hours=1), frozenset({"b"}))) == (
        due + timedelta(hours=1)
    )
    assert due_of(TIMELINE, b, Extension(timedelta(hours=1), frozenset({"a"}))) == due


@pytest.mark.parametrize(
    ("after", "days"),
    [
        (timedelta(0), 0),
        (-timedelta(hours=3), 0),
        (SECOND, 1),
        (timedelta(days=1), 1),
        (timedelta(days=1) + SECOND, 2),
        (timedelta(days=2, seconds=1), 3),
    ],
    ids=["at-the-due", "early", "a-second-late", "a-day-late", "a-day-and-a-second", "over-two"],
)
def test_a_submission_is_late_by_its_started_days(after: timedelta, days: int) -> None:
    due = END - timedelta(hours=1)

    assert late_days(due, due + after) == days


def test_a_task_without_a_due_is_never_late() -> None:
    assert late_days(None, AFTER) == 0


def test_a_task_reveals_when_it_has_closed_for_every_row() -> None:
    a, b = TIMELINE.tasks
    extensions = [
        Extension(timedelta(hours=1)),
        Extension(timedelta(hours=3), frozenset({"b"})),
        NONE,
    ]

    assert reveal_of(TIMELINE, a, extensions) == END + timedelta(hours=1)
    assert reveal_of(TIMELINE, b, extensions) == END - timedelta(minutes=30) + timedelta(hours=3)
    assert reveal_of(TIMELINE, a, []) == END


READERS = {
    "visitor": {"has_session": False, "is_contestant": False, "is_organiser": False},
    "signed-in": {"has_session": True, "is_contestant": False, "is_organiser": False},
    "contestant": {"has_session": True, "is_contestant": True, "is_organiser": False},
    "organiser": {"has_session": True, "is_contestant": False, "is_organiser": True},
}


@pytest.mark.parametrize(
    ("state", "visibility", "sees"),
    [
        ("published", "everyone", {"visitor", "signed-in", "contestant", "organiser"}),
        ("published", "signed-in", {"signed-in", "contestant", "organiser"}),
        ("published", "hidden", {"contestant", "organiser"}),
        ("archived", "everyone", {"organiser"}),
        ("draft", "everyone", {"organiser"}),
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
    assert not is_running(contest(), START - SECOND)
    assert not is_running(contest("draft"), DURING)
    assert not is_running(contest("archived"), DURING)


def test_a_contest_has_started_once_published_and_past_its_start() -> None:
    assert has_started(contest(), START)
    assert has_started(contest(), AFTER)
    assert not has_started(contest(), START - SECOND)
    assert not has_started(contest("draft"), DURING)
    assert not has_started(contest("archived"), DURING)
