"""What a page names each reported value by (TASK-FORMAT.md section 1.3):
its `title`, "Run time", and its `unit`, "ms", on a number only, as the
workflow's `report` gives them, with how the value folds over tests and
which way is better, so a page can say "Run time (ms), the longest".

The words are frozen with a publication, in its note beside what each
per-test number means, in the order the workflow reports the values, so a
result, a board or a form never reads the workflow again. A publication
made before they were frozen lists its per-test numbers by name alone, and
a page names each by its key.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from forge.domain.scoring import Better, Measure
from forge.domain.workflow_definition import Fold


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


__all__ = ["ValueMeaning", "in_sentence", "meanings_noted", "shown_meanings", "with_measures"]
