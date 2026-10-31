"""A value a program of the task works out (`exec`, SIMPLER-CORE.md 14),
beside one a manager enters (`entered`): the task saves, building the
program in its save pipeline; a grading runs the program after the runs
it reads, handing it their runs file through the door, and scores; the
same runs reuse the number stored; a program that faults is retried once
and then waits for staff, never the entrant's verdict; a change to its
block regrades running the program alone, the save saying the cost; a
folder with no `program.yaml` is refused; and an entry outlives a
regrade.
"""

import hashlib
import json
from collections.abc import Callable
from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import select

from forge.db.tables.entrants import Entrant
from forge.domain.grading import GradingStatus
from forge.domain.names import EntrantOwner
from forge.domain.roles import Role
from forge.runtime.setup import Setup
from forge.services import entries, gradings, steps, submissions
from forge.services.task_save import Published
from forge.settings import Settings
from forge.testing import APP_URL, FORGE_URL, FakeClock
from tests.services.conftest import Acme, organiser
from tests.services.test_entries import REVIEWED
from tests.services.test_pipelines import (
    BEFORE_CLOSE,
    ISLANDS,
    LIVE,
    _early,
    _grading,
    _rows,
    _submit,
    _values,
)
from tests.services.test_run_task_saves import _errors
from tests.services.test_run_task_saves import _save as _saved_as
from tests.services.test_steps import (
    Islands,
    Written,
    _batch,
    _bearer,
    _built,
    _download,
    _finish,
    _islands,
    _job,
    _mint,
    _named,
    _outputs,
    _report,
    _row,
    _save,
    _step,
    _to_run,
)

MEAN_YAML = b"reports:\n  mean: {better: higher, at_least: 0, at_most: 1}\n"
MEAN_FILES = {"programs/mean/mean.py": b"print(0.75)\n", "programs/mean/program.yaml": MEAN_YAML}
TEXT = REVIEWED.replace(
    "    total:\n",
    "    mean: {block: exec, program: programs/mean/, language: python, over: {runs: all}}\n"
    "    total:\n",
).replace(
    "    after_close: [st1, st2, total, all]\n", "    after_close: [st1, st2, total, all, mean]\n"
)
"""islands with a reviewer's mark, and the mean score of every run worked
out by a program of the task, shown after the close."""


@pytest.fixture
def settings(migrated_database_url: str) -> Settings:
    return Settings.for_tests(
        database_url=migrated_database_url,
        public_url=APP_URL,
        forge_public_url=FORGE_URL,
        retry_failed=True,
    )


def _reported(mean: object = 0.75, outcome: str = "accepted") -> Callable[..., Written]:
    """The program's run: accepted with `mean` as its report, or ending
    `outcome`; no report at all when `mean` is none."""

    def write(job: Any) -> Written:
        outputs: dict[str, Any] = {"outcome": outcome, "log": "built"}
        files: Written = {}
        if outcome == "accepted":
            outputs |= {"output": {"file": "out/output"}, "time_ms": 3, "memory_kb": 900}
            files["out/output"] = b"done\n"
            if mean is not None:
                outputs["mean"] = mean
        return {"outputs.json": _outputs({"schema_version": 6, "outputs": outputs}), **files}

    return write


async def _checked(setup: Setup, acme: Acme, text: str = TEXT) -> Islands:
    """islands as `text`, published in a running contest, its checker and
    the program built in its save pipeline."""
    islands = await _islands(setup, acme, text=text, contest=LIVE, files=MEAN_FILES)
    [save] = await _rows(setup, "save")
    assert await _to_run(setup, save.id, "save") == [
        ("build-compare", []),
        ("build-mean", ["build-compare"]),
    ]
    await _step(setup, acme, save.id, "save", "build-compare", _built())
    built = await _step(setup, acme, save.id, "save", "build-mean", _built())
    assert built["inputs"]["inputs"]["language"] == "python"
    assert set(built["inputs"]["inputs"]["source"]) == {"folder"}
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()
    return islands


async def _late(setup: Setup, acme: Acme, islands: Islands) -> tuple[Any, Any]:
    """A submission graded early, and its late row with the runs done; the
    entrant's session and the late row."""
    session = await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    assert await _early(setup, acme, early.id) is GradingStatus.DONE
    [late] = await _rows(setup, "late")
    await _step(setup, acme, late.id, "late", "solution", _batch())
    await _step(setup, acme, late.id, "late", "compare", _batch({"r4": "wrong_answer"}, score=True))
    return session, late


async def _owner(setup: Setup) -> EntrantOwner:
    async with setup.unit_of_work() as ctx:
        entrant = await ctx.db.scalar(select(Entrant.id))
    assert entrant is not None
    return EntrantOwner(entrant)


async def _organised(setup: Setup, acme: Acme, islands: Islands) -> dict[str, Any]:
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    seen = await submissions.organised(setup, manager, islands.task, await _owner(setup), 1)
    assert seen.grading is not None
    return _values(seen.grading)


async def test_a_task_with_a_program_and_an_entered_value_saves_grades_and_scores(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme)
    session, late = await _late(setup, acme, islands)
    assert await _to_run(setup, late.id, "late") == [("exec-mean", [])]

    access = await _mint(setup, late.id, "late", "exec-mean")
    job = await _job(setup, late.id, "exec-mean", access)
    given = job["inputs"]["inputs"]
    runs = _download(job, given["files"]["files"]["runs.json"]["file"])
    sha256 = runs["url"].rpartition("/files/")[2]
    served = await steps.step_file(setup, late.id, _named(access), sha256, _bearer(access))
    # The program is compiled from its source in the step, never handed over built.
    assert (given["language"], "program" in given) == ("python", False)
    source = given["source"]["folder"]
    assert f"{source}/mean.py" in {each["path"] for each in job["files"]}
    assert (given["time_limit"], given["memory_limit"]) == (2, 256)
    assert hashlib.sha256(served).hexdigest() == sha256 == runs["sha256"]
    read = json.loads(served)
    assert [(run["id"], run["verdict"], run["passed"]) for run in read["runs"]] == [
        ("r1", "accepted", 1),
        ("r2", "accepted", 1),
        ("r3", "accepted", 1),
        ("r4", "wrong_answer", 0),
    ]
    assert read["runs"][0] == {
        "id": "r1",
        "key": {"answer": "samples/1.ans", "input": "samples/1.in"},
        "passed": 1,
        "reports": {"memory_kb": 1500, "score": 1, "time_ms": 12},
        "verdict": "accepted",
    }
    await _finish(setup, acme, late.id, "exec-mean", access, _reported(0.75)(job))
    assert await _report(setup, late.id, "late") is GradingStatus.DONE

    result = (await _row(setup, late.id)).result
    assert result is not None and result["values"] == {"mean": Fraction(3, 4)}
    assert [run["id"] for run in result["runs"]] == ["r3", "r4"]
    organised = await _organised(setup, acme, islands)
    assert (organised["mean"], organised["review"], organised["total"]) == (
        Fraction(3, 4),
        None,
        40,
    )
    # The mean is shown after the close, and an entered mark no cell counted
    # from the reveal, read as no mark before it.
    seen = _values(await _grading(setup, session, islands))
    assert set(seen) == {"sample_score", "sample_time", "review", "reviewed"}
    assert (seen["review"], seen["reviewed"]) == (None, None)

    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    await entries.enter(setup, manager, islands.task, await _owner(setup), 1, "review", "6")
    await gradings.rejudge(setup, manager, islands.task)
    await setup.settle()

    # The same runs make the same runs file, so the program's number is
    # reused from the store and nothing runs again.
    assert [row.status for row in await _rows(setup, "late") if row.attempt == 2] == [
        GradingStatus.DONE
    ]
    organised = await _organised(setup, acme, islands)
    assert (organised["mean"], organised["review"], organised["reviewed"]) == (
        Fraction(3, 4),
        6,
        7,
    )


async def test_a_program_that_fails_is_retried_once_and_then_waits_for_staff(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme)
    _, late = await _late(setup, acme, islands)

    await _step(setup, acme, late.id, "late", "exec-mean", _reported(outcome="runtime_error"))
    assert await _report(setup, late.id, "late") is GradingStatus.SYSTEM_ERROR
    await setup.settle()
    failed = await _row(setup, late.id)
    assert failed.error == "The step exec-mean faulted: The program of mean ended runtime_error."
    assert failed.result is not None
    assert {run["verdict"] for run in failed.result["runs"]} == {"accepted", "wrong_answer"}

    [_, retried] = await _rows(setup, "late")
    assert (retried.attempt, retried.automatic) == (2, True)
    # The runs are reused, so only the program runs again.
    assert await _to_run(setup, retried.id, "late") == [("exec-mean", [])]
    await _step(setup, acme, retried.id, "late", "exec-mean", _reported(None))
    assert await _report(setup, retried.id, "late") is GradingStatus.SYSTEM_ERROR
    await setup.settle()

    assert len(await _rows(setup, "late")) == 2
    assert (await _row(setup, retried.id)).error == (
        "The step exec-mean faulted: An accepted item's outputs leave out mean."
    )
    assert (await _organised(setup, acme, islands))["mean"] == "waiting"


async def test_a_change_to_a_programs_block_regrades_running_the_program_alone(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme)
    _, late = await _late(setup, acme, islands)
    await _step(setup, acme, late.id, "late", "exec-mean", _reported(0.75))
    assert await _report(setup, late.id, "late") is GradingStatus.DONE

    changed = TEXT.replace("over: {runs: all}}", "over: {runs: all}, time_limit: 3}")
    saved = await _save(setup, acme, islands.task, {"task.yaml": changed.encode()})

    assert saved.effect.runs_again[-1] == (
        "The program of mean works out this value again for 1 submission, in a container each."
    )
    [_, save] = await _rows(setup, "save")
    # The program compiles as it did, so the save has nothing to run.
    assert save.status == GradingStatus.DONE
    await setup.settle()
    [_, rerun] = await _rows(setup, "late")
    assert await _to_run(setup, rerun.id, "late") == [("exec-mean", [])]
    job = await _step(setup, acme, rerun.id, "late", "exec-mean", _reported(0.5))
    assert job["inputs"]["inputs"]["time_limit"] == 3
    assert await _report(setup, rerun.id, "late") is GradingStatus.DONE
    assert (await _organised(setup, acme, islands))["mean"] == Fraction(1, 2)


async def test_a_program_folder_with_no_program_yaml_is_refused_at_the_save(
    setup: Setup, acme: Acme, spring: str
) -> None:
    islands = await _checked(setup, acme)

    found = await _saved_as(
        setup,
        acme,
        islands.task,
        {"task.yaml": TEXT.encode(), "programs/mean/program.yaml": None},
    )

    assert not isinstance(found, Published)
    assert _errors(found) == [
        (
            "scoring.values.mean.program",
            "programs/mean/ holds no program.yaml, which declares the one number the program "
            "reports.",
        )
    ]


async def test_an_early_programs_fault_with_no_retry_by_itself_holds_only_its_value_at_once(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup.settings, "retry_failed", False)
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, EARLY)
    await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    await _step(setup, acme, early.id, "early", "solution", _batch())
    await _step(setup, acme, early.id, "early", "compare", _batch(score=True))
    await _step(setup, acme, early.id, "early", "exec-mean", _reported(outcome="runtime_error"))

    assert await _report(setup, early.id, "early") is GradingStatus.DONE
    await setup.settle()
    assert len(await _rows(setup, "early")) == 1
    [late] = await _rows(setup, "late")
    assert late.early_id == early.id


EARLY = (
    TEXT.replace("over: {runs: all}}", "over: {runs: sample_tests}}")
    .replace("after_close: [st1, st2, total, all, mean]", "after_close: [st1, st2, total, all]")
    .replace("review, reviewed]", "review, reviewed, mean]")
)
"""The mean over the samples alone, shown during the contest, so its
program runs early."""


async def test_an_early_programs_fault_is_retried_once_alone_then_holds_only_its_value(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands = await _checked(setup, acme, EARLY)
    session = await _submit(setup, acme, islands)
    [early] = await _rows(setup, "early")
    await _step(setup, acme, early.id, "early", "solution", _batch())
    await _step(setup, acme, early.id, "early", "compare", _batch(score=True))
    await _step(setup, acme, early.id, "early", "exec-mean", _reported(outcome="runtime_error"))

    # The platform tries the program again by itself before the late row.
    assert await _report(setup, early.id, "early") is GradingStatus.SYSTEM_ERROR
    await setup.settle()
    assert await _rows(setup, "late") == []
    [_, retried] = await _rows(setup, "early")
    assert (retried.attempt, retried.automatic) == (2, True)
    # The runs are reused, so only the program runs again.
    assert await _to_run(setup, retried.id, "early") == [("exec-mean", [])]
    await _step(setup, acme, retried.id, "early", "exec-mean", _reported(outcome="runtime_error"))

    assert await _report(setup, retried.id, "early") is GradingStatus.DONE
    await setup.settle()
    ended = await _row(setup, retried.id)
    # The reason is kept for staff; no retry is made of a row that is done.
    assert ended.error == "The step exec-mean faulted: The program of mean ended runtime_error."
    assert len(await _rows(setup, "early")) == 2
    [late] = await _rows(setup, "late")
    assert late.early_id == retried.id
    await _step(setup, acme, late.id, "late", "solution", _batch())
    await _step(setup, acme, late.id, "late", "compare", _batch(score=True))
    assert await _report(setup, late.id, "late") is GradingStatus.DONE

    organised = await _organised(setup, acme, islands)
    assert (organised["mean"], organised["st1"], organised["total"]) == ("waiting", 1, 100)
    seen = _values(await _grading(setup, session, islands))
    assert (seen["mean"], seen["sample_score"]) == ("waiting", 1)

    # A staff retry runs only the program again.
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    await gradings.retry(setup, manager, retried.id)
    await setup.settle()
    [*_, again] = await _rows(setup, "early")
    assert await _to_run(setup, again.id, "early") == [("exec-mean", [])]
    await _step(setup, acme, again.id, "early", "exec-mean", _reported(0.5))
    assert await _report(setup, again.id, "early") is GradingStatus.DONE
    await setup.settle()
    assert (await _organised(setup, acme, islands))["mean"] == Fraction(1, 2)
