"""Numbers past Python's limit of 4300 digits on turning text into an int,
and back, are read, scored and written to the digit, and a value spelled
into text keeps every digit (TASK-FORMAT.md sections 1.3 and 1.6).
"""

from decimal import Decimal
from fractions import Fraction

from forge.domain import exact_json
from forge.domain.plans import spelled
from forge.domain.scoring import written
from forge.domain.yaml_models import load_yaml

NINES = "9" * 5000


def test_a_result_holding_a_whole_number_of_any_length_reads_and_writes_back() -> None:
    document = exact_json.loads(f'{{"value": {NINES}}}')

    assert document == {"value": 10**5000 - 1}
    assert exact_json.dumps(document) == f'{{"value": {NINES}}}'


def test_a_number_of_any_length_is_written_as_its_digits() -> None:
    assert written(Fraction(10**5000 + 1, 2 * 10**5000)) == "0.5" + "0" * 4999 + "5"
    assert written(Fraction(10**5000 + 1, 2)) == "5" + "0" * 4999 + "0.5"


def test_a_whole_number_of_any_length_in_a_file_is_read() -> None:
    assert load_yaml(f"limit: {NINES}\n") == {"limit": 10**5000 - 1}


def test_a_value_spelled_into_text_keeps_every_digit() -> None:
    assert spelled(Decimal("12345678901234567890123456789.5")) == (
        "12345678901234567890123456789.5"
    )
    assert spelled(Decimal("1E+5000")) == "1" + "0" * 5000
    assert spelled(10**5000) == "1" + "0" * 5000
    assert spelled(Decimal("5.00")) == "5"
    assert spelled(Decimal("-0.0")) == "0"
