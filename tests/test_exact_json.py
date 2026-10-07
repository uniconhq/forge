"""JSON whose numbers stay exactly as written: a decimal reads as the decimal
it is, writes back digit for digit, and a number JSON cannot spell is
refused either way.
"""

import json
import math
from decimal import Decimal
from typing import Any

import pytest

from forge.domain import exact_json


def test_a_decimal_reads_as_the_decimal_it_is_written_as() -> None:
    document = exact_json.loads('{"a": 0.1, "b": 1.0000000000000000001, "c": 3, "d": 1e-3}')

    assert document == {
        "a": Decimal("0.1"),
        "b": Decimal("1.0000000000000000001"),
        "c": 3,
        "d": Decimal("0.001"),
    }
    assert isinstance(document["c"], int)
    assert document["b"] > 1
    assert document["a"] * 3 == Decimal("0.3")


@pytest.mark.parametrize(
    "text",
    [
        '{"x": 0.1}',
        '{"x": 1.0000000000000000001}',
        '{"x": 1.50}',
        '{"x": -0.0}',
        '[1, 2.25, "a", true, null, {"y": []}]',
        '{"x": 12345678901234567890.123456789}',
    ],
)
def test_a_document_writes_back_digit_for_digit(text: str) -> None:
    written = exact_json.dumps(exact_json.loads(text))

    assert json.loads(written, parse_float=Decimal) == json.loads(text, parse_float=Decimal)
    assert written.replace(" ", "") == text.replace(" ", "")


@pytest.mark.parametrize("text", ["NaN", '{"x": Infinity}', "[-Infinity]"])
def test_a_number_json_cannot_spell_is_refused_when_read(text: str) -> None:
    with pytest.raises(ValueError):
        exact_json.loads(text)


@pytest.mark.parametrize(
    "value", [math.nan, math.inf, Decimal("NaN"), Decimal("-Infinity"), {"x": [float("nan")]}]
)
def test_a_number_that_is_not_finite_is_refused_when_written(value: Any) -> None:
    with pytest.raises(ValueError):
        exact_json.dumps(value)


@pytest.mark.parametrize("value", [{1: "a"}, {"x": object()}, b"bytes"])
def test_what_is_not_a_json_value_is_refused_when_written(value: Any) -> None:
    with pytest.raises(ValueError):
        exact_json.dumps(value)


def test_text_is_written_as_json_text() -> None:
    assert exact_json.dumps({"name": 'Ada "the first"', "é": 0.5}) == (
        '{"name": "Ada \\"the first\\"", "é": 0.5}'
    )
    assert exact_json.dumps((1, 2)) == "[1, 2]"
    assert exact_json.dumps(0.1) == "0.1"
    assert exact_json.loads(b"[0.5]") == [Decimal("0.5")]
    with pytest.raises(ValueError):
        exact_json.loads("{not json")
