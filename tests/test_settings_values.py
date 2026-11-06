"""A settings file's data as a form's values and back: named lists for the
mappings a form edits as lists, JSON numbers, and weights keys given back the
type of the repeated values they name.
"""

from decimal import Decimal
from typing import Any

import pytest

from forge.domain.settings_values import NotValues, settings_of, values_of, written_values
from forge.domain.yaml_models import InvalidDefinition, load_yaml


def test_a_tasks_groups_and_values_are_named_lists_and_numbers_are_json() -> None:
    data = {
        "time_limit": Decimal("1.5"),
        "scoring": {"groups": {"small": {"n": [1]}}, "values": {"score": {"block": "sum"}}},
    }

    values = values_of("task", data)

    assert values == {
        "time_limit": 1.5,
        "scoring": {
            "groups": [{"name": "small", "picks": {"n": [1]}}],
            "values": [{"name": "score", "formula": {"block": "sum"}}],
        },
    }
    assert settings_of("task", values)["scoring"] == data["scoring"]


def test_a_board_holding_a_key_called_key_stays_a_mapping() -> None:
    data = {"leaderboards": {"main": {"key": "x"}}}

    assert values_of("contest", data) == data


def test_a_number_json_cannot_carry_is_not_values() -> None:
    with pytest.raises(NotValues):
        values_of("task", {"limit": Decimal("Infinity")})


def test_weights_keys_take_the_type_of_the_repeated_values() -> None:
    values: dict[str, Any] = {
        "repeat": [{"inputs": ["seed"], "tuples": [1, 2]}],
        "scoring": {
            "values": [
                {
                    "name": "score",
                    "formula": {"block": "sum", "weights": {"seed": {"1": 2, "x": 3}}},
                }
            ]
        },
    }

    settings = settings_of("task", values)

    assert settings["scoring"]["values"]["score"]["weights"] == {"seed": {1: 2, "x": 3}}
    assert values["scoring"]["values"][0]["formula"]["weights"] == {"seed": {"1": 2, "x": 3}}


def test_values_with_two_entries_of_one_name_are_refused_at_that_name() -> None:
    with pytest.raises(InvalidDefinition) as refused:
        settings_of(
            "task",
            {"scoring": {"values": [{"name": "a", "formula": 1}, {"name": "a", "formula": 2}]}},
        )

    assert [problem["path"] for problem in refused.value.errors] == [("scoring", "values", "a")]


def test_values_written_into_a_file_read_back_as_the_values() -> None:
    content = b"name: A  # the name\nscoring:\n  values:\n    score: 1  # one\n"
    values = values_of("task", load_yaml(content))
    values["scoring"]["values"].append({"name": "rest", "formula": 2})

    written = written_values("task", content, values)

    assert written == content + b"    rest: 2\n"


def test_values_that_cannot_be_written_are_refused_at_the_file() -> None:
    with pytest.raises(InvalidDefinition) as refused:
        written_values("task", b"- a\n", {"name": "A"})

    assert [problem["path"] for problem in refused.value.errors] == [()]


def test_two_entries_with_no_name_or_an_entry_with_more_are_refused() -> None:
    with pytest.raises(InvalidDefinition) as refused:
        settings_of(
            "task",
            {
                "scoring": {
                    "groups": [{"name": "", "picks": {}}, {"name": "", "picks": {}}],
                    "values": [{"name": "a", "formula": 1, "extra": 5}],
                }
            },
        )

    assert [(p["path"], p["message"]) for p in refused.value.errors] == [
        (("scoring", "groups", ""), "Give each one a name."),
        (("scoring", "values", "a"), "extra is not part of this entry."),
    ]
