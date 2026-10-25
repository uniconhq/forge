"""What a page names each reported value by: its `title`, "Run time", and
its `unit`, "ms", on a number only, as the workflow's `outputs` give them.
Which way is better and its bounds come from the `primitive.yaml` or
`program.yaml` that gives the value, and `ReportMeaning` says which one
that was.
"""

from dataclasses import dataclass
from decimal import Decimal

from forge.domain.blocks.numbers import Better


@dataclass(frozen=True, slots=True)
class ReportMeaning:
    """One report of a run workflow (SIMPLER-CORE.md 7.3) as a page names it
    and what its number means: its `title` and `unit`, the unit on a number
    only, which the report gives, and which way is better and its bounds,
    which only the `primitive.yaml` or `program.yaml` that made the number
    says, and nothing after it restates. `made_by` names that file:
    `unicon/exec@v1`, or `diff/program.yaml`.
    """

    name: str
    made_by: str
    title: str | None = None
    unit: str | None = None
    better: Better | None = None
    at_least: int | Decimal | None = None
    at_most: int | Decimal | None = None


__all__ = ["ReportMeaning"]
