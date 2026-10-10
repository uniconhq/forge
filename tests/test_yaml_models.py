"""Every definition file is read as YAML 1.2's core schema, whatever `%YAML`
line it starts with: only `true` and `false` are booleans, so `no`, `NO` and
`on` are text, `1_000`, `1:30` and a date are text, a leading zero is no
octal, and every number that is not whole is the exact `Decimal` it is
written as. `one_line` writes a value so that the same reader reads it back.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from forge.domain.definitions import parse_contest, parse_task
from forge.domain.yaml_models import InvalidDefinition, load_mapping, load_yaml, one_line

THIRTY_DIGITS = "123456789012345.678901234567891"


@pytest.mark.parametrize(
    ("written", "read"),
    [
        ("no", "no"),
        ("NO", "NO"),
        ("on", "on"),
        ("off", "off"),
        ("yes", "yes"),
        ("y", "y"),
        ("1_000", "1_000"),
        ("1:30", "1:30"),
        ("2026-06-01", "2026-06-01"),
        ("2026-06-01T09:00:00Z", "2026-06-01T09:00:00Z"),
        ("<<", "<<"),
        ("017", 17),
        ("0o17", 15),
        ("0x1F", 31),
        ("-3", -3),
        ("true", True),
        ("False", False),
        ("~", None),
        ("", None),
        ("2.50", Decimal("2.50")),
        ("1e3", Decimal("1e3")),
        ("-.5", Decimal("-0.5")),
        (THIRTY_DIGITS, Decimal(THIRTY_DIGITS)),
    ],
)
def test_a_scalar_reads_as_yaml_1_2_says(written: str, read: object) -> None:
    found = load_yaml(f"value: {written}\n")

    assert found == {"value": read}
    assert isinstance(found, dict)
    assert type(found["value"]) is type(read)


def test_a_file_naming_yaml_1_1_is_still_read_as_1_2() -> None:
    assert load_yaml("%YAML 1.1\n---\ncountry: NO\nlate: no\n") == {
        "country": "NO",
        "late": "no",
    }


def test_a_key_that_1_1_would_read_as_a_boolean_is_text() -> None:
    assert load_yaml("no: 1\non: 2\nyes: 3\n") == {"no": 1, "on": 2, "yes": 3}


def test_a_key_given_twice_is_refused_at_its_line() -> None:
    with pytest.raises(InvalidDefinition) as raised:
        load_mapping("task.yaml", "name: a\ntest_groups: {}\nname: b\n")

    assert raised.value.errors[0]["message"] == (
        "The file does not parse as YAML at line 3: the key 'name' is given twice."
    )


def test_groups_named_no_and_on_are_groups_of_those_names() -> None:
    task = parse_task(
        b"name: Sum\nworkflow: unicon/classic@v2\n"
        b"test_groups:\n  no: {each: 1}\n  NO: {pass: 2.5}\n  on: {}\n"
    )

    assert list(task.test_groups) == ["no", "NO", "on"]
    assert task.test_groups["NO"].pass_ == Decimal("2.5")


@pytest.mark.parametrize(
    "value",
    ["no", "NO", "on", "yes", "1_000", "1e3", "0o17", "-.5", "2026-06-01", "", "a: b", "#x"],
)
def test_text_is_written_so_it_reads_back_as_text(value: str) -> None:
    assert load_yaml(f"value: {one_line(value)}\n") == {"value": value}


def test_a_decimal_is_written_as_its_digits() -> None:
    assert one_line(Decimal(THIRTY_DIGITS)) == THIRTY_DIGITS
    assert load_yaml(one_line({"bound": Decimal(THIRTY_DIGITS), "id": "no"})) == {
        "bound": Decimal(THIRTY_DIGITS),
        "id": "no",
    }


def contest_starting(start: str) -> bytes:
    return (
        f"name: Spring\nstart: {start}\nend: 2027-01-01T00:00:00Z\nstate: draft\n"
        "visibility: signed-in\nleaderboards: []\n"
    ).encode()


@pytest.mark.parametrize(
    ("written", "moment"),
    [
        ("2026-06-01T09:00:00Z", datetime(2026, 6, 1, 9, tzinfo=UTC)),
        ("2026-06-01 09:00:00Z", datetime(2026, 6, 1, 9, tzinfo=UTC)),
        ("2026-06-01t09:00:00z", datetime(2026, 6, 1, 9, tzinfo=UTC)),
        ("2026-6-1T9:00:00+08:00", datetime(2026, 6, 1, 1, tzinfo=UTC)),
        ("2026-06-01 09:00:00.5 -5", datetime(2026, 6, 1, 14, 0, 0, 500000, tzinfo=UTC)),
        ("2026-06-01T09:00:00+0800", datetime(2026, 6, 1, 1, tzinfo=UTC)),
    ],
)
def test_a_time_is_read_in_every_spelling_a_definition_file_has_used(
    written: str, moment: datetime
) -> None:
    contest = parse_contest(contest_starting(written))

    assert contest.start == moment


@pytest.mark.parametrize(
    ("written", "message"),
    [
        ("2026-06-01", "Must carry a time and a timezone as well as a date"),
        ("2026-06-01T09:00:00", "Must carry a timezone"),
        ("2026-02-30T09:00:00Z", "Must be a date and time"),
        ("tomorrow", "Must be a date and time"),
    ],
)
def test_a_time_that_is_not_one_says_what_it_lacks(written: str, message: str) -> None:
    with pytest.raises(InvalidDefinition) as raised:
        parse_contest(contest_starting(written))

    assert raised.value.errors[0]["message"].startswith(message)
