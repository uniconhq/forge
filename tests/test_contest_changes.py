"""A running contest asks only about changes that can move results (#459)."""

import pytest

from forge.domain.contest_changes import contest_changes
from forge.domain.definitions import parse_contest

BASE = """\
name: Spring
start: 2026-09-26T10:00:00Z
end: 2026-09-26T15:00:00Z
state: published
visibility: everyone
tasks:
  - id: sum
"""
BOARD = """\
leaderboards:
  standings:
    title: Standings
    columns: {total: 1}
    sort: [{total: desc}]
    display: [total]
"""


@pytest.mark.parametrize(
    ("entry", "words"),
    [
        ("due: 2026-09-26T14:00:00Z", "due moves"),
        ("release_at: 2026-09-26T11:00:00Z", "release moves"),
        ("closes: 2026-09-26T14:00:00Z", "close moves"),
        ("withdrawn: true", "withdrawn"),
        ("marks: 2", "marked submissions"),
    ],
)
def test_each_task_result_key_is_named(entry: str, words: str) -> None:
    changes = contest_changes(parse_contest(BASE), parse_contest(BASE + f"    {entry}\n"))
    assert len(changes) == 1 and words in changes[0]


@pytest.mark.parametrize(
    ("old", "new", "words"),
    [
        ("columns: {total: 1}", "columns: {total: 2}", "columns change"),
        ("sort: [{total: desc}]", "sort: [{total: asc}]", "ranking changes"),
        ("display: [total]", "display: []", "displayed columns change"),
        ("title: Standings", "title: Standings\n    audience: {who: everyone}", "audience changes"),
    ],
)
def test_every_leaderboard_key_is_named(old: str, new: str, words: str) -> None:
    changes = contest_changes(
        parse_contest(BASE + BOARD), parse_contest(BASE + BOARD.replace(old, new))
    )
    assert len(changes) == 1 and words in changes[0]


def test_adding_and_removing_leaderboards_and_tasks_are_named() -> None:
    plain, board = parse_contest(BASE), parse_contest(BASE + BOARD)
    assert contest_changes(plain, board) == ("The Standings leaderboard is added.",)
    assert contest_changes(board, plain) == ("The Standings leaderboard is removed.",)
    extra = parse_contest(BASE + "  - id: product\n")
    assert contest_changes(plain, extra) == ("The task product is added to the contest.",)
    assert contest_changes(extra, plain) == ("The task product is removed from the contest.",)


def test_changing_a_hold_names_both_sides() -> None:
    plain = parse_contest(BASE)
    held = parse_contest(BASE + "hold: {from: 2026-09-26T14:00:00Z, until: staff}\n")
    assert "no hold to a hold" in contest_changes(plain, held)[0]
    assert "to no hold" in contest_changes(held, plain)[0]


def test_text_default_times_and_reordered_keys_do_not_ask() -> None:
    before = parse_contest(BASE + BOARD)
    text = BASE.replace("name: Spring", "name: Autumn\ndescription: A contest.")
    assert contest_changes(before, parse_contest(text + BOARD)) == ()
    renamed = BOARD.replace("title: Standings", "title: Results")
    assert contest_changes(before, parse_contest(BASE + renamed)) == ()
    explicit = BASE + "    release_at: 2026-09-26T10:00:00Z\n    closes: 2026-09-26T15:00:00Z\n"
    assert contest_changes(parse_contest(BASE), parse_contest(explicit)) == ()
    two = BOARD.replace("columns: {total: 1}", "columns: {total: 1, other: 2}")
    reordered = two.replace("{total: 1, other: 2}", "{other: 2, total: 1}")
    assert contest_changes(parse_contest(BASE + two), parse_contest(BASE + reordered)) == ()
