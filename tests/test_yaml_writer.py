"""A form's values written into a settings file: what they leave alone keeps
its lines and comments, what changed is written in place, new keys come
after the old, and values that cannot be written so are refused.
"""

from decimal import Decimal

import pytest

from forge.domain.yaml_models import load_yaml
from forge.domain.yaml_writer import Ordered, Unwritable, same, write_values

CONTEST = """\
# Spring contest settings.
name: Spring 2026  # shown on the home page
start: 2026-10-01T10:00:00Z
end: 2026-10-01T15:00:00Z   # three hours after
state: draft

# Teams of two or three.
teams:
  fewest: 2
  most: 3   # most first
  # fewest next
  locks: false

tasks:
  # Easiest first.
  - id: sum  # warm-up
    due: 2026-10-01T12:00:00Z
  - {id: product}
  - id: quotient
    marks: 5

leaderboards: {}  # none yet
"""


def read(text: str) -> object:
    return load_yaml(text)


def comments(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if "#" in line]


def test_values_the_file_holds_leave_it_exactly_as_it_is() -> None:
    values = read(CONTEST)
    assert isinstance(values, dict)

    assert write_values(CONTEST, values) == CONTEST


def test_saving_values_keeps_every_comment_of_the_file() -> None:
    values = read(CONTEST)
    assert isinstance(values, dict)
    values["name"] = "Autumn 2026"
    values["teams"]["most"] = 4
    values["tasks"][1]["due"] = "2026-10-01T13:00:00Z"
    values["capacity"] = 40

    written = write_values(CONTEST, values)

    assert same(read(written), values)
    assert "name: Autumn 2026  # shown on the home page\n" in written
    assert "  most: 4   # most first\n" in written
    assert written.endswith("leaderboards: {}  # none yet\ncapacity: 40\n")
    for comment in comments(CONTEST):
        assert comment.split("#", 1)[1] in written


def test_a_changed_value_changes_its_line_and_no_other() -> None:
    values = read(CONTEST)
    assert isinstance(values, dict)
    values["state"] = "published"

    written = write_values(CONTEST, values)

    assert written == CONTEST.replace("state: draft", "state: published")


def test_a_reordered_list_moves_each_item_with_its_comments() -> None:
    values = read(CONTEST)
    assert isinstance(values, dict)
    values["tasks"] = [values["tasks"][2], values["tasks"][0], values["tasks"][1]]

    written = write_values(CONTEST, values)

    assert same(read(written), values)
    assert "  - id: sum  # warm-up\n    due: 2026-10-01T12:00:00Z\n" in written
    assert written.index("quotient") < written.index("sum") < written.index("product")
    assert "# Easiest first." in written


def test_a_key_taken_out_drops_its_line_and_keeps_the_comments_after_it() -> None:
    values = read(CONTEST)
    assert isinstance(values, dict)
    del values["teams"]["most"]

    written = write_values(CONTEST, values)

    assert "most" not in written
    assert "most first" not in written
    assert "  fewest: 2\n  # fewest next\n  locks: false\n" in written


def test_a_task_taken_out_takes_only_its_own_lines() -> None:
    values = read(CONTEST)
    assert isinstance(values, dict)
    values["tasks"] = [values["tasks"][0], values["tasks"][2]]

    written = write_values(CONTEST, values)

    assert written == CONTEST.replace("  - {id: product}\n", "")


def test_the_first_entry_of_an_empty_mapping_or_list_is_a_block() -> None:
    text = "name: A\nleaderboards: {}\nchecks: []\n"

    written = write_values(
        text,
        {"name": "A", "leaderboards": {"main": {"title": "Main"}}, "checks": [{"id": "c"}]},
    )

    assert written == "name: A\nleaderboards:\n  main: {title: Main}\nchecks:\n- {id: c}\n"


def test_a_key_with_nothing_after_it_takes_a_mapping() -> None:
    written = write_values("name: A\nsamples:\n", {"name": "A", "samples": {"x": 1}})

    assert written == "name: A\nsamples: {x: 1}\n"


def test_a_new_item_is_indented_as_the_file_indents_lists() -> None:
    text = "name: A\ntasks:\n  - id: sum\n"

    written = write_values(text, {"name": "A", "tasks": [{"id": "sum"}, {"id": "product"}]})

    assert written == "name: A\ntasks:\n  - id: sum\n  - {id: product}\n"


@pytest.mark.parametrize(
    ("value", "line"),
    [
        ("true", "name: 'true'\n"),
        ("12", "name: '12'\n"),
        ("null", "name: 'null'\n"),
        ("no", "name: no\n"),
        ("2026-10-09", "name: 2026-10-09\n"),
        ("a\tb", 'name: "a\\tb"\n'),
    ],
)
def test_text_is_quoted_only_where_yaml_would_read_it_as_something_else(
    value: str, line: str
) -> None:
    written = write_values("name: A\n", {"name": value})

    assert written == line
    assert read(written) == {"name": value}


def test_a_number_keeps_its_digits_while_its_value_is_the_same() -> None:
    text = "weight: 1.000000000000000000000000000001  # exact\nscale: 1.50\n"

    written = write_values(text, {"weight": 1.0, "scale": 1.5, "extra": 0.1})

    assert written == text + "extra: 0.1\n"
    assert read(written)["extra"] == Decimal("0.1")  # type: ignore[index]


def test_an_ordered_mapping_renames_a_key_in_place_and_follows_the_values_order() -> None:
    text = "groups:\n  easy: {input: [a]}  # the small ones\n  hard: {input: [b]}\n"

    renamed = write_values(
        text, {"groups": Ordered({"small": {"input": ["a"]}, "hard": {"input": ["b"]}})}
    )
    reordered = write_values(
        text, {"groups": Ordered({"hard": {"input": ["b"]}, "easy": {"input": ["a"]}})}
    )

    assert renamed == ("groups:\n  small: {input: [a]}  # the small ones\n  hard: {input: [b]}\n")
    assert reordered == ("groups:\n  hard: {input: [b]}\n  easy: {input: [a]}  # the small ones\n")


def test_a_number_key_stays_a_number() -> None:
    text = "weights:\n  n: {1: 2, 3: 4}\n"

    written = write_values(text, {"weights": {"n": {1: 2, 3: 5}}})

    assert written == "weights:\n  n: {1: 2, 3: 5}\n"


def test_a_file_whose_lines_end_in_crlf_keeps_them() -> None:
    text = "# Settings.\r\nname: A  # the name\r\nstate: draft\r\n"

    written = write_values(text, {"name": "B", "state": "draft"})

    assert written == "# Settings.\r\nname: B  # the name\r\nstate: draft\r\n"


def test_an_empty_file_takes_the_values() -> None:
    assert write_values("", {"name": "A"}) == "name: A\n"


@pytest.mark.parametrize(
    "text",
    [
        "- a\n- b\n",
        "name: *missing\n",
        "a: &x 1\nb: *x\n",
        "name: A\nname: B\n",
        "name: [unclosed\n",
    ],
)
def test_a_file_that_is_not_named_settings_yaml_reads_is_refused(text: str) -> None:
    with pytest.raises(Unwritable):
        write_values(text, {"name": "A"})


BLOCK = "name: A\ndescription: |\n  one\n  two\n# --- schedule ---\nstart: x  # s\n"


def test_a_block_taken_out_keeps_the_comment_heading_the_next_key() -> None:
    written = write_values(BLOCK, {"name": "A", "start": "x"})

    assert written == "name: A\n# --- schedule ---\nstart: x  # s\n"


def test_a_block_replaced_keeps_its_style_or_the_comment_after_it() -> None:
    longer = write_values(BLOCK, {"name": "A", "description": "one\nthree\n", "start": "x"})
    plain = write_values(BLOCK, {"name": "A", "description": "short", "start": "x"})

    assert longer == BLOCK.replace("  two\n", "  three\n")
    assert plain == "name: A\ndescription: short\n# --- schedule ---\nstart: x  # s\n"


def test_an_item_taken_out_of_a_list_without_ids_leaves_the_others_their_comments() -> None:
    text = "checks:\n- {x: 1}  # first\n- {x: 2}  # second\n- {x: 3}  # third\n"

    written = write_values(text, {"checks": [{"x": 2}, {"x": 3}]})

    assert written == "checks:\n- {x: 2}  # second\n- {x: 3}  # third\n"


def test_a_key_renamed_while_another_goes_keeps_its_own_comment() -> None:
    text = "groups:\n  small: {n: [1]}  # small group\n  big: {n: [3]}  # big group\n"

    written = write_values(text, {"groups": Ordered({"huge": {"n": [3]}})})

    assert written == "groups:\n  huge: {n: [3]}  # big group\n"


def test_an_ordered_mapping_inside_a_list_follows_the_values_order() -> None:
    text = "boards:\n- columns:\n    a: 1  # first\n    b: 2\n"

    written = write_values(text, {"boards": [{"columns": Ordered({"b": 2, "a": 1})}]})

    assert written == "boards:\n- columns:\n    b: 2\n    a: 1  # first\n"


COMMENTED = """\
# A workflow, commented throughout.
inputs:   # what a task gives
  code: {type: folder, title: Solution}  # the entrant's
  # the limit, in seconds
  time_limit: {type: number, title: Time limit}

steps:
    # Run first.
    - id: run   # the program
      use: unicon/exec@v4
      with:
          source: ${{ inputs.code }}
          # passed on as it is
          time_limit: ${{ inputs.time_limit }}
    - id: check
      use: unicon/exec@v4   # the checker

outputs:
  score: ${{ steps.check.score }}  # out of one
# The end.
"""


def test_a_commented_file_saved_as_values_keeps_every_comment() -> None:
    values = read(COMMENTED)
    assert isinstance(values, dict)
    values["inputs"]["memory_limit"] = {"type": "number", "title": "Memory limit"}
    values["steps"][0]["with"]["time_limit"] = 3
    values["steps"].append({"id": "tidy", "use": "unicon/exec@v4"})
    values["outputs"]["log"] = "${{ steps.run.log }}"

    written = write_values(COMMENTED, values)

    assert same(read(written), values)
    assert sorted(notes(written)) == sorted(notes(COMMENTED))


def notes(text: str) -> list[str]:
    return [line[line.index("#") :] for line in text.splitlines() if "#" in line]


def test_a_part_added_at_the_end_goes_before_the_lines_after_it() -> None:
    text = (
        "steps:\n  - id: run\n  - id: check  # last\n\n# Results.\n"
        "outputs:\n  score: 1\n# The end.\n"
    )

    written = write_values(
        text,
        {
            "steps": [{"id": "run"}, {"id": "check"}, {"id": "tidy"}],
            "outputs": {"score": 1, "log": 2},
        },
    )

    assert written == (
        "steps:\n  - id: run\n  - id: check  # last\n  - {id: tidy}\n\n# Results.\n"
        "outputs:\n  score: 1\n  log: 2\n# The end.\n"
    )
