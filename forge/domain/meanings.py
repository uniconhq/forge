"""What a page names each reported value by (TASK-FORMAT.md section 1.3):
its `title`, "Run time", and its `unit`, "ms", on a number only, as the
workflow's `report` gives them, with how the value folds over tests and
which way is better, so a page can say "Run time (ms), the longest".

The words are frozen with a publication, in its note beside what each
per-test number means, in the order the workflow reports the values, so a
result, a board or a form never reads the workflow again. A publication
made before they were frozen lists its per-test numbers by name alone, and
a page names each by its key.

A workflow without a test block names a report the same way, but a
report there adds only a title and a unit: which way is better and its
bounds come from the `primitive.yaml` or `program.yaml` that gives the
value, and `ReportMeaning` says which one that was.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from forge.domain.blocks.numbers import Better, written
from forge.domain.workflow_definition import Fold


@dataclass(frozen=True, slots=True)
class Measure:
    """What a per-test number means, as its workflow declares it with the
    task's values in: how it folds over tests, which way is better, and its
    bounds.
    """

    fold: Fold | None = None
    better: Better | None = None
    at_least: Fraction | None = None
    at_most: Fraction | None = None

    def noted(self) -> dict[str, str]:
        """The measure as a publication's note keeps it."""
        found: dict[str, str] = {}
        if self.fold is not None:
            found["fold"] = self.fold.value
        if self.better is not None:
            found["better"] = self.better.value
        if self.at_least is not None:
            found["at_least"] = written(self.at_least)
        if self.at_most is not None:
            found["at_most"] = written(self.at_most)
        return found


def measure_noted(value: object) -> Measure | None:
    """A measure as a publication's note keeps it, or none for one that does
    not read.
    """
    if not isinstance(value, dict):
        return None
    try:
        fold = Fold(value["fold"]) if "fold" in value else None
        better = Better(value["better"]) if "better" in value else None
        at_least = Fraction(str(value["at_least"])) if "at_least" in value else None
        at_most = Fraction(str(value["at_most"])) if "at_most" in value else None
    except ValueError:
        return None
    return Measure(fold, better, at_least, at_most)


@dataclass(frozen=True, slots=True)
class ValueMeaning:
    """One reported value as a page names it: its name in the report, its
    title and unit, none where the workflow gives none, and, on a number
    reported for each test, how it folds and which way is better.
    """

    name: str
    title: str | None = None
    unit: str | None = None
    fold: Fold | None = None
    better: Better | None = None

    def noted(self) -> dict[str, str]:
        """The words as a publication's note keeps them; the fold and the
        direction are kept with the value's other meanings.
        """
        found: dict[str, str] = {}
        if self.title is not None:
            found["title"] = self.title
        if self.unit is not None:
            found["unit"] = self.unit
        return found


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


def in_sentence(title: str) -> str:
    """A value's title inside a sentence, "run time": its first letter
    lowered, unless its first word is an abbreviation such as "CPU".
    """
    if len(title) > 1 and title[1].islower():
        return title[0].lower() + title[1:]
    return title


def with_measures(
    words: Iterable[ValueMeaning], measures: Mapping[str, Measure]
) -> tuple[ValueMeaning, ...]:
    """`words`, each with the fold and direction `measures` give its name;
    with no words, as a publication made before they were frozen has, every
    measured value by name alone, in name order.
    """
    listed = tuple(words) or tuple(ValueMeaning(name) for name in sorted(measures))
    found: list[ValueMeaning] = []
    for meaning in listed:
        measure = measures.get(meaning.name)
        found.append(
            ValueMeaning(
                meaning.name,
                meaning.title,
                meaning.unit,
                measure.fold if measure is not None else None,
                measure.better if measure is not None else None,
            )
        )
    return tuple(found)


def shown_meanings(
    meanings: Iterable[ValueMeaning], sealed: Collection[str], *, revealed: bool
) -> tuple[ValueMeaning, ...]:
    """The meanings a result is served with: every one once `revealed`,
    otherwise those of the values no sealed step holds back, so a value's
    words say no more of a sealed step than its number would.
    """
    return tuple(meaning for meaning in meanings if revealed or meaning.name not in sealed)


def meanings_noted(value: object) -> tuple[ValueMeaning, ...]:
    """The words a publication's note keeps, in the order it lists them;
    an entry that does not read is named by its key alone.
    """
    if not isinstance(value, dict):
        return ()
    found: list[ValueMeaning] = []
    for name, noted in value.items():
        words = noted if isinstance(noted, dict) else {}
        title = words.get("title")
        unit = words.get("unit")
        found.append(
            ValueMeaning(
                str(name),
                title if isinstance(title, str) else None,
                unit if isinstance(unit, str) else None,
            )
        )
    return tuple(found)


__all__ = [
    "Measure",
    "ReportMeaning",
    "ValueMeaning",
    "in_sentence",
    "meanings_noted",
    "measure_noted",
    "shown_meanings",
    "with_measures",
]
