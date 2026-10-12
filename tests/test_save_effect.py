"""What a save does, in the words a running contest asks with before a save
and a save that published answers with: the first line, what runs again, the
scores and what entrants read. Results change when a submission is graded
again, or when the task has a graded submission and the save moves what one
earns or shows; the rate alone, the statement and the title move none.
"""

import pytest

from forge.domain.definitions import parse_contest, parse_task
from forge.domain.save_effect import ReadChanges, SaveEffect, effect_of

CONTEST = parse_contest(
    """\
name: Spring
start: 2026-09-26T10:00:00Z
end: 2026-09-26T15:00:00Z
state: published
visibility: everyone
tasks:
  - id: sum
  - id: other
leaderboards:
  - {name: Standings}
  - {name: Live, over: live}
  - {name: Other, tasks: [other]}
"""
)


def _task(groups: str, extra: str = "", name: str = "Sum") -> str:
    return f"""\
name: {name}
workflow: unicon/classic@v2
{extra}test_groups:
{groups}"""


NOTHING_READ = ReadChanges()
BEFORE = _task("  main: {each: 100}\n  large: {pass: 50, show: after_close}\n")


def _effect(
    after: str,
    *,
    before: str | None = BEFORE,
    changes: tuple[str, ...] = (),
    regrades: int = 0,
    graded: bool = True,
    read: ReadChanges = NOTHING_READ,
) -> SaveEffect:
    return effect_of(
        "sum",
        parse_task(before) if before is not None else None,
        parse_task(after),
        CONTEST,
        changes=changes,
        regrades=regrades,
        graded=graded,
        read=read,
    )


def test_a_save_that_changes_nothing_says_so_in_each_part() -> None:
    assert _effect(BEFORE) == SaveEffect(
        results_change=False,
        summary="Nobody's result changes.",
        runs_again=("No submission runs again: the tests and how they run are the same.",),
        scores=("No score changes.",),
        reading=("Nothing entrants read changes.",),
    )


def test_a_moved_total_names_the_group_with_its_old_points_and_scores_every_board_again() -> None:
    effect = _effect(_task("  main: {each: 110}\n  large: {pass: 50, show: after_close}\n"))

    assert effect.results_change is True
    assert effect.scores == (
        "Points: main is now worth up to 110 (was 100).",
        "Every result is scored again, and the leaderboards Standings and Live with them.",
    )


def test_a_mix_whose_parts_move_together_says_its_points_alone() -> None:
    # Raising a hand-set mix's points, each part in step, changes no rule:
    # only the points line is said, never how the group earns.
    before = _task("  main: {each: 60, pass: 40, pass_at: 0.5}\n")
    after = _task("  main: {each: 66, pass: 44, pass_at: 0.5}\n")

    effect = _effect(after, before=before)

    assert effect.scores == (
        "Points: main is now worth up to 110 (was 100).",
        "Every result is scored again, and the leaderboards Standings and Live with them.",
    )


def test_a_task_with_a_set_worth_names_each_groups_share_of_it() -> None:
    worth = parse_contest(
        """\
name: Spring
start: 2026-09-26T10:00:00Z
end: 2026-09-26T15:00:00Z
state: published
visibility: everyone
tasks:
  - {id: sum, worth: 300}
"""
    )
    effect = effect_of(
        "sum",
        parse_task(BEFORE),
        parse_task(_task("  main: {each: 100}\n  large: {pass: 100, show: after_close}\n")),
        worth,
        changes=(),
        regrades=0,
        graded=False,
        read=ReadChanges(),
    )

    assert effect.scores[:2] == (
        "Points: main is now worth up to 150 (was 200).",
        "Points: large is now worth up to 150 (was 100).",
    )
    # Nobody has a result yet, so nobody's result changes.
    assert effect.summary == "Nobody's result changes."


def test_a_new_mix_of_rules_says_how_the_group_earns_now_and_before() -> None:
    effect = _effect(
        _task(
            "  main: {each: 60, pass: 40, pass_at: 0.5}\n  large: {pass: 50, show: after_close}\n"
        )
    )

    assert effect.scores[0] == (
        "How main earns its points: 60 shared over its tests and 40 when 50% of its tests pass "
        "(was 100 shared over its tests)."
    )


def test_credit_weights_and_show_are_each_said_and_a_show_change_moves_a_live_boards_count() -> (
    None
):
    after = _task(
        "  main: {each: 100, test_weights: {'1': 2}}\n  large: {pass: 50}\n",
        extra="credit: fraction\n",
    )

    effect = _effect(after)

    assert effect.scores == (
        "main weighs its tests differently.",
        "What each test earns: the fraction it reports, from 0 to 1 (was all or nothing, as it "
        "passes or fails).",
        "Results of large shown right away (was once the task closes).",
        "Every result is scored again, and the leaderboards Standings and Live with them.",
        "A change to when entrants see results changes what the leaderboard Live counts of sum.",
    )
    assert effect.results_change is True


def test_what_a_test_earns_names_the_value_by_its_title() -> None:
    before = _task("  main: {each: 100}\n", extra="credit: {relative: memory_kb}\n")
    after = _task("  main: {each: 100}\n", extra="credit: {relative: time_ms}\n")

    effect = effect_of(
        "sum",
        parse_task(before),
        parse_task(after),
        CONTEST,
        changes=(),
        regrades=0,
        graded=True,
        read=NOTHING_READ,
        titles={"time_ms": "Run time", "memory_kb": "Memory used"},
    )

    assert effect.scores[0] == (
        "What each test earns: its run time against the best any entrant reached (was its "
        "memory used against the best any entrant reached)."
    )


def test_a_removed_group_and_the_submission_limits_are_said_and_the_limits_move_no_result() -> None:
    gone = _effect(_task("  main: {each: 100}\n"), changes=("The tests of large were removed.",))
    rate = _effect(
        _task(
            "  main: {each: 100}\n  large: {pass: 50, show: after_close}\n",
            extra="submissions: {rate: {count: 2, per: 3600}}\n",
        )
    )
    limit = _effect(
        _task(
            "  main: {each: 100}\n  large: {pass: 50, show: after_close}\n",
            extra="submissions: {max: 10}\n",
        )
    )

    assert gone.scores[0] == "Points: large is gone (was worth up to 50)."
    assert gone.runs_again == (
        "No submission runs again: there are none yet.",
        "The tests of large were removed.",
    )
    assert rate.scores == ("Submission rate: 2 every hour (was 1 every 30 seconds).",)
    assert rate.results_change is False
    assert limit.scores == ("Submission limit: 10 per entrant (was 50).",)
    assert limit.results_change is False


@pytest.mark.parametrize(
    ("regrades", "said"),
    [
        (1, "1 submission is graded again: the input of test 1 in the group main changed."),
        (12, "12 submissions are graded again: the input of test 1 in the group main changed."),
    ],
)
def test_a_grading_change_counts_what_runs_again_and_counts_the_boards_again(
    regrades: int, said: str
) -> None:
    effect = _effect(
        BEFORE,
        changes=("The input of test 1 in the group main changed.",),
        regrades=regrades,
        graded=False,
    )

    assert (effect.summary, effect.runs_again) == ("Results change.", (said,))
    assert effect.scores == ("The leaderboards Standings and Live are counted again.",)


def test_what_entrants_read_is_the_title_the_statement_and_the_files_beside_it() -> None:
    effect = _effect(
        _task("  main: {each: 100}\n  large: {pass: 50, show: after_close}\n", name="Sum of two"),
        read=ReadChanges(statement=True, public=True),
    )

    assert effect.reading == (
        "The task's title changes to Sum of two (was Sum).",
        "The statement changes.",
        "The pictures and files beside the statement change.",
    )
    assert effect.results_change is False


def test_a_first_publication_lists_each_groups_points_and_the_boards_it_joins() -> None:
    effect = _effect(BEFORE, before=None, graded=False)

    assert effect == SaveEffect(
        results_change=False,
        summary="Nobody's result changes.",
        runs_again=("No submission runs again: this is the task's first publication.",),
        scores=(
            "Points: main is worth up to 100 and large up to 50.",
            "sum now counts on the leaderboard Standings.",
            "sum now counts on the leaderboard Live.",
        ),
        reading=("Entrants read the task from its release.",),
    )
