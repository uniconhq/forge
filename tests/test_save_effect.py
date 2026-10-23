"""What a save does, in the words a running contest asks with before a save
and a save that published answers with: the first line, what runs again, the
scores and what entrants read. Results change when a submission is graded
again, or when the task has a graded submission and the save moves what one
earns or shows; the rate alone, the statement and the title move none, and
on a task graded test by test so do its points. A run task's groups and
values are named as they change, a value with what it reads, and what
`show` shows when.
"""

import pytest

from forge.domain.definitions import TaskDefinition, parse_contest, parse_task
from forge.domain.save_effect import ReadChanges, SaveEffect, effect_of
from tests.test_run_task import ISLANDS

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
  standings:
    title: Standings
    columns:
      sum_total: {value: [{block: latest, from: {results: sum}}, total]}
  other_leaderboard:
    title: Other
    columns:
      other_total: {value: [{block: latest, from: {results: other}}, total]}
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


def test_a_classic_tasks_points_credit_and_weights_move_no_result() -> None:
    # A task graded test by test gives a leaderboard nothing and scores nothing
    # (stage 5 section 6, item 123): only when a group shows is said.
    effect = _effect(
        _task(
            "  main: {each: 60, pass: 50, pass_at: 0.5, test_weights: {'1': 2}}\n"
            "  large: {pass: 50, show: always}\n",
            extra="credit: fraction\n",
        )
    )

    assert effect.scores == ("Results of large shown right away (was once the task closes).",)
    assert effect.results_change is True
    assert _effect(_task("  main: {each: 110}\n  large: {pass: 5, show: after_close}\n")) == (
        _effect(BEFORE)
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

    assert gone.scores == ("No score changes.",)
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
def test_a_grading_change_counts_what_runs_again(regrades: int, said: str) -> None:
    effect = _effect(
        BEFORE,
        changes=("The input of test 1 in the group main changed.",),
        regrades=regrades,
        graded=False,
    )

    assert (effect.summary, effect.runs_again) == ("Results change.", (said,))
    assert effect.scores == ("No score changes.",)


def test_a_grading_change_that_regrades_none_of_the_submissions_made_says_so() -> None:
    # Every submission made was cancelled by staff: there are some, and none
    # is graded again.
    effect = effect_of(
        "sum",
        parse_task(BEFORE),
        parse_task(BEFORE),
        CONTEST,
        changes=("The input of test 1 in the group main changed.",),
        regrades=0,
        graded=False,
        submitted=True,
        read=NOTHING_READ,
    )

    assert effect.runs_again == (
        "No submission runs again: none of those made is graded again.",
        "The input of test 1 in the group main changed.",
    )


def test_a_latest_publication_that_no_longer_reads_is_not_taken_for_a_first_one() -> None:
    effect = effect_of(
        "sum",
        None,
        parse_task(BEFORE),
        CONTEST,
        changes=(),
        regrades=0,
        graded=True,
        unread=True,
        read=ReadChanges(statement=True),
    )

    assert effect.runs_again == (
        "No submission runs again: the tests and how they run are the same.",
    )
    assert effect.scores == (
        "The latest publication's settings no longer read, so its scores cannot be compared: "
        "every result is scored as this save says.",
        "Every result is scored again.",
    )
    assert effect.reading == ("The statement changes.",)
    assert effect.results_change is True


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


def test_a_first_publication_of_a_classic_task_changes_no_score() -> None:
    effect = _effect(BEFORE, before=None, graded=False)

    assert effect == SaveEffect(
        results_change=False,
        summary="Nobody's result changes.",
        runs_again=("No submission runs again: this is the task's first publication.",),
        scores=("No score changes.",),
        reading=("Entrants read the task from its release.",),
    )


@pytest.mark.parametrize(
    ("regrades", "said"),
    [
        (
            1,
            "The program of mean works out this value again for 1 submission, in a container each.",
        ),
        (
            9000,
            "The program of mean works out this value again for 9,000 submissions, in a "
            "container each.",
        ),
    ],
)
def test_a_values_program_running_again_says_its_cost(regrades: int, said: str) -> None:
    changes = ("The block of mean changed.",)

    effect = effect_of(
        "sum",
        parse_task(BEFORE),
        parse_task(BEFORE),
        CONTEST,
        changes=changes,
        regrades=regrades,
        graded=True,
        read=NOTHING_READ,
        programs=("mean",),
    )

    assert effect.runs_again[-1] == said
    assert effect_of(
        "sum",
        parse_task(BEFORE),
        parse_task(BEFORE),
        CONTEST,
        changes=(),
        regrades=0,
        graded=True,
        read=NOTHING_READ,
        programs=("mean",),
    ).runs_again == ("No submission runs again: the tests and how they run are the same.",)


# Run tasks


def _run(text: str) -> TaskDefinition:
    return parse_task(text, runs=True)


def _run_effect(after: str, before: str | None = ISLANDS, graded: bool = True) -> SaveEffect:
    return effect_of(
        "sum",
        _run(before) if before is not None else None,
        _run(after),
        CONTEST,
        changes=(),
        regrades=0,
        graded=graded,
        read=NOTHING_READ,
    )


def test_a_run_tasks_values_are_named_as_they_change_with_what_they_read() -> None:
    effect = _run_effect(
        ISLANDS.replace(
            "st2_tests: {input: [tests/3.in, tests/4.in]}", "st2_tests: {input: [tests/4.in]}"
        )
        .replace("of: [40, {value: st1}]", "of: [50, {value: st1}]")
        .replace(
            "    sample_time:",
            "    tries: {block: count, over: {runs: all}}\n    sample_time:",
        )
    )

    assert effect.results_change and effect.summary == "Results change."
    assert effect.scores == (
        "The group st2_tests now holds other runs.",
        "The value tries is new.",
        "The value st2 changes with st2_tests.",
        "The value total is worked out another way.",
        "Every result is scored again, and the leaderboard Standings with it.",
    )


def test_a_value_gone_a_group_gone_and_what_is_shown_when_are_each_said() -> None:
    effect = _run_effect(
        ISLANDS.replace("    st1_tests: {input: [tests/3.in]}\n", "")
        .replace(
            "    st1: {block: min, over: {runs: st1_tests}, of: {run: [st1_tests, score]}}\n", ""
        )
        .replace("of: [40, {value: st1}]", "of: [40, 1]")
        .replace("after_close: [st1, st2, total, all]", "after_close: [st2, all]")
        .replace("now: [sample_score,", "now: [total, sample_score,")
    )

    assert effect.scores == (
        "The group st1_tests is gone.",
        "The value total is worked out another way.",
        "The value st1 is gone.",
        "Entrants see total as soon as a grading ends (was at the reveal).",
        "Entrants no longer see st1 (was at the reveal).",
        "Every result is scored again, and the leaderboard Standings with it.",
    )


def test_a_run_task_moving_only_what_is_shown_scores_nothing_again() -> None:
    effect = _run_effect(
        ISLANDS.replace("after_close: [st1, st2, total, all]", "on_release: [st1, st2, total, all]")
    )
    untouched = _run_effect(ISLANDS)
    ungraded = _run_effect(ISLANDS.replace("[40,", "[30,"), graded=False)
    first = _run_effect(ISLANDS, before=None)

    assert effect.results_change
    assert effect.scores[0] == "Entrants see st1 once results are released (was at the reveal)."
    assert not any("scored again" in line for line in effect.scores)
    assert (untouched.results_change, untouched.scores) == (False, ("No score changes.",))
    assert not ungraded.results_change
    assert first.scores == ("sum now counts on the leaderboard Standings.",)


def test_a_leaderboard_reading_the_task_through_another_leaderboards_column_is_named_too() -> None:
    through = CONTEST.model_copy(
        update={
            "leaderboards": {
                **CONTEST.leaderboards,
                "mirror": CONTEST.leaderboards["standings"].model_copy(
                    update={
                        "title": "Mirror",
                        "columns": {"m": {"column": ["standings", "sum_total"]}},
                    }
                ),
            }
        }
    )

    def effect(after: str, before: str | None = ISLANDS) -> SaveEffect:
        return effect_of(
            "sum",
            _run(before) if before is not None else None,
            _run(after),
            through,
            changes=(),
            regrades=0,
            graded=True,
            read=NOTHING_READ,
        )

    changed = effect(ISLANDS.replace("of: [40, {value: st1}]", "of: [50, {value: st1}]"))
    first = effect(ISLANDS, before=None)

    assert changed.scores[-1] == (
        "Every result is scored again, and the leaderboards Standings and Mirror with them."
    )
    assert first.scores == (
        "sum now counts on the leaderboard Standings.",
        "sum now counts on the leaderboard Mirror.",
    )


def test_a_task_file_shown_at_another_time_is_said_under_what_entrants_read_alone() -> None:
    later = ISLANDS.replace(
        "now: [sample_score, sample_time, sample_tests, samples/]",
        "now: [sample_score, sample_time, sample_tests]",
    ).replace(
        "after_close: [st1, st2, total, all]", "after_close: [st1, st2, total, all, samples/]"
    )
    effect = _run_effect(later)
    gone = _run_effect(later.replace(", samples/]", "]"))

    assert (effect.results_change, effect.summary) == (False, "Nobody's result changes.")
    assert effect.scores == ("No score changes.",)
    assert effect.reading == (
        "Entrants can read samples/ from the reveal (was during the contest).",
    )
    assert gone.reading == ("Entrants can no longer read samples/ (was during the contest).",)


def test_a_file_only_a_check_names_is_never_said_to_be_shown() -> None:
    checked = ISLANDS.replace(
        "submissions:",
        "checks:\n  - submit: {code: checks/good}\n    expect: {verdict: accepted}\nsubmissions:",
    )
    effect = _run_effect(
        checked.replace("samples/]", "samples/, checks/good/]"),
        before=checked,
    )

    assert effect.reading == ("Nothing entrants read changes.",)
    assert effect.scores == ("No score changes.",)
