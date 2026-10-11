"""`B`, the best value relative credit is measured against, worked out once a
read for each generation, value and direction it is asked for: two
publications of one generation may name different values in `relative`,
since a change of `credit` alone is no change of how the task grades.
"""

from fractions import Fraction
from types import SimpleNamespace
from typing import Any

from forge.domain.scoring import Better, Measure
from forge.domain.showing import NOTHING_SEALED, Graded
from forge.services.scores import Scorer

RESULT = {
    "stopped": None,
    "tests": [{"test": "main/1", "outcome": "accepted", "values": {"time_ms": 30, "memory_kb": 7}}],
}


def _scorer() -> Scorer:
    current: Any = SimpleNamespace(
        publications=[SimpleNamespace(id="acme/spring/sum#1")], generation=lambda _: 1
    )
    scorer = Scorer(ctx=None, current=current, contest=None)  # type: ignore[arg-type]

    async def candidates() -> list[Any]:
        return [SimpleNamespace(result=RESULT, publication_id="acme/spring/sum#1")]

    scorer._candidates = candidates  # type: ignore[method-assign]
    return scorer


def _graded(name: str, better: Better) -> Graded:
    return Graded(("main/1",), {}, NOTHING_SEALED, None, {name: Measure(better=better)}, 1)


async def test_each_value_of_one_generation_has_its_own_best() -> None:
    scorer = _scorer()

    time = await scorer._best(_graded("time_ms", Better.LOWER), "time_ms")
    memory = await scorer._best(_graded("memory_kb", Better.LOWER), "memory_kb")

    assert time == {"main/1": Fraction(30)}
    assert memory == {"main/1": Fraction(7)}
