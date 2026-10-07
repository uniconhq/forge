"""JSON whose numbers stay exactly as written (TASK-FORMAT.md section 1.6). A
result's numbers are decimals a primitive wrote, and the platform reads each
as the exact rational of that decimal: `0.1` is 1/10, never the binary
double nearest it, and `1.0000000000000000001` is more than 1. `loads`
reads every number with a point or an exponent as a `Decimal`, and `dumps`
writes a `Decimal` back as the digits it holds, so a result passes through
the platform, its database included, without a digit changing.
"""

import json
import math
from decimal import Decimal
from typing import Any


def loads(text: str | bytes) -> Any:
    """The document in `text`, its decimals as `Decimal`. `ValueError` for
    one that is not JSON, or that spells a number JSON has not, such as
    `NaN` or `Infinity`.
    """

    def refuse(name: str) -> Any:
        raise ValueError(f"{name} is not a JSON number")

    return json.loads(text, parse_float=Decimal, parse_constant=refuse)


def dumps(value: Any) -> str:
    """`value` as JSON, a `Decimal` written as the digits it holds. A float
    or a `Decimal` that is not finite is refused with `ValueError`.
    """
    return "".join(_parts(value))


def _parts(value: Any) -> list[str]:
    match value:
        case None:
            return ["null"]
        case bool():
            return ["true" if value else "false"]
        case int():
            return [str(value)]
        case Decimal():
            if not value.is_finite():
                raise ValueError(f"{value} is not a finite number")
            return [str(value)]
        case float():
            if not math.isfinite(value):
                raise ValueError(f"{value} is not a finite number")
            return [repr(value)]
        case str():
            return [json.dumps(value, ensure_ascii=False)]
        case dict():
            parts = ["{"]
            for index, (key, item) in enumerate(value.items()):
                if not isinstance(key, str):
                    raise ValueError("a JSON object's keys are text")
                parts.extend([", " if index else "", json.dumps(key, ensure_ascii=False), ": "])
                parts.extend(_parts(item))
            parts.append("}")
            return parts
        case list() | tuple():
            parts = ["["]
            for index, item in enumerate(value):
                if index:
                    parts.append(", ")
                parts.extend(_parts(item))
            parts.append("]")
            return parts
    raise ValueError(f"{type(value).__name__} is not a JSON value")
