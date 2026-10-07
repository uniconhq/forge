"""A new task's entry is written into the text of `contest.yaml`: `{id:
<task>}`, and in a published contest with `release_at` and `closes` at the
contest's end, so work in progress shows nothing. An empty list becomes a
list of one, a block list gets one more item at its own indent with the
comments and the keys after it kept, a file without the key gets one, and a
list in flow style is refused. A name YAML would read as something else is
quoted, and a task the list holds already leaves the file as it is.
"""

from datetime import UTC, datetime

import pytest

from forge.domain.contest_entries import Unlisted, with_entry
from forge.domain.definitions import parse_contest
from forge.domain.yaml_models import InvalidDefinition

HEAD = (
    "name: Spring 2026\n"
    "start: 2026-10-01T10:00:00Z\n"
    "end: 2026-10-01T15:00:00Z\n"
    "state: draft\n"
    "visibility: signed-in\n"
)
END = datetime(2026, 10, 1, 15, tzinfo=UTC)


def ids(content: bytes) -> list[str]:
    return [entry.id for entry in parse_contest(content).tasks]


def test_an_empty_list_becomes_a_list_of_one_and_keeps_its_comment() -> None:
    content = (HEAD + "tasks: []  # none yet\n\nleaderboards: []\n").encode()

    expected = HEAD + "tasks:  # none yet\n  - id: sum\n"

    assert with_entry(content, "sum") == (expected + "\nleaderboards: []\n").encode()


def test_a_block_list_gets_one_more_item_at_its_own_indent() -> None:
    content = (
        HEAD + "tasks:\n"
        "    # Easiest first.\n"
        "    - id: product\n"
        "      worth: 50\n"
        "\n"
        "# The boards.\n"
        "leaderboards: []\n"
    ).encode()

    updated = with_entry(content, "sum")

    assert updated is not None
    text = updated.decode()
    assert "      worth: 50\n    - id: sum\n\n# The boards.\n" in text
    assert "    # Easiest first.\n" in text
    contest = parse_contest(updated)
    assert ids(updated) == ["product", "sum"]
    assert [contest.label_of(task) for task in ("product", "sum")] == ["A", "B"]
    assert contest.tasks[1].worth is None


def test_items_at_no_indent_get_one_at_no_indent() -> None:
    content = (HEAD + "tasks:\n- id: product\n  worth: 50\n").encode()

    updated = with_entry(content, "sum")

    assert updated is not None
    assert updated.decode().endswith("- id: product\n  worth: 50\n- id: sum\n")


def test_a_file_without_the_key_gets_one_at_the_end() -> None:
    updated = with_entry(HEAD.encode(), "sum")

    assert updated == (HEAD + "tasks:\n  - id: sum\n").encode()


def test_in_a_published_contest_a_new_task_opens_and_closes_at_the_end() -> None:
    content = HEAD.replace("state: draft", "state: published").encode()

    updated = with_entry(content, "sum")

    assert updated is not None
    assert updated.decode().endswith(
        "tasks:\n  - id: sum\n"
        "    release_at: 2026-10-01T15:00:00Z\n"
        "    closes: 2026-10-01T15:00:00Z\n"
    )
    entry = parse_contest(updated).tasks[0]
    assert (entry.release_at, entry.closes) == (END, END)


def test_windows_line_endings_stay() -> None:
    content = (HEAD + "tasks: []\n").replace("\n", "\r\n").encode()

    updated = with_entry(content, "sum")

    assert updated is not None
    assert b"\n" not in updated.replace(b"\r\n", b"")
    assert ids(updated) == ["sum"]


def test_a_name_yaml_reads_as_something_else_is_quoted() -> None:
    updated = with_entry((HEAD + "tasks: []\n").encode(), "123")

    assert updated is not None
    assert b'- id: "123"' in updated
    assert ids(updated) == ["123"]


def test_a_task_the_list_holds_already_leaves_the_file_alone() -> None:
    content = (HEAD + "tasks:\n  - id: sum\n    worth: 5\n").encode()

    assert with_entry(content, "sum") is None


def test_a_flow_list_with_items_is_not_edited() -> None:
    content = (HEAD + "tasks: [{id: product, worth: 50}]\n").encode()

    with pytest.raises(Unlisted):
        with_entry(content, "sum")


def test_a_file_that_is_not_a_contest_yaml_is_refused() -> None:
    with pytest.raises(InvalidDefinition):
        with_entry(b"name: [\n", "sum")
    with pytest.raises(InvalidDefinition):
        with_entry((HEAD + "submissions_closed: false\n").encode(), "sum")
