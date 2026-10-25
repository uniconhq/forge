"""Every definition file is read as YAML 1.2's core schema, whatever `%YAML`
line it starts with: only `true` and `false` are booleans, so `no`, `NO` and
`on` are text, `1_000`, `1:30` and a date are text, a leading zero is no
octal, every number that is not whole is the exact `Decimal` it is
written as, and an anchor or an alias is refused. `one_line` writes a
value so that the same reader reads it back.
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


@pytest.mark.parametrize(
    "written",
    [
        "!!timestamp 2026-01-01",
        "!!binary aGk=",
        "!!set {a: null}",
        "!!omap [{a: 1}]",
        "!!pairs [{a: 1}]",
    ],
)
def test_a_tag_of_yaml_1_1_alone_is_refused(written: str) -> None:
    with pytest.raises(InvalidDefinition) as raised:
        load_mapping("task.yaml", f"value: {written}\n")

    assert "is not one Unicon reads" in raised.value.errors[0]["message"]


def test_a_key_given_twice_is_refused_at_its_line() -> None:
    with pytest.raises(InvalidDefinition) as raised:
        load_mapping("task.yaml", "name: a\ntest_groups: {}\nname: b\n")

    assert raised.value.errors[0]["message"] == (
        "Unicon could not read the task's settings, because of a mistake on line 3: the setting "
        "'name' is given twice."
    )


ALIASED_TASK = """\
name: Review
workflow: unicon/run@v1
scoring:
  values:
    review: &mark {block: entered, at_least: 0, at_most: 10}
    bonus: *mark
"""
ALIASED_CONTEST = """\
name: Spring
leaderboards:
  one: {columns: {total: &x 1}}
  two: {columns: {total: *x}}
"""


def test_an_anchor_or_an_alias_is_refused_at_its_line() -> None:
    # An alias would make two values one object, so the mark entered for
    # one would be read for the other.
    with pytest.raises(InvalidDefinition) as task:
        parse_task(ALIASED_TASK)
    with pytest.raises(InvalidDefinition) as contest:
        parse_contest(ALIASED_CONTEST)

    assert task.value.errors[0]["message"] == (
        "Unicon could not read the task's settings, because of a mistake on line 5: a part is "
        "named with & or repeated with *, which Unicon does not read; write each part out in "
        "full."
    )
    assert (
        "a mistake on line 3: a part is named with & or repeated with *"
        in (contest.value.errors[0]["message"])
    )
    assert load_yaml("a: 'x & y * z'\nb: x&y\n") == {"a": "x & y * z", "b": "x&y"}
    # What forge writes gives a part given twice out in full each time.
    shared = {"block": "entered", "at_least": 0, "at_most": 10}
    written = one_line({"review": shared, "bonus": shared})
    assert load_yaml(f"values: {written}\n") == {"values": {"review": shared, "bonus": shared}}


def test_groups_named_no_and_on_are_groups_of_those_names() -> None:
    task = parse_task(
        b"name: Sum\nworkflow: unicon/classic@v3\n"
        b"scoring:\n  groups:\n    no: {input: [a]}\n    NO: {input: [b]}\n    on: {}\n"
        b"  values:\n    score: {block: count, over: {runs: all}}\n"
    )

    assert list(task.scoring.groups) == ["no", "NO", "on"]
    assert task.scoring.groups["NO"] == {"input": ["b"]}


@pytest.mark.parametrize(
    "value",
    ["no", "NO", "on", "yes", "1_000", "1e3", "0o17", "-.5", "2026-06-01", "", "a: b", "#x"],
)
def test_text_is_written_so_it_reads_back_as_text(value: str) -> None:
    assert load_yaml(f"value: {one_line(value)}\n") == {"value": value}


@pytest.mark.parametrize(("value", "written"), [(Decimal("3"), "3"), (Decimal("-12"), "-12")])
def test_a_whole_decimal_is_written_bare(value: Decimal, written: str) -> None:
    assert one_line(value) == written
    assert one_line({"a": value}) == f"{{a: {written}}}"


def test_a_decimal_is_written_as_its_digits() -> None:
    assert one_line(Decimal(THIRTY_DIGITS)) == THIRTY_DIGITS
    assert load_yaml(one_line({"bound": Decimal(THIRTY_DIGITS), "id": "no"})) == {
        "bound": Decimal(THIRTY_DIGITS),
        "id": "no",
    }


def contest_starting(start: str) -> bytes:
    return (
        f"name: Spring\nstart: {start}\nend: 2027-01-01T00:00:00Z\nstate: draft\n"
        "visibility: signed-in\n"
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
        ("2026-06-01T09:00Z", datetime(2026, 6, 1, 9, tzinfo=UTC)),
        ("2026-06-01T09:00+08:00", datetime(2026, 6, 1, 1, tzinfo=UTC)),
        ("20260601T090000Z", datetime(2026, 6, 1, 9, tzinfo=UTC)),
        ('"2026-06-01T09:00:00.123456+00:00"', datetime(2026, 6, 1, 9, 0, 0, 123456, tzinfo=UTC)),
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
        ("2026-06-01", "Give a time and a time zone as well as the date"),
        ("2026-06-01T09:00:00", "Give the time zone with the time"),
        ("2026-02-30T09:00:00Z", "Give a date and a time"),
        ("tomorrow", "Give a date and a time"),
    ],
)
def test_a_time_that_is_not_one_says_what_it_lacks(written: str, message: str) -> None:
    with pytest.raises(InvalidDefinition) as raised:
        parse_contest(contest_starting(written))

    assert raised.value.errors[0]["message"].startswith(message)
