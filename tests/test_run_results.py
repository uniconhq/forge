"""A run task's result (SIMPLER-CORE.md 9.2 to 9.4, 15 and 17): islands
scores a correct, a wrong and a slow submission as 9 writes them, a wrong
sample leaving `sample_time` missing; cartpole shows `reward` at once and
`windy` and its runs only after the close, though every run is early; a
run that faulted makes its values wait, told through an early run and not
through a late one; a run still to come waits, and one never run is
missing; until the reveal an entrant reads the early row alone, whatever
the late row does, a run a later save added never waits, and a value a
save moved to `now` is missing where it read a run the grading ran late;
the facts
count from the due and the start, 0 before, and `compiled` reads an
untainted compile done once, waiting while a row is still to run it;
what is shown before and after the reveal;
an entrant's texts are read by where they are shown, never by the output
that holds them; a key's file not yet shown is never named; a value
reading a column the entrant could set is marked; a task with nothing
shown during the contest says so; and organisers see everything at once,
a run the live publication dropped among it.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any, NamedTuple

from forge.domain.blocks.evaluate import Column
from forge.domain.blocks.numbers import Better
from forge.domain.definitions import Scoring, parse_task
from forge.domain.release import seconds, seconds_late
from forge.domain.run_results import (
    NOT_RUN,
    Worked,
    facts_of,
    run_grading,
    worked_out,
)
from forge.domain.run_showing import Shown, TextAt, addresses, bare, shown, texts
from forge.domain.run_task import Run
from tests.test_run_task import CARTPOLE_TASK, ISLANDS

COLUMNS = {
    "score": Column(Better.HIGHER, Fraction(0), Fraction(1)),
    "time_ms": Column(Better.LOWER, Fraction(0), None),
    "memory_kb": Column(Better.LOWER, Fraction(0), None),
    "reward": Column(Better.HIGHER, None, None),
}
FACTS = {"seconds_late": Fraction(0), "seconds": Fraction(60), "compiled": Fraction(1)}
ISLAND_RUNS = (
    Run("r1", {"input": "samples/1.in", "answer": "samples/1.ans"}, True),
    Run("r2", {"input": "samples/2.in", "answer": "samples/2.ans"}, True),
    Run("r3", {"input": "tests/3.in", "answer": "tests/3.ans"}, False),
    Run("r4", {"input": "tests/4.in", "answer": "tests/4.ans"}, False),
)
ISLAND_FILES = frozenset(
    f"{folder}/{n}.{kind}"
    for folder, numbers in (("samples", (1, 2)), ("tests", (3, 4)))
    for n in numbers
    for kind in ("in", "ans")
)
CARTPOLE_RUNS = tuple(
    Run(f"r{n}", {"env": env, "seed": seed}, True)
    for n, (env, seed) in enumerate(
        ((env, seed) for env in ("cartpole", "cartpole-wind") for seed in (1, 2, 3)), start=1
    )
)
LOG = {"output": "0199a2c1-0000-7000-8000-000000000001", "name": "log"}
LOG_SHOWN = ISLANDS.replace("sample_tests, samples/]", "sample_tests, samples/, log]")
"""islands showing the compile log with its runs."""


def _scoring(text: str) -> Scoring:
    scoring = parse_task(text).scoring
    assert scoring is not None
    return scoring


def _row(
    verdicts: Mapping[str, str | tuple[str, Mapping[str, Any]]],
    *,
    outcomes: Mapping[str, str] | None = None,
    log: bool = False,
) -> dict[str, Any]:
    """A row's result: each run by id with its verdict, and its reports, or
    an accepted run's score 1, time 12 and a message."""
    runs = []
    for run, given in verdicts.items():
        verdict, reports = given if isinstance(given, tuple) else (given, None)
        if reports is None:
            reports = {"score": 1, "time_ms": 12} if verdict == "accepted" else {}
        message = {"output": f"out-{run}", "name": "message"}
        runs.append(
            {
                "id": run,
                "key": {},
                "verdict": verdict,
                "passed": 1 if verdict == "accepted" else 0,
                "reports": reports,
                "texts": {},
            }
            | ({"message": message} if verdict == "accepted" else {})
        )
    once = {"reports": {}, "texts": {"log": LOG} if log else {}, "outcomes": outcomes or {}}
    return {"schema_version": 6, "runs": runs, "once": once}


class Scored(NamedTuple):
    """A grading worked out from both its rows and from its early row
    alone."""

    whole: Worked
    alone: Worked


def _worked(
    runs: Sequence[Run],
    text: str,
    early: Mapping[str, Any] | None,
    late: Mapping[str, Any] | None,
    *,
    waiting: frozenset[str] = frozenset(),
    facts: Mapping[str, Fraction] = FACTS,
) -> Worked:
    grading = run_grading(runs, early, late, waiting=waiting, compiling={"compile"})
    return Worked(grading, worked_out(_scoring(text), runs, grading, facts, COLUMNS), facts)


def _islands(
    early: Mapping[str, Any] | None,
    late: Mapping[str, Any] | None,
    *,
    waiting: frozenset[str] = frozenset(),
    early_waiting: frozenset[str] = frozenset(),
    text: str = ISLANDS,
    runs: Sequence[Run] = ISLAND_RUNS,
) -> Scored:
    return Scored(
        _worked(runs, text, early, late, waiting=waiting),
        _worked(runs, text, early, None, waiting=early_waiting),
    )


def _numbers(outcomes: Mapping[str, Any]) -> dict[str, Fraction | str | None]:
    return {
        name: "waiting" if outcome.waiting else outcome.number for name, outcome in outcomes.items()
    }


def _seen(
    scored: Scored,
    *,
    revealed: bool = False,
    everything: bool = False,
    text: str = ISLANDS,
    runs: Sequence[Run] = ISLAND_RUNS,
    files: frozenset[str] = ISLAND_FILES,
    self_reported: frozenset[str] = frozenset(),
    late: frozenset[str] = frozenset(),
) -> Shown:
    return shown(
        _scoring(text),
        runs,
        files,
        scored.whole,
        lambda: scored.alone,
        self_reported,
        revealed=revealed,
        everything=everything,
        late=late,
    )


def _values(seen: Shown) -> dict[str, Fraction | str | None]:
    return {value.name: "waiting" if value.waiting else value.number for value in seen.values}


def test_islands_scores_a_correct_a_wrong_and_a_slow_submission_as_9_writes_them() -> None:
    correct = _islands(
        _row({"r1": "accepted", "r2": "accepted"}), _row({"r3": "accepted", "r4": "accepted"})
    )
    wrong = _islands(
        _row({"r1": "accepted", "r2": "accepted"}),
        _row({"r3": "accepted", "r4": ("wrong_answer", {"time_ms": 9})}),
    )
    every = dict.fromkeys(("r1", "r2", "r3", "r4"), "time_limit")
    slow = _islands(_row({"r1": "time_limit", "r2": "time_limit"}), _row(every))

    assert _numbers(correct.whole.outcomes) == {
        "sample_score": 1,
        "sample_time": 12,
        "st1": 1,
        "st2": 1,
        "total": 100,
    }
    # A wrong answer's score counts as its worst bound, 0.
    assert _numbers(wrong.whole.outcomes) == {
        "sample_score": 1,
        "sample_time": 12,
        "st1": 1,
        "st2": 0,
        "total": 40,
    }
    # time_ms has no worst bound, so a wrong sample leaves sample_time missing.
    assert _numbers(slow.whole.outcomes) == {
        "sample_score": 0,
        "sample_time": None,
        "st1": 0,
        "st2": 0,
        "total": 0,
    }


def test_a_wrong_sample_leaves_sample_time_missing_never_better_than_a_right_one() -> None:
    outcomes = _islands(
        _row({"r1": "accepted", "r2": ("wrong_answer", {"time_ms": 1})}),
        _row({"r3": "accepted", "r4": "accepted"}),
    ).whole.outcomes
    assert outcomes["sample_time"].missing
    assert outcomes["sample_score"].number == Fraction(1, 2)


def test_cartpole_shows_reward_at_once_and_windy_and_every_run_only_after_the_close() -> None:
    reward = {f"r{n}": ("accepted", {"reward": n * 10}) for n in range(1, 7)}
    scored = _islands(_row(reward), None, text=CARTPOLE_TASK, runs=CARTPOLE_RUNS)

    before = _seen(scored, text=CARTPOLE_TASK, runs=CARTPOLE_RUNS)
    after = _seen(scored, text=CARTPOLE_TASK, runs=CARTPOLE_RUNS, revealed=True)

    assert _values(before) == {"reward": 35}
    # Every run is early, since reward reads all, but all is shown after the close.
    assert before.runs == ()
    assert _values(after) == {"reward": 35, "windy": 50}
    assert [(run["group"], run["place"]) for run in after.runs] == [
        ("all", place) for place in range(1, 7)
    ]


def test_before_the_reveal_only_nows_entries_are_shown_and_after_it_after_closes_too() -> None:
    scored = _islands(
        _row({"r1": "accepted", "r2": "accepted"}), _row({"r3": "accepted", "r4": "accepted"})
    )

    before = _seen(scored)
    after = _seen(scored, revealed=True)

    assert _values(before) == {"sample_score": 1, "sample_time": 12}
    assert [(run["group"], run["place"], run["key"]) for run in before.runs] == [
        ("sample_tests", 1, {"input": "samples/1.in", "answer": "samples/1.ans"}),
        ("sample_tests", 2, {"input": "samples/2.in", "answer": "samples/2.ans"}),
    ]
    assert "id" not in before.runs[0]
    assert _values(after) == {
        "sample_score": 1,
        "sample_time": 12,
        "st1": 1,
        "st2": 1,
        "total": 100,
    }
    assert [(run["group"], run["place"]) for run in after.runs[2:]] == [
        ("all", 1),
        ("all", 2),
        ("all", 3),
        ("all", 4),
    ]
    # No entry of show lists tests/, so a run's key never names its files.
    assert [run["key"] for run in after.runs[4:]] == [{}, {}]


def test_a_run_that_faulted_is_told_being_checked_through_an_early_run_only() -> None:
    # The early row failed: its runs wait, and so do the late ones, since no
    # late row is made until staff act.
    early_fault = _islands(
        _row({"r1": "accepted", "r2": "system_error"}),
        None,
        waiting=frozenset({"r1", "r2", "r3", "r4"}),
        early_waiting=frozenset({"r1", "r2"}),
    )
    late_fault = _islands(
        _row({"r1": "accepted", "r2": "accepted"}),
        _row({"r3": "accepted", "r4": "system_error"}),
        waiting=frozenset({"r3", "r4"}),
    )

    assert _numbers(early_fault.whole.outcomes)["sample_score"] == "waiting"
    assert _numbers(late_fault.whole.outcomes) == {
        "sample_score": 1,
        "sample_time": 12,
        "st1": 1,
        "st2": "waiting",
        "total": "waiting",
    }
    # Through an early run: "being checked", with the run itself.
    seen = _seen(early_fault)
    assert _values(seen) == {"sample_score": "waiting", "sample_time": "waiting"}
    assert [run["verdict"] for run in seen.runs] == ["accepted", "system_error"]
    # Through a late run: nothing until that run is shown, after the close.
    assert _values(_seen(late_fault)) == {"sample_score": 1, "sample_time": 12}
    # After it, while no group shows r4, st2 and total stay untold.
    unlisted = ISLANDS.replace(
        "after_close: [st1, st2, total, all]", "after_close: [st1, st2, total]"
    )
    assert _values(_seen(late_fault, revealed=True, text=unlisted)) == {
        "sample_score": 1,
        "sample_time": 12,
        "st1": 1,
    }
    after = _seen(late_fault, revealed=True)
    assert (_values(after)["st2"], _values(after)["total"]) == ("waiting", "waiting")
    assert after.runs[-1]["verdict"] == "system_error"


def test_a_run_still_to_come_waits_and_one_never_run_is_missing() -> None:
    early = _row({"r1": "accepted", "r2": "accepted"})
    coming = _islands(early, None, waiting=frozenset({"r3", "r4"})).whole
    never = _islands(early, None).whole

    assert coming.grading.read["r3"].verdict == "system_error"
    assert _numbers(coming.outcomes)["total"] == "waiting"
    assert never.grading.read["r3"].verdict == NOT_RUN
    # A run never run gave no score, so it counts as score's worst bound.
    assert _numbers(never.outcomes)["total"] == 0
    # A run of a row's result the live publication dropped is not read, but
    # kept apart for organisers.
    dropped = _islands(_row({"r1": "accepted", "r2": "accepted", "r9": "accepted"}), None)
    assert set(dropped.whole.grading.results) == {"r1", "r2"}
    assert set(dropped.whole.grading.dropped) == {"r9"}


def test_until_the_reveal_an_entrant_reads_the_early_row_alone_whatever_the_late_row_does() -> None:
    # A later save added the sample r5, which neither row's plan holds: it
    # never waits, so a value reading it does not move with the late row.
    text = LOG_SHOWN.replace(
        "    sample_tests: {input: [samples/1.in, samples/2.in]}",
        "    sample_tests: {input: [samples/1.in, samples/2.in, samples/5.in]}",
    ).replace(
        "      - {answer: tests/4.ans, input: tests/4.in}",
        "      - {answer: tests/4.ans, input: tests/4.in}\n      - [samples/5.in, samples/5.ans]",
    )
    runs = (*ISLAND_RUNS, Run("r5", {"input": "samples/5.in", "answer": "samples/5.ans"}, True))
    files = ISLAND_FILES | {"samples/5.in", "samples/5.ans"}
    early = _row({"r1": "accepted", "r2": "accepted"}, outcomes={"compile": "accepted"}, log=True)
    late_rows: dict[str, Mapping[str, Any] | None] = {
        "pending": None,
        "done": _row({"r3": "accepted", "r4": "accepted"}),
        "faulted": _row({"r3": "system_error", "r4": "accepted"}),
        # The planner never gives a late row a compile while the early row
        # has one; were it to, the entrant would not hear of it early either.
        "compile_error": _row(
            dict.fromkeys(("r3", "r4"), "compile_error"), outcomes={"compile": "compile_error"}
        ),
    }

    seen = {}
    for name, late in late_rows.items():
        waiting = frozenset({"r3", "r4"}) if name in ("pending", "faulted") else frozenset()
        scored = _islands(early, late, waiting=waiting, text=text, runs=runs)
        assert scored.whole.grading.read["r5"].verdict == NOT_RUN
        seen[name] = _seen(scored, text=text, runs=runs, files=files)

    first = seen["pending"]
    assert _values(first) == {"sample_score": Fraction(2, 3), "sample_time": None}
    assert (first.compiled, first.once["texts"]) == (1, {"log": LOG})
    for each in seen.values():
        assert (each.values, each.runs, each.once, each.compiled) == (
            first.values,
            first.runs,
            first.once,
            first.compiled,
        )


MOVED = {
    "groups": {"samples": {"input": "a"}},
    "values": {"total": {"block": "sum", "over": {"runs": "all"}, "of": {"run": ["all", "score"]}}},
    "show": {"now": ["total", "samples"]},
}
"""A total over every run, which a save moved to `now` after a grading that
ran r2 late."""
MOVED_RUNS = (Run("r1", {"input": "a"}, True), Run("r2", {"input": "b"}, False))


def test_a_value_moved_to_now_is_missing_until_the_reveal_where_it_read_a_late_run() -> None:
    scoring = Scoring.model_validate(MOVED)
    early, late = _row({"r1": "accepted"}), _row({"r2": "accepted"})

    def worked(first: Mapping[str, Any], then: Mapping[str, Any] | None) -> Worked:
        grading = run_grading(MOVED_RUNS, first, then, waiting=(), compiling=())
        return Worked(grading, worked_out(scoring, MOVED_RUNS, grading, FACTS, COLUMNS), FACTS)

    whole, alone = worked(early, late), worked(early, None)

    def seen(late_runs: frozenset[str], *, revealed: bool = False, everything: bool = False) -> Any:
        found = shown(
            scoring,
            MOVED_RUNS,
            (),
            whole,
            lambda: alone,
            (),
            revealed=revealed,
            everything=everything,
            late=late_runs,
        )
        return [(each.name, each.number, each.waiting) for each in found.values]

    # Staff read 2; the entrant reads it missing until the reveal, not 1
    # from r2 read as never run, and 2 from then on.
    assert seen(frozenset({"r2"}), everything=True)[0][:2] == ("total", 2)
    assert seen(frozenset({"r2"})) == [("total", None, False)]
    assert seen(frozenset({"r2"}), revealed=True) == [("total", 2, False)]
    # A grading made after the save ran every run early: nothing ran late.
    both = _row({"r1": "accepted", "r2": "accepted"})
    whole = alone = worked(both, None)
    assert seen(frozenset()) == [("total", 2, False)]


def test_seconds_late_counts_from_the_due_and_seconds_from_the_start_each_0_before() -> None:
    due = datetime(2026, 9, 26, 14, 0, tzinfo=UTC)
    start = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
    at = datetime(2026, 9, 26, 14, 30, 0, 500_000, tzinfo=UTC)

    assert seconds_late(due, at) == Fraction(3601, 2)
    # An extension moves the row's due.
    assert seconds_late(due + timedelta(minutes=10), at) == Fraction(2401, 2)
    assert seconds_late(due, due) == 0
    assert seconds_late(due, due - timedelta(hours=1)) == 0
    assert seconds_late(None, at) == 0
    assert seconds(start, start - timedelta(seconds=1)) == 0
    assert facts_of(due, start, at, 0) == {
        "seconds_late": Fraction(3601, 2),
        "seconds": Fraction(32401, 2),
        "compiled": 0,
    }


def test_compiled_is_0_on_a_compile_error_of_an_untainted_step_done_once() -> None:
    every = dict.fromkeys(("r1", "r2"), "compile_error")
    failed = _row(every, outcomes={"compile": "compile_error"})
    stopped = run_grading(ISLAND_RUNS, failed, None, waiting=(), compiling={"compile"})
    tainted = run_grading(ISLAND_RUNS, failed, None, waiting=(), compiling=set())
    built = run_grading(
        ISLAND_RUNS,
        _row({"r1": "accepted"}, outcomes={"compile": "accepted"}),
        None,
        waiting=(),
        compiling={"compile"},
    )
    assert (stopped.compiled, tainted.compiled, built.compiled) == (0, 1, 1)


def test_compiled_waits_while_a_row_whose_plan_compiles_is_still_to_give_it() -> None:
    text = ISLANDS.replace("  values:\n", "  values:\n    built: {fact: compiled}\n")
    start = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
    early = _row({"r1": "accepted", "r2": "accepted"})

    def worked(late: Mapping[str, Any] | None, coming: set[str]) -> tuple[Any, Any, bool]:
        grading = run_grading(
            ISLAND_RUNS, early, late, waiting=(), compiling={"compile"}, compiling_waiting=coming
        )
        facts = facts_of(None, start, start, grading.compiled)
        built = _numbers(worked_out(_scoring(text), ISLAND_RUNS, grading, facts, COLUMNS))["built"]
        return grading.compiled, built, "compiled" in facts

    late = _row({"r3": "accepted", "r4": "accepted"}, outcomes={"compile": "accepted"})
    failed = _row(
        dict.fromkeys(("r3", "r4"), "compile_error"), outcomes={"compile": "compile_error"}
    )
    # The late row's plan compiles: until it gives the outcome, it waits.
    assert worked(None, {"compile"}) == (None, "waiting", False)
    assert worked(late, {"compile"}) == (1, 1, True)
    assert worked(failed, {"compile"}) == (0, 0, True)
    # A compile no row's plan holds still to give never waits.
    assert worked(None, set()) == (1, 1, True)


def test_compiled_and_the_compile_log_are_shown_once_a_run_is() -> None:
    scored = _islands(
        _row({"r1": "accepted", "r2": "accepted"}, outcomes={"compile": "accepted"}, log=True),
        _row({"r3": "accepted", "r4": "accepted"}),
    )
    values_only = LOG_SHOWN.replace(
        "now: [sample_score, sample_time, sample_tests, samples/, log]", "now: [sample_score, log]"
    ).replace("after_close: [st1, st2, total, all]", "after_close: [total]")

    seen = _seen(scored, text=LOG_SHOWN)
    unseen = _seen(scored, text=values_only, revealed=True)
    unlisted = _seen(scored)

    assert (seen.compiled, seen.once["texts"]) == (1, {"log": LOG})
    assert (unseen.compiled, unseen.once, unseen.runs) == (None, {"reports": {}, "texts": {}}, ())
    assert texts(unseen.runs, unseen.once) == set()
    # A report of a step done once that show does not list is never shown,
    # though `compiled` is, with the runs.
    assert (unlisted.compiled, unlisted.once) == (1, {"reports": {}, "texts": {}})
    assert addresses(unlisted.runs, unlisted.once) == {
        TextAt("sample_tests", 1, "message", None): ("out-r1", "message"),
        TextAt("sample_tests", 2, "message", None): ("out-r2", "message"),
    }


def test_an_entrant_reads_a_text_by_where_it_is_shown_and_is_never_sent_its_output() -> None:
    scored = _islands(
        _row({"r1": "accepted", "r2": "accepted"}, outcomes={"compile": "accepted"}, log=True),
        _row({"r3": "accepted", "r4": "accepted"}),
    )
    seen = _seen(scored, text=LOG_SHOWN)

    assert addresses(seen.runs, seen.once) == {
        TextAt(None, None, "texts", "log"): (LOG["output"], "log"),
        TextAt("sample_tests", 1, "message", None): ("out-r1", "message"),
        TextAt("sample_tests", 2, "message", None): ("out-r2", "message"),
    }
    runs, once = bare(seen.runs, seen.once)
    assert (runs[0]["message"], once["texts"]) == ({}, {"log": {}})
    assert "output" not in repr((runs, once))
    # What is not shown has no place to read it from.
    unseen = _seen(scored, text=LOG_SHOWN.replace("sample_tests, samples/, log]", "samples/, log]"))
    assert addresses(unseen.runs, unseen.once) == {}


def test_a_value_reading_a_column_the_entrant_could_set_is_marked() -> None:
    scored = _islands(
        _row({"r1": "accepted", "r2": "accepted"}), _row({"r3": "accepted", "r4": "accepted"})
    )
    marked = frozenset({"sample_score", "st1", "st2", "total"})

    seen = _seen(scored, revealed=True, self_reported=marked)

    assert {value.name: value.self_reported for value in seen.values} == {
        "sample_score": True,
        "sample_time": False,
        "st1": True,
        "st2": True,
        "total": True,
    }


def test_a_task_with_nothing_shown_during_the_contest_tells_it_until_the_reveal() -> None:
    nothing_early = ISLANDS.replace(
        "now: [sample_score, sample_time, sample_tests, samples/]", "now: [samples/]"
    )
    scored = _islands(None, _row(dict.fromkeys(("r1", "r2"), "accepted")))

    assert _seen(scored, text=nothing_early).later
    assert not _seen(scored, text=nothing_early, revealed=True).later
    assert not _seen(scored).later


def test_organisers_see_every_value_and_run_at_once_with_what_each_read() -> None:
    scored = _islands(
        _row({"r1": "accepted", "r2": "accepted", "r9": "accepted"}, log=True),
        _row({"r3": "accepted", "r4": "wrong_answer"}),
    )

    seen = _seen(scored, everything=True)

    total = next(value for value in seen.values if value.name == "total")
    assert (total.number, total.show, total.runs, total.values) == (
        Fraction(40),
        "after_close",
        ("r3", "r4"),
        ("st1", "st2"),
    )
    assert [(run["group"], run["place"], run["id"], "dropped" in run) for run in seen.runs] == [
        ("all", 1, "r1", False),
        ("all", 2, "r2", False),
        ("all", 3, "r3", False),
        ("all", 4, "r4", False),
        ("all", 5, "r9", True),
    ]
    assert seen.runs[2]["key"] == {"input": "tests/3.in", "answer": "tests/3.ans"}
    assert seen.runs[4]["dropped"] is True
    assert seen.facts == FACTS and seen.compiled == 1 and not seen.later
    assert seen.once["texts"] == {"log": LOG}
    # Organisers read a text by the output that holds it.
    assert ("out-r1", "message") in texts(seen.runs, seen.once)
