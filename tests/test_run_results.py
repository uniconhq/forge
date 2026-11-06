"""A task's result (SIMPLER-CORE.md 9.2 to 9.4, 15 and 17): islands
scores a correct, a wrong and a slow submission as 9 writes them, a wrong
sample leaving `sample_time` missing; cartpole shows `reward` at once and
`windy` and its runs only after the close, though every run is early; a
run that faulted makes its values wait, told through an early run and not
through a late one; a run still to come waits, and one never run is
missing; until the reveal an entrant reads the early row alone, whatever
the late row does, a run a later save added never waits, and a value a
save moved to `always` is missing where it read a run the grading ran late;
the facts
count from the due and the start, 0 before, and `compiled` reads an
untainted compile done once, waiting while a row is still to run it;
what is shown before and after the deadline, and with a release, each
moment apart; a run entry shows the fields it names and nothing else,
`first_not_passed` the first run that did not pass, and a shared input a
file by its path;
an entrant's texts are read by where they are shown, never by the output
that holds them; no entrant is sent a run's id; a value
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
from forge.domain.run_plan import RunPlan
from forge.domain.run_results import (
    NOT_RUN,
    Worked,
    compilers,
    facts_of,
    run_grading,
    worked_out,
)
from forge.domain.run_showing import Shown, TextAt, addresses, bare, shown, shown_file, texts
from forge.domain.run_task import Run
from forge.domain.types import Type
from tests.conftest import released
from tests.test_run_task import CARTPOLE_TASK, ISLANDS, ISLANDS_AFTER, ISLANDS_ALWAYS

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
CARTPOLE_RUNS = tuple(
    Run(f"r{n}", {"env": env, "seed": seed}, True)
    for n, (env, seed) in enumerate(
        ((env, seed) for env in ("cartpole", "cartpole-wind") for seed in (1, 2, 3)), start=1
    )
)
LOG = {"output": "0199a2c1-0000-7000-8000-000000000001", "name": "log"}
SAMPLE_FIELDS = "fields: [input, answer, verdict, passed, score, time_ms]"
LOG_SHOWN = ISLANDS.replace(
    "      values: [sample_score, sample_time]\n",
    "      values: [sample_score, sample_time]\n      outputs: [compiled, log]\n",
).replace(SAMPLE_FIELDS, "fields: [input, answer, verdict, passed, message, score, time_ms]")
"""islands showing `compiled`, the compile log and each sample's message."""


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


def _axes(text: str) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(axis.inputs) for axis in parse_task(text).repeat)


def _seen(
    scored: Scored,
    *,
    revealed: bool = False,
    released: bool = False,
    everything: bool = False,
    text: str = ISLANDS,
    runs: Sequence[Run] = ISLAND_RUNS,
    self_reported: frozenset[str] = frozenset(),
    late: frozenset[str] = frozenset(),
    titles: Mapping[str, tuple[str | None, ...]] | None = None,
    meanings: Mapping[str, tuple[str | None, str | None]] | None = None,
    settings: Mapping[str, object] | None = None,
    kinds: Mapping[str, Type] | None = None,
) -> Shown:
    return shown(
        _scoring(text),
        runs,
        scored.whole,
        lambda: scored.alone,
        self_reported,
        revealed=revealed,
        released=released,
        everything=everything,
        late=late,
        titles=titles,
        axes=_axes(text),
        meanings=meanings,
        settings=settings,
        kinds=kinds,
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


def test_before_the_reveal_only_always_entries_are_shown_and_after_it_after_deadline_too() -> None:
    scored = _islands(
        _row({"r1": "accepted", "r2": "accepted"}), _row({"r3": "accepted", "r4": "accepted"})
    )

    before = _seen(scored)
    after = _seen(scored, revealed=True)

    assert _values(before) == {"sample_score": 1, "sample_time": 12}
    assert [(run["group"], run["place"], run["tuples"]) for run in before.runs] == [
        (
            "sample_tests",
            1,
            [{"title": None, "key": {"input": "samples/1.in", "answer": "samples/1.ans"}}],
        ),
        (
            "sample_tests",
            2,
            [{"title": None, "key": {"input": "samples/2.in", "answer": "samples/2.ans"}}],
        ),
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
    # The entry of all names no input, so its runs' tuples name none.
    assert [run["tuples"] for run in after.runs[4:]] == [
        [{"title": None, "key": {}}],
        [{"title": None, "key": {}}],
    ]


def test_a_run_serves_one_tuple_per_axis_and_each_output_by_its_title() -> None:
    runs = (
        Run("r1", {"env": "cartpole", "seed": 1}, True),
        Run("r2", {"env": "cartpole-wind", "seed": 2}, True),
    )
    text = CARTPOLE_TASK.replace(
        "fields: [env, seed, verdict, passed, reward]",
        "fields: [env, seed, verdict, passed, score, time_ms]",
    )
    scored = _islands(
        _row({"r1": "accepted", "r2": ("wrong_answer", {})}),
        None,
        text=text,
        runs=runs,
    )

    seen = _seen(
        scored,
        text=text,
        runs=runs,
        revealed=True,
        titles={"r1": ("Big grid", None)},
        meanings={"time_ms": ("Run time", "ms"), "score": (None, None)},
    )

    assert seen.runs[0]["tuples"] == [
        {"title": "Big grid", "key": {"env": "cartpole"}},
        {"title": None, "key": {"seed": 1}},
    ]
    assert seen.runs[1]["tuples"] == [
        {"title": None, "key": {"env": "cartpole-wind"}},
        {"title": None, "key": {"seed": 2}},
    ]
    assert seen.runs[0]["reports"] == [
        {"title": "score", "unit": None, "number": 1},
        {"title": "Run time", "unit": "ms", "number": 12},
    ]
    assert seen.runs[1]["reports"] == []


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
    # Through a late run: nothing until that run is shown, after the deadline.
    assert _values(_seen(late_fault)) == {"sample_score": 1, "sample_time": 12}
    # After it, while no run entry shows r4, st2 and total stay untold.
    unlisted = ISLANDS.replace(
        "      runs: [{of: all, fields: [verdict, passed, score, time_ms]}]\n", ""
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
        seen[name] = _seen(scored, text=text, runs=runs)

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
    "show": {"always": {"values": ["total"], "runs": [{"of": "samples", "fields": ["verdict"]}]}},
}
"""A total over every run, which a save moved to `always` after a grading
that ran r2 late."""
MOVED_RUNS = (Run("r1", {"input": "a"}, True), Run("r2", {"input": "b"}, False))


def test_a_value_moved_to_always_is_missing_until_the_reveal_where_it_read_a_late_run() -> None:
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


def test_compiled_is_0_on_a_compile_error_of_a_step_compiling_the_entrants_code() -> None:
    every = dict.fromkeys(("r1", "r2"), "compile_error")
    failed = _row(every, outcomes={"compile": "compile_error"})
    stopped = run_grading(ISLAND_RUNS, failed, None, waiting=(), compiling={"compile"})
    elsewhere = run_grading(ISLAND_RUNS, failed, None, waiting=(), compiling=set())
    built = run_grading(
        ISLAND_RUNS,
        _row({"r1": "accepted"}, outcomes={"compile": "accepted"}),
        None,
        waiting=(),
        compiling={"compile"},
    )
    assert (stopped.compiled, elsewhere.compiled, built.compiled) == (0, 1, 1)


def test_a_plan_saved_before_steps_said_they_compile_reads_compiled_as_it_did() -> None:
    """The runner's last release's plan runs compile@v3 and says no step's
    `compiled`: the steps done once that are not tainted are what
    `compiled` reads, a value's program's build not among them."""
    old = RunPlan.model_validate_json(released("runner", "v0.10.0", "examples/plan-v6.json"))

    assert compilers(old) == {"runner"}


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


def test_compiled_and_the_compile_log_are_shown_only_where_outputs_lists_them() -> None:
    scored = _islands(
        _row({"r1": "accepted", "r2": "accepted"}, outcomes={"compile": "accepted"}, log=True),
        _row({"r3": "accepted", "r4": "accepted"}),
    )
    log_alone = (
        ISLANDS.replace(ISLANDS_ALWAYS, "    always:\n      outputs: [log]\n")
        .replace(ISLANDS_AFTER, "")
        .rstrip()
    )

    seen = _seen(scored, text=LOG_SHOWN)
    alone = _seen(scored, text=log_alone, revealed=True)
    unlisted = _seen(scored)

    assert (seen.compiled, seen.once["texts"]) == (1, {"log": LOG})
    # Listed alone, the log is shown with no run and no `compiled`.
    assert (alone.compiled, alone.once, alone.runs) == (
        None,
        {"reports": {}, "texts": {"log": LOG}},
        (),
    )
    assert texts(alone.runs, alone.once) == {(LOG["output"], "log")}
    # What the steps done once gave that outputs does not list is never
    # shown, nor `compiled`, nor a run's message a run entry does not name.
    assert (unlisted.compiled, unlisted.once) == (None, {"reports": {}, "texts": {}})
    assert addresses(unlisted.runs, unlisted.once) == {}


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
    unseen = _seen(scored)
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
        ISLANDS_ALWAYS, "    always:\n      runs: [{of: sample_tests, fields: [input, answer]}]\n"
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
        "after_deadline",
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
    assert seen.runs[2]["tuples"] == [
        {"title": None, "key": {"input": "tests/3.in", "answer": "tests/3.ans"}}
    ]
    assert seen.runs[4]["dropped"] is True
    assert seen.facts == FACTS and seen.compiled == 1 and not seen.later
    assert seen.once["texts"] == {"log": LOG}
    # Organisers read a text by the output that holds it.
    assert ("out-r1", "message") in texts(seen.runs, seen.once)


def _plain(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def test_a_run_entry_shows_the_fields_it_names_and_nothing_else() -> None:
    score_only = ISLANDS.replace(
        ISLANDS_ALWAYS, "    always:\n      runs: [{of: sample_tests, fields: [score]}]\n"
    )
    ran = _row({"r1": "accepted", "r2": "accepted"}, outcomes={"compile": "accepted"}, log=True)
    for run in ran["runs"]:
        run["texts"] = {"log": LOG}
    # A later workflow version gives each run another number and text.
    more = _row({"r1": "accepted", "r2": "accepted"}, outcomes={"compile": "accepted"}, log=True)
    for run in more["runs"]:
        run["reports"] = {**run["reports"], "memory_kb": 300}
        run["texts"] = {"log": LOG, "hint": LOG}
    more["once"]["reports"] = {"steps": 3}

    seen = _seen(_islands(ran, None), text=score_only)
    added = _seen(_islands(more, None), text=score_only)

    assert _plain(seen.runs) == [
        {
            "group": "sample_tests",
            "place": place,
            "tuples": [{"title": None, "key": {}}],
            "reports": [{"title": "score", "unit": None, "number": 1}],
            "texts": {},
        }
        for place in (1, 2)
    ]
    assert seen.once == {"reports": {}, "texts": {}}
    assert (seen.values, seen.compiled, seen.inputs) == ((), None, {})
    # What a workflow version adds is shown only once show names it.
    assert (added.runs, added.once, added.values) == (seen.runs, seen.once, seen.values)


FAILING = ISLANDS.replace(
    ISLANDS_ALWAYS,
    "    always:\n      runs: [{of: first_not_passed, fields: [verdict, passed]}]\n",
)
"""islands showing, during the contest, the first run that did not pass."""
EVERY_EARLY = tuple(Run(run.id, run.key, True) for run in ISLAND_RUNS)
"""The islands runs, every one early, as `first_not_passed` during the
contest makes them."""


def test_first_not_passed_names_the_first_failing_run_the_same_at_the_deadline() -> None:
    every = {"r1": "accepted", "r2": "accepted", "r3": "wrong_answer", "r4": "time_limit"}
    scored = _islands(_row(every), None, runs=EVERY_EARLY)

    def picked(revealed: bool) -> list[dict[str, Any]]:
        seen = _seen(scored, text=FAILING, runs=EVERY_EARLY, revealed=revealed)
        return [dict(run) for run in seen.runs if run["group"] == "first_not_passed"]

    assert picked(False) == [
        {
            "group": "first_not_passed",
            "place": 1,
            "tuples": [{"title": None, "key": {}}],
            "verdict": "wrong_answer",
            "passed": 0,
            "reports": [],
            "texts": {},
            "labelled": {"group": "st1_tests", "place": 1},
        }
    ]
    assert picked(True) == picked(False)
    # Every run passed: nothing to name.
    passed = _islands(_row(dict.fromkeys(every, "accepted")), None, runs=EVERY_EARLY)
    assert _seen(passed, text=FAILING, runs=EVERY_EARLY).runs == ()


def test_a_value_under_both_later_moments_is_shown_at_whichever_comes_first() -> None:
    both = ISLANDS.replace(
        ISLANDS_AFTER, ISLANDS_AFTER + "    after_release:\n      values: [total]\n"
    )
    scored = _islands(
        _row({"r1": "accepted", "r2": "accepted"}), _row({"r3": "accepted", "r4": "accepted"})
    )

    def total(*, revealed: bool, released: bool) -> list[Fraction | str | None]:
        seen = _seen(scored, text=both, revealed=revealed, released=released)
        return [number for name, number in _values(seen).items() if name == "total"]

    assert total(revealed=False, released=False) == []
    assert total(revealed=True, released=False) == [100]
    assert total(revealed=False, released=True) == [100]
    # A release before the deadline shows after_release, not after_deadline.
    released_only = _seen(scored, text=both, released=True)
    assert set(_values(released_only)) == {"sample_score", "sample_time", "total"}


def test_no_entrant_answer_carries_a_run_id_or_a_count_of_hidden_runs() -> None:
    scored = _islands(
        _row({"r1": "accepted", "r2": "wrong_answer"}),
        _row({"r3": "accepted", "r4": "wrong_answer"}),
    )
    allowed = {
        "group",
        "place",
        "tuples",
        "verdict",
        "passed",
        "label",
        "message",
        "reports",
        "texts",
        "labelled",
    }

    for text in (ISLANDS, LOG_SHOWN, FAILING):
        for revealed in (False, True):
            seen = _seen(scored, text=text, revealed=revealed)
            runs, once = bare(seen.runs, seen.once)
            for run in runs:
                assert set(run) <= allowed, run
            assert "r1" not in repr((runs, once, seen.values))
    staff = _seen(scored, everything=True)
    assert [run["id"] for run in staff.runs] == ["r1", "r2", "r3", "r4"]


def test_a_shared_input_is_shown_by_name_a_file_by_its_path() -> None:
    text = ISLANDS.replace(
        "      values: [sample_score, sample_time]\n",
        "      values: [sample_score, sample_time]\n      inputs: [time_limit, checker]\n",
    ).replace(
        "      values: [st1, st2, total]\n",
        "      values: [st1, st2, total]\n      inputs: [memory_limit]\n",
    )
    settings = {"time_limit": 2, "memory_limit": 256, "checker": "checker/check.py"}
    kinds = {
        "time_limit": Type.NUMBER,
        "memory_limit": Type.NUMBER,
        "checker": Type.FILE,
        "input": Type.FILE,
        "answer": Type.FILE,
    }
    scored = _islands(
        _row({"r1": "accepted", "r2": "accepted"}), _row({"r3": "accepted", "r4": "accepted"})
    )

    before = _seen(scored, text=text, settings=settings, kinds=kinds)
    after = _seen(scored, text=text, revealed=True, settings=settings, kinds=kinds)

    assert before.inputs == {"time_limit": 2, "checker": {"file": "checker/check.py"}}
    assert after.inputs == {**before.inputs, "memory_limit": 256}
    assert before.runs[0]["tuples"] == [
        {
            "title": None,
            "key": {"input": {"file": "samples/1.in"}, "answer": {"file": "samples/1.ans"}},
        }
    ]
    assert shown_file(before.inputs, before.runs, "checker") == (Type.FILE, "checker/check.py")
    assert shown_file(before.inputs, before.runs, "input", "sample_tests", 2) == (
        Type.FILE,
        "samples/2.in",
    )
    # What the answer does not show is no file to serve.
    assert shown_file(before.inputs, before.runs, "time_limit") is None
    assert shown_file(before.inputs, before.runs, "input", "all", 3) is None
