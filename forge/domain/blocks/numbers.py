"""Every number the blocks read and give, exact (SIMPLER-CORE.md 14).

Every number is the exact rational of the decimal it was written as: a
result's numbers arrive as `Decimal` (`forge.domain.exact_json`) and a
YAML number as the shortest decimal that reads back as it (`repr`), so
`0.8` is 4/5 whichever file it came from. Only `decimal_of` rounds, and
only a rational that is no finite decimal, such as 1/3.

A decimal written with an exponent past `MOST_EXPONENT` is no number:
`1e2000000` is nine characters an entrant can report, and its rational a
whole number of two million digits, which every read of a leaderboard would
build again. A long number written out digit by digit costs only as much
as its text, so it is read.
"""

import math
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction

SIGNIFICANT = 30
"""The significant digits a rational that is no finite decimal is given in,
far past any two numbers a board tells apart."""
MOST_EXPONENT = 1000
"""The largest exponent, up or down, of a decimal read as a number."""


class Better(StrEnum):
    """Which way a number is better, as the program that makes it says."""

    HIGHER = "higher"
    LOWER = "lower"


def exact(value: object) -> Fraction | None:
    """The exact rational of a number as written, or none for anything that
    is not a finite number (text, `true`, a missing value) or is a decimal
    with an exponent past `MOST_EXPONENT`.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Decimal):
        if not value.is_finite() or abs(int(value.as_tuple().exponent)) > MOST_EXPONENT:
            return None
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(repr(value)) if math.isfinite(value) else None
    if isinstance(value, Fraction):
        return value
    return None


def decimal_of(number: Fraction) -> Decimal:
    """`number` as a decimal: exactly when it is one, and to `SIGNIFICANT`
    digits when it is not.
    """
    rest = number.denominator
    twos = fives = 0
    while rest % 2 == 0:
        rest //= 2
        twos += 1
    while rest % 5 == 0:
        rest //= 5
        fives += 1
    if rest == 1:
        scale = max(twos, fives)
        digits = number.numerator * 10**scale // number.denominator
        # Built from the int's own digits, never its text, which Python
        # refuses past 4300 digits.
        whole = Decimal(digits).as_tuple()
        return Decimal((whole.sign, whole.digits, -scale))
    with localcontext() as context:
        context.prec = SIGNIFICANT
        return Decimal(number.numerator) / Decimal(number.denominator)


def written(number: Fraction) -> str:
    """`number` as the plain decimal digits it is served and noted as."""
    return format(decimal_of(number), "f")


__all__ = ["MOST_EXPONENT", "SIGNIFICANT", "Better", "decimal_of", "exact", "written"]
