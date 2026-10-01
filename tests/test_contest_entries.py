"""A new task's entry is written into the text of `contest.yaml`: an empty
list becomes a list of one, a block list gets one more item at its own
indent with the comments and the keys after it kept, a file without the key
gets one, and a list in flow style is refused. The label is the first free
letter, a name YAML would read as something else is quoted, and a task the
list holds already leaves the file as it is.
"""

import pytest

from forge.domain.contest_entries import Unlisted, label_for, with_entry
from forge.domain.definitions import parse_contest
from forge.domain.yaml_models import InvalidDefinition

HEAD = (
    "name: Spring 2026\n"
    "start: 2026-10-01T10:00:00Z\n"
    "end: 2026-10-01T15:00:00Z\n"
    "state: draft\n"
    "visibility: signed-in\n"
)


def _entries(content: bytes) -> list[tuple[str, str, int]]:
    return [(entry.id, entry.label, entry.points) for entry in parse_contest(content).tasks]


def test_an_empty_list_becomes_a_list_of_one_and_keeps_its_comment() -> None:
    content = (HEAD + "tasks: []  # none yet\n\nleaderboards: []\n").encode()

    expected = HEAD + "tasks:  # none yet\n  - id: sum\n    label: A\n    points: 100\n"

    assert with_entry(content, "sum") == (expected + "\nleaderboards: []\n").encode()


def test_a_block_list_gets_one_more_item_at_its_own_indent() -> None:
    content = (
        HEAD + "tasks:\n"
        "    # Easiest first.\n"
        "    - id: product\n"
        "      label: A\n"
        "      points: 50\n"
        "\n"
        "# The boards.\n"
        "leaderboards: []\n"
    ).encode()

    updated = with_entry(content, "sum")

    assert updated is not None
    text = updated.decode()
    assert "    - id: sum\n      label: B\n      points: 100\n\n# The boards.\n" in text
    assert "    # Easiest first.\n" in text
    assert _entries(updated) == [("product", "A", 50), ("sum", "B", 100)]


def test_items_at_no_indent_get_one_at_no_indent() -> None:
    content = (HEAD + "tasks:\n- id: product\n  label: A\n  points: 50\n").encode()

    updated = with_entry(content, "sum")

    assert updated is not None
    assert updated.decode().endswith("- id: sum\n  label: B\n  points: 100\n")


def test_a_file_without_the_key_gets_one_at_the_end() -> None:
    updated = with_entry(HEAD.encode(), "sum")

    assert updated == (HEAD + "tasks:\n  - id: sum\n    label: A\n    points: 100\n").encode()


def test_windows_line_endings_stay() -> None:
    content = (HEAD + "tasks: []\n").replace("\n", "\r\n").encode()

    updated = with_entry(content, "sum")

    assert updated is not None
    assert b"\n" not in updated.replace(b"\r\n", b"")
    assert _entries(updated) == [("sum", "A", 100)]


def test_a_name_yaml_reads_as_something_else_is_quoted() -> None:
    updated = with_entry((HEAD + "tasks: []\n").encode(), "123")

    assert updated is not None
    assert b'- id: "123"' in updated


def test_a_task_the_list_holds_already_leaves_the_file_alone() -> None:
    content = (HEAD + "tasks:\n  - id: sum\n    label: Z\n    points: 5\n").encode()

    assert with_entry(content, "sum") is None


def test_a_flow_list_with_items_is_not_edited() -> None:
    content = (HEAD + "tasks: [{id: product, label: A, points: 50}]\n").encode()

    with pytest.raises(Unlisted):
        with_entry(content, "sum")


def test_a_file_that_is_not_a_contest_yaml_is_refused() -> None:
    with pytest.raises(InvalidDefinition):
        with_entry(b"name: [\n", "sum")


def test_the_label_is_the_first_free_letter() -> None:
    assert label_for(set()) == "A"
    assert label_for({"A", "C"}) == "B"
    assert label_for({chr(code) for code in range(ord("A"), ord("Z") + 1)}) == "AA"
