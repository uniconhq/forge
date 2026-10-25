"""The save that publishes. A valid save writes the organiser's files and the
task's plan, `plans/plan.json`, as one change and publishes exactly that
change, numbered after the last, with a note that reads back. A save another
save landed under is kept as a draft, and so, always, is a save asked to be.
A save inside `plans/` and a manager's change to an admin-only setting are
refused with nothing written. A save that does not check is a draft: its
files written, no publication, and its errors recomputed whenever the task's
state is read. What changed how the task grades is named, and once the
contest has started such a save asks to be confirmed; a change to how it
scores changes nothing that grades, and a published change to how it grades
queues every submission to be graded again. Once the task has a done
grading, a group's `show` neither hides what any graded publication showed
nor is left unsaid on a new group. A task left giving no points may keep
its due.
"""

import asyncio
import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from unittest.mock import ANY

import pytest
from sqlalchemy import func, select, update

from forge.db.tables import Grading
from forge.domain.content import Edit
from forge.domain.contracts import violation
from forge.domain.errors import (
    AdminOnly,
    ConfirmationRequired,
    Conflict,
    Forbidden,
    InvalidInputs,
    ReservedPath,
)
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import ContestId, TaskId
from forge.domain.plan_basics import PLAN_PATH
from forge.domain.roles import Role, RoleGrant, Scope, contest_id_of, task_scope
from forge.domain.run_plan import RunPlan
from forge.domain.run_task import RUNS_PATH
from forge.domain.save_effect import SaveEffect
from forge.domain.submissions import SubmittedInput
from forge.domain.workflow_definition import WORKFLOW_FILE
from forge.domain.workflows import Visibility
from forge.runtime.setup import Setup
from forge.services import gradings, submissions, task_save, tasks, timelines
from forge.services.access import Organiser
from forge.services.task_save import Draft, Published
from forge.testing import CLASSIC, PLACEHOLDER_DIGEST, PRIMITIVES, FakeClock
from tests.services.conftest import Acme, Entered, checked, organiser, upload, write_contest

RUNNING = """\
name: Spring 2026
start: 2026-09-26T10:00:00Z
end: 2026-09-26T15:00:00Z
state: {state}
visibility: everyone
tasks:
  - id: sum
"""

WORKFLOW = CLASSIC[WORKFLOW_FILE]
"""The built-in workflow's `workflow.yaml`."""
ONE_RUN = b"      - [tests/1.in, tests/1.ans]\n"
"""The starter's one run, as its `repeat` lists it."""
SECOND_RUN = ONE_RUN + b"      - [tests/2.in, tests/2.ans]\n"

OLD_CLASSIC = b"""\
name: unicon/classic
version: v1

inputs:
  - id: submission
    type: code
  - id: testcases
    type: file[]

steps:
  - id: compile
    use: unicon/compile@v1
    with:
      source: ${{ inputs.submission }}
      language: ${{ inputs.submission.language }}

outputs:
  outcome: ${{ steps.compile.outcome }}
"""
"""`unicon/classic@v1` as the format before this one wrote it, cut short."""

OLD_COMPILE = b"""\
name: unicon/compile
version: v1
batch: false
limits: {time_ms: 60000, cpu_ms: 60000, memory_mb: 1024, pids: 128, output_mb: 64}
limits_from: {}
inputs:
  source: {type: file}
  language: {type: enum, values: [python, c, cpp, java]}
outputs:
  binary: {type: file, optional: true}
  compile_log: {type: text}
  outcome: {type: outcome}
"""
"""`unicon/compile@v1`'s declaration as the format before this one wrote it."""


async def _edits(acme: Acme, task: TaskId, contents: Mapping[str, bytes | None]) -> dict[str, Edit]:
    """Each file with the token it has now, as an editor would send it."""
    head = await acme.fake.content.list_files(PLATFORM, task)
    return {path: Edit(content, head.tokens.get(path)) for path, content in contents.items()}


def _task_yaml(acme: Acme) -> bytes:
    return acme.fake.state.repos[("acme", "spring.sum.task")].files["task.yaml"]


def _with(acme: Acme, old: bytes, new: bytes) -> dict[str, bytes]:
    """`task.yaml` as it is now, with `old` replaced by `new`."""
    current = _task_yaml(acme)
    assert old in current, old
    return {"task.yaml": current.replace(old, new)}


async def _save(
    setup: Setup,
    acme: Acme,
    task: TaskId,
    contents: Mapping[str, bytes | None],
    who: Organiser | None = None,
    **options: Any,
) -> Published | Draft:
    result = await task_save.save(
        setup, who or acme.ada, task, await _edits(acme, task, contents), **options
    )
    if isinstance(result, Published):
        await checked(setup, task)
    return result


def _written(acme: Acme) -> list[str]:
    return [
        call.operation
        for call in acme.fake.calls
        if call.operation in {"save_files", "write_file", "publish"}
    ]


async def _published(setup: Setup, acme: Acme, task: TaskId) -> Published:
    """The task's first publication, a save of the starter as it is,
    confirmed, since a save asks first once the contest has started.
    """
    result = await _save(setup, acme, task, {"statement.md": b"Add two numbers.\n"}, confirm=True)
    assert isinstance(result, Published), result
    acme.fake.reset_calls()
    return result


async def _workflow(acme: Acme, name: str, content: bytes, owner: str = "unicon") -> None:
    """A public workflow at `v1` with `content` as its `workflow.yaml`, beside
    the built-in workflow's other files."""
    made = await acme.fake.workflows.create_workflow(
        PLATFORM, owner, name, {**CLASSIC, WORKFLOW_FILE: content}, Visibility.PUBLIC
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, made, "v1")


async def test_the_starters_first_save_publishes_its_plan_and_the_plan_keeps_the_contract(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    result = await _save(setup, acme, sum_task, {"statement.md": b"Add two numbers.\n"})

    assert isinstance(result, Published)
    assert (result.number, result.grading_changed, result.changes) == (1, False, ())
    # The save says what it did, in the words a running contest asks with,
    # and that what is shown at once reads runs beside hidden files.
    assert [note.split(" ", 1)[0] for note in result.notes] == ["score", "all"]
    assert result.effect == SaveEffect(
        results_change=False,
        summary="Nobody's result changes.",
        runs_again=("No submission runs again: this is the task's first publication.",),
        scores=("No score changes.",),
        reading=("Entrants read the task from its release.",),
    )
    (saved,) = acme.fake.calls_to("save_files")
    # The platform writes it, with ada as its author (#443).
    assert (saved.identity, saved.arguments["author"]) == (PLATFORM, acme.ada.user.id)
    assert saved.arguments["paths"] == [PLAN_PATH, RUNS_PATH, "statement.md"]
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    head = repo.history[-1]
    assert (head.author_id, head.message) == (7, "Save")
    assert repo.versions == {"published/1": head.version}
    assert sorted(path for path in repo.files if path.startswith("plans/")) == [
        PLAN_PATH,
        RUNS_PATH,
    ]
    document = json.loads(repo.files[PLAN_PATH])
    assert violation(document, "plan") is None
    plan = RunPlan.from_bytes(repo.files[PLAN_PATH])
    assert document["schema_version"] == 6
    assert plan.workflow.ref == "unicon/classic@v3"
    assert [run.id for run in plan.runs] == ["r1"]
    assert sorted(plan.task_files) == ["tests/1.ans", "tests/1.in"]
    reads = acme.fake.calls_to("read_declaration")
    assert {call.arguments["primitive"] for call in reads} == {"compile", "exec"}
    assert all(call.identity == acme.ada.identity for call in reads)
    (published,) = acme.fake.calls_to("publish")
    assert published.identity == PLATFORM
    (listed,) = await task_save.list_publications(setup, acme.ada, sum_task)
    assert (listed.id, listed.number, listed.version) == (result.publication, 1, head.version)
    assert (listed.grading_changed, listed.changes) == (False, ())
    assert set(listed.workflows) == {"unicon/classic"}


async def test_each_publication_is_numbered_after_the_last(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    first = await _save(setup, acme, sum_task, {"statement.md": b"One.\n"})
    second = await _save(setup, acme, sum_task, {"statement.md": b"Two.\n"})

    assert isinstance(first, Published) and isinstance(second, Published)
    assert (first.number, second.number) == (1, 2)


async def test_a_save_that_changes_nothing_since_the_publication_publishes_nothing_new(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    first = await _published(setup, acme, sum_task)

    again = await task_save.save(setup, acme.ada, sum_task, {})

    assert isinstance(again, Published)
    assert (again.publication, again.number) == (first.publication, 1)
    assert _written(acme) == []
    assert again.effect == SaveEffect(
        results_change=False,
        summary="Nobody's result changes.",
        runs_again=("No submission runs again: the tests and how they run are the same.",),
        scores=("No score changes.",),
        reading=("Nothing entrants read changes.",),
    )


async def test_a_stale_token_is_a_conflict_and_nothing_is_written(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    stale = await _edits(acme, sum_task, {"statement.md": b"One.\n"})
    await _save(setup, acme, sum_task, {"statement.md": b"Other.\n"})
    acme.fake.reset_calls()

    with pytest.raises(Conflict):
        await task_save.save(setup, acme.ada, sum_task, stale)
    assert acme.fake.calls_to("publish") == []
    assert len(acme.fake.state.repos[("acme", "spring.sum.task")].versions) == 1


async def test_a_save_another_landed_under_is_kept_as_a_draft_and_not_published(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    save_files = acme.fake.content.save_files

    async def after_another(*args: Any, **kwargs: Any) -> Any:
        acme.fake.state.commit(repo, {"task.yaml": b"name: [\n"}, "Theirs", 8)
        return await save_files(*args, **kwargs)

    monkeypatch.setattr(acme.fake.content, "save_files", after_another)
    result = await _save(setup, acme, sum_task, {"statement.md": b"Add two numbers.\n"})

    assert isinstance(result, Draft)
    assert result.version == repo.head
    assert [problem["path"] for problem in result.errors] == [""]
    assert acme.fake.calls_to("publish") == []
    assert repo.versions == {}


async def test_a_plan_file_the_compiler_no_longer_writes_is_removed_in_the_same_change(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    acme.fake.state.commit(repo, {"plans/default.json": b"{}\n"}, "An old plan", 7)

    result = await _save(setup, acme, sum_task, {"statement.md": b"Add two numbers.\n"})

    assert isinstance(result, Published)
    (saved,) = acme.fake.calls_to("save_files")
    assert saved.arguments["paths"] == [
        "plans/default.json",
        PLAN_PATH,
        RUNS_PATH,
        "statement.md",
    ]
    assert sorted(path for path in repo.files if path.startswith("plans/")) == [
        PLAN_PATH,
        RUNS_PATH,
    ]


async def test_a_save_inside_plans_is_refused_and_writes_nothing(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    with pytest.raises(ReservedPath) as refused:
        await _save(
            setup,
            acme,
            sum_task,
            {"plans/plan.json": b"{}", "statement.md": b"x", "plans": b"x"},
        )

    assert refused.value.code == "reserved_path"
    assert refused.value.extra["paths"] == ["plans", "plans/plan.json"]
    assert _written(acme) == []


@pytest.fixture
async def manager(setup: Setup, acme: Acme, sum_task: TaskId) -> Organiser:
    """bob, a manager of the task alone."""
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring", "sum"), Role.MANAGER)
    found = await organiser(setup, acme.fake, 8, Scope("acme", "spring", "sum"), Role.MANAGER)
    acme.fake.reset_calls()
    return found


@pytest.mark.parametrize(
    ("contents", "keys"),
    [
        ({"statement.md": b"Mine.\n"}, ["statement.md"]),
        ({"task.yaml": (b'name: "Sum of Two"', b'name: "Mine"')}, ["name"]),
        ({"task.yaml": (b"scoring:", b"submissions: {max: 5}\nscoring:")}, ["submissions"]),
        (
            {
                "task.yaml": (
                    b"scoring:",
                    b"submissions:\n  rate: {count: 2, per: 30}\nscoring:",
                ),
                "statement.md": b"Mine.\n",
            },
            ["submissions", "statement.md"],
        ),
    ],
    ids=["statement", "name", "submissions", "both"],
)
async def test_a_managers_admin_only_change_is_refused_naming_it_and_an_admins_is_not(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    manager: Organiser,
    contents: dict[str, Any],
    keys: list[str],
) -> None:
    task_yaml = _task_yaml(acme)
    files = {
        path: task_yaml.replace(*value) if isinstance(value, tuple) else value
        for path, value in contents.items()
    }

    with pytest.raises(AdminOnly) as refused:
        await _save(setup, acme, sum_task, files, manager)

    assert refused.value.extra["keys"] == keys
    assert _written(acme) == []
    assert isinstance(await _save(setup, acme, sum_task, files), Published)


async def test_a_managers_save_of_everything_else_publishes(
    setup: Setup, acme: Acme, sum_task: TaskId, manager: Organiser
) -> None:
    task_yaml = _task_yaml(acme)
    statement = acme.fake.state.repos[("acme", "spring.sum.task")].files["statement.md"]

    result = await _save(
        setup,
        acme,
        sum_task,
        {
            "task.yaml": task_yaml.replace(b"time_limit: 2", b"time_limit: 3").replace(
                b"now: [score, all, public/]", b"now: [score, public/]"
            ),
            "statement.md": statement,
            "tests/2.in": b"2 2\n",
            "tests/2.ans": b"4\n",
            "notes.txt": b"Read me.\n",
        },
        manager,
    )

    assert isinstance(result, Published), result
    (saved,) = acme.fake.calls_to("save_files")
    assert (saved.identity, saved.arguments["author"]) == (PLATFORM, manager.user.id)


async def test_a_save_needs_the_manager_role_at_the_task(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring"), Role.OBSERVER)
    bob = await organiser(setup, acme.fake, 8, Scope("acme", "spring", "sum"))

    with pytest.raises(Forbidden, match="manager role at acme/spring/sum"):
        await _save(setup, acme, sum_task, {"statement.md": b"x"}, bob)
    assert await task_save.list_publications(setup, bob, sum_task) == ()


async def test_a_run_naming_files_the_task_lacks_is_a_draft_and_the_state_recomputes_its_errors(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    published = await _published(setup, acme, sum_task)
    broken = _with(acme, ONE_RUN, SECOND_RUN)

    draft = await _save(setup, acme, sum_task, broken)

    assert isinstance(draft, Draft)
    assert draft.errors == (
        {
            "path": "repeat[0].tuples[1][0]",
            "message": "The task has no file tests/2.in. Add it, or give another file.",
        },
        {
            "path": "repeat[0].tuples[1][1]",
            "message": "The task has no file tests/2.ans. Add it, or give another file.",
        },
    )
    assert draft.held_back == ()
    assert _written(acme) == ["save_files"]
    (saved,) = acme.fake.calls_to("save_files")
    assert saved.arguments["paths"] == ["task.yaml"]
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    assert repo.files["task.yaml"] == broken["task.yaml"]
    assert repo.head == draft.version
    assert len(repo.versions) == 1

    state = await tasks.state(setup, acme.ada, sum_task)
    assert (state.draft, state.head, state.errors) == (True, draft.version, draft.errors)
    assert state.latest is not None
    assert state.latest.id == published.publication

    fixed = await _save(setup, acme, sum_task, SECOND_TEST)
    assert isinstance(fixed, Published), fixed
    assert fixed.number == 2
    later = await tasks.state(setup, acme.ada, sum_task)
    assert (later.draft, later.errors) == (False, ())


SECOND_TEST = {"tests/2.in": b"1 1\n", "tests/2.ans": b"2\n"}


async def test_removing_a_file_a_run_names_is_a_draft_naming_it(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)

    draft = await _save(setup, acme, sum_task, {"tests/1.ans": None})

    assert isinstance(draft, Draft)
    assert [(error["path"], error["message"]) for error in draft.errors] == [
        (
            "repeat[0].tuples[0][1]",
            "The task has no file tests/1.ans. Add it, or give another file.",
        )
    ]
    assert acme.fake.calls_to("publish") == []


async def test_a_removal_with_a_stale_token_is_a_conflict_and_nothing_is_written(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)
    await _save(setup, acme, sum_task, {"notes/hint.txt": b"One.\n"})
    stale = await _edits(acme, sum_task, {"notes/hint.txt": None})
    await _save(setup, acme, sum_task, {"notes/hint.txt": b"Two.\n"})
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    head = repo.head
    acme.fake.reset_calls()

    with pytest.raises(Conflict):
        await task_save.save(setup, acme.ada, sum_task, stale)

    assert acme.fake.calls_to("publish") == []
    assert (repo.head, repo.files["notes/hint.txt"]) == (head, b"Two.\n")


async def test_a_removal_of_a_file_that_is_not_there_is_a_conflict(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)
    token = (await acme.fake.content.list_files(PLATFORM, sum_task)).tokens["statement.md"]
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    head = repo.head

    with pytest.raises(Conflict):
        await task_save.save(setup, acme.ada, sum_task, {"notes/gone.txt": Edit(None, token)})

    assert acme.fake.calls_to("publish") == []
    assert repo.head == head


async def test_a_removal_without_its_token_is_refused_naming_it(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    with pytest.raises(InvalidInputs) as refused:
        await task_save.save(
            setup,
            acme.ada,
            sum_task,
            {"tests/1.in": Edit(None), "statement.md": Edit(b"x\n")},
        )

    assert refused.value.extra["errors"] == [
        {"input": "tests/1.in", "message": "Reload the files, then remove this one again."}
    ]
    assert _written(acme) == []


async def test_a_removal_inside_plans_is_refused_and_writes_nothing(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)

    with pytest.raises(ReservedPath) as refused:
        await _save(setup, acme, sum_task, {PLAN_PATH: None})

    assert refused.value.extra["paths"] == [PLAN_PATH]
    assert _written(acme) == []


@pytest.mark.parametrize(
    ("removed", "keys"),
    [
        ("statement.md", ["statement.md"]),
        ("task.yaml", ["name"]),
    ],
    ids=["statement", "task-yaml"],
)
async def test_removing_an_admins_file_is_the_admins_alone(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    manager: Organiser,
    removed: str,
    keys: list[str],
) -> None:
    with pytest.raises(AdminOnly) as refused:
        await _save(setup, acme, sum_task, {removed: None}, manager)

    assert refused.value.extra["keys"] == keys
    assert _written(acme) == []
    assert isinstance(await _save(setup, acme, sum_task, {removed: None}), Published | Draft)
    assert removed not in acme.fake.state.repos[("acme", "spring.sum.task")].files


async def test_a_manager_removes_tests_and_public_files(
    setup: Setup, acme: Acme, sum_task: TaskId, manager: Organiser
) -> None:
    await _save(setup, acme, sum_task, {**SECOND_TEST, "notes/hint.txt": b"Add.\n"})
    acme.fake.reset_calls()

    result = await _save(
        setup, acme, sum_task, {**dict.fromkeys(SECOND_TEST), "notes/hint.txt": None}, manager
    )

    assert isinstance(result, Published), result
    (saved,) = acme.fake.calls_to("save_files")
    assert (saved.identity, saved.arguments["author"]) == (PLATFORM, manager.user.id)
    files = acme.fake.state.repos[("acme", "spring.sum.task")].files
    assert "notes/hint.txt" not in files and "tests/2.in" not in files


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ((b"workflow: unicon/classic@v3", b"workflow: unicon/classic@v9"), "unicon/classic@v9"),
        ((b"workflow: unicon/classic@v3", b"workflow: bob/mine@v1"), "bob/mine@v1"),
    ],
    ids=["no-such-version", "not-shared"],
)
async def test_a_workflow_the_organiser_cannot_read_is_an_error_at_its_line_naming_it(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    change: tuple[bytes, bytes],
    named: str,
) -> None:
    bob = AsUser(8, acme.fake.mint(8))
    mine = await acme.fake.workflows.create_workflow(
        bob, "bob", "mine", CLASSIC, Visibility.PRIVATE
    )
    await acme.fake.workflows.create_workflow_version(bob, mine, "v1")

    draft = await _save(setup, acme, sum_task, _with(acme, *change))

    assert isinstance(draft, Draft)
    (error,) = draft.errors
    assert error["path"] == "workflow"
    assert "Unicon cannot open the workflow" in error["message"]
    assert named in error["message"]
    assert acme.fake.calls_to("publish") == []
    reads = acme.fake.calls_to("workflow_version")
    assert reads and {call.identity for call in reads} == {acme.ada.identity}


async def test_a_workflow_in_an_old_format_is_refused_at_its_line_for_its_owner_to_retag(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _workflow(acme, "old", OLD_CLASSIC)

    draft = await _save(
        setup,
        acme,
        sum_task,
        _with(acme, b"workflow: unicon/classic@v3", b"workflow: unicon/old@v1"),
    )

    assert isinstance(draft, Draft)
    (error,) = draft.errors
    assert error["path"] == "workflow"
    assert error["message"].startswith(
        "The workflow unicon/old@v1 is written in an older form that Unicon no longer reads, so "
        "its owner needs to make a new version of it. There are "
    )
    assert acme.fake.calls_to("publish") == []


async def test_a_workflow_that_does_not_check_as_a_version_is_refused_at_its_line(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _workflow(
        acme, "broken", WORKFLOW.replace(b"steps.compile.program", b"steps.compile.nothing")
    )

    draft = await _save(
        setup,
        acme,
        sum_task,
        _with(acme, b"workflow: unicon/classic@v3", b"workflow: unicon/broken@v1"),
    )

    assert isinstance(draft, Draft)
    assert draft.errors
    assert {error["path"] for error in draft.errors} == {"workflow"}
    assert all(
        error["message"].startswith("The workflow unicon/broken@v1 has a problem (")
        for error in draft.errors
    )


@pytest.mark.parametrize(
    ("use", "said"),
    [
        (
            b"unicon/compile@v7",
            "In the workflow unicon/custom@v1, the step compile cannot run: Unicon cannot open "
            "unicon/compile@v7: there is no such primitive or workflow at that version, or it is "
            "not shared with you.",
        ),
    ],
    ids=["no-such-version"],
)
async def test_a_step_whose_use_is_no_primitive_the_organiser_reads_is_an_error_naming_it(
    setup: Setup, acme: Acme, sum_task: TaskId, use: bytes, said: str
) -> None:
    await _workflow(acme, "custom", WORKFLOW.replace(b"use: unicon/compile@v3", b"use: " + use, 1))

    draft = await _save(
        setup,
        acme,
        sum_task,
        _with(acme, b"workflow: unicon/classic@v3", b"workflow: unicon/custom@v1"),
    )

    assert isinstance(draft, Draft)
    assert [(error["path"], error["message"]) for error in draft.errors] == [("workflow", said)]
    reads = acme.fake.calls_to("read_declaration")
    assert reads and {call.identity for call in reads} == {acme.ada.identity}
    assert acme.fake.calls_to("publish") == []


async def test_a_primitive_in_an_old_format_is_refused_naming_it(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    acme.fake.primitives.add("odd", {"v1": OLD_COMPILE, "v2": PRIMITIVES["unicon/compile@v3"]})
    await _workflow(
        acme, "custom", WORKFLOW.replace(b"use: unicon/compile@v3", b"use: unicon/odd@v1", 1)
    )

    draft = await _save(
        setup,
        acme,
        sum_task,
        _with(acme, b"workflow: unicon/classic@v3", b"workflow: unicon/custom@v1"),
    )

    assert isinstance(draft, Draft)
    (error,) = draft.errors
    assert error["path"] == "workflow"
    assert error["message"].startswith(
        "In the workflow unicon/custom@v1, the step compile cannot run: The primitive "
        "unicon/odd@v1 is written in an older form that Unicon no longer reads. Use a later "
        "version of it. There are "
    )


async def test_an_invalid_task_yaml_and_an_uncovered_input_are_drafts_with_their_paths(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    invalid = _with(acme, b"time_limit: 2", b"time_limit: x")
    uncovered = _with(acme, b"  memory_limit: 256\n", b"")
    given = _with(acme, b"  code: {entrant: {label: Your solution}}", b"  code: main.py")
    unknown = _with(acme, b"  time_limit: 2\n", b"  time_limit: 2\n  speed: 3\n")

    drafts = [
        await _save(setup, acme, sum_task, files) for files in (invalid, uncovered, given, unknown)
    ]

    paths = [
        [error["path"] for error in draft.errors] for draft in drafts if isinstance(draft, Draft)
    ]
    assert paths == [
        ["inputs.time_limit"],
        ["inputs.memory_limit"],
        ["inputs.code"],
        ["inputs.speed"],
    ]
    assert acme.fake.calls_to("publish") == []


async def test_scoring_changes_never_change_grading_and_a_test_or_a_limit_does(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)

    text = await _save(setup, acme, sum_task, {"statement.md": b"Clearer.\n"})
    rules = await _save(
        setup, acme, sum_task, _with(acme, b"now: [score, all, public/]", b"now: [score, public/]")
    )
    caps = await _save(
        setup,
        acme,
        sum_task,
        _with(acme, b"scoring:", b"submissions: {max: 9, rate: {count: 2, per: 60}}\nscoring:"),
    )
    public = await _save(setup, acme, sum_task, {"notes/hint.txt": b"Add.\n"})
    limit = await _save(setup, acme, sum_task, _with(acme, b"time_limit: 2", b"time_limit: 3"))
    edited = await _save(setup, acme, sum_task, {"tests/1.in": b"2 3\n"})
    same = await _save(setup, acme, sum_task, {"tests/1.in": b"2 3\n"})
    added = await _save(setup, acme, sum_task, {**SECOND_TEST, **_with(acme, ONE_RUN, SECOND_RUN)})

    results = [
        result
        for result in (text, rules, caps, public, limit, edited, same, added)
        if isinstance(result, Published)
    ]
    added_run = (
        "The run r2 was added.",
        "The file tests/2.ans was added.",
        "The file tests/2.in was added.",
    )
    assert [(result.grading_changed, result.changes) for result in results] == [
        (False, ()),
        (False, ()),
        (False, ()),
        (False, ()),
        (True, ("The inputs of the step solution changed.",)),
        (True, ("The file tests/1.in changed.",)),
        (False, ()),
        (True, added_run),
    ]
    history = await task_save.list_publications(setup, acme.ada, sum_task)
    assert [entry.grading_changed for entry in history] == [
        False,
        False,
        False,
        False,
        False,
        True,
        True,
        False,
        True,
    ]
    assert history[5].changes == ("The inputs of the step solution changed.",)


@pytest.fixture
async def running(setup: Setup, acme: Acme, sum_task: TaskId) -> TaskId:
    """The task published once, in a contest that has started."""
    await write_contest(acme.fake, RUNNING.format(state="published"))
    await _published(setup, acme, sum_task)
    return sum_task


@pytest.mark.parametrize(
    ("change", "listed"),
    [
        ((b"time_limit: 2", b"time_limit: 1"), ["The inputs of the step solution changed."]),
        (None, ["The file tests/1.in changed."]),
    ],
    ids=["limit", "test"],
)
async def test_a_grading_change_once_the_contest_has_started_asks_first_and_writes_nothing(
    setup: Setup,
    acme: Acme,
    running: TaskId,
    change: tuple[bytes, bytes] | None,
    listed: list[str],
) -> None:
    files = _with(acme, *change) if change else {"tests/1.in": b"5 5\n"}

    with pytest.raises(ConfirmationRequired) as asked:
        await _save(setup, acme, running, files)

    assert asked.value.code == "confirmation_required"
    assert asked.value.extra == {
        "asked": ANY,
        "changes": listed,
        "regrades": 0,
        "results_change": False,
        "summary": "Nobody's result changes.",
        "runs_again": ["No submission runs again: there are none yet.", *listed],
        "scores": ["No score changes."],
        "reading": ["Nothing entrants read changes."],
    }
    assert _written(acme) == []
    confirmed = await _save(setup, acme, running, files, confirm=True)
    assert isinstance(confirmed, Published)
    assert (confirmed.number, confirmed.grading_changed, list(confirmed.changes)) == (
        2,
        True,
        listed,
    )


async def test_a_confirmation_stands_only_for_the_files_and_publication_it_was_asked_about(
    setup: Setup, acme: Acme, running: TaskId
) -> None:
    files = {"statement.md": b"Typo fixed.\n"}
    with pytest.raises(ConfirmationRequired) as first:
        await _save(setup, acme, running, files)
    await _save(setup, acme, running, {"tests/1.in": b"5 5\n"}, keep_as_draft=True)
    acme.fake.reset_calls()

    with pytest.raises(ConfirmationRequired) as again:
        await _save(setup, acme, running, files, confirm=True, asked=first.value.extra["asked"])

    assert again.value.extra["changes"] == ["The file tests/1.in changed."]
    assert again.value.extra["asked"] != first.value.extra["asked"]
    assert _written(acme) == []
    confirmed = await _save(
        setup, acme, running, files, confirm=True, asked=again.value.extra["asked"]
    )
    assert isinstance(confirmed, Published)
    assert (confirmed.number, confirmed.grading_changed) == (2, True)


@pytest.mark.parametrize(
    ("files", "scores", "reading"),
    [
        (
            (b"scoring:", b"submissions: {max: 3, rate: {count: 2, per: 600}}\nscoring:"),
            [
                "Submission limit: 3 per entrant (was 50).",
                "Submission rate: 2 every 10 minutes (was 1 every 30 seconds).",
            ],
            ["Nothing entrants read changes."],
        ),
        (
            {"statement.md": b"Typo fixed.\n"},
            ["No score changes."],
            ["The statement changes."],
        ),
    ],
    ids=["limits", "statement"],
)
async def test_every_save_that_publishes_once_the_contest_has_started_asks_first(
    setup: Setup,
    acme: Acme,
    running: TaskId,
    files: tuple[bytes, bytes] | dict[str, bytes],
    scores: list[str],
    reading: list[str],
) -> None:
    saved = files if isinstance(files, dict) else _with(acme, *files)

    with pytest.raises(ConfirmationRequired) as asked:
        await _save(setup, acme, running, saved)

    assert asked.value.extra == {
        "asked": ANY,
        "changes": [],
        "regrades": 0,
        "results_change": False,
        "summary": "Nobody's result changes.",
        "runs_again": ["No submission runs again: the tests and how they run are the same."],
        "scores": scores,
        "reading": reading,
    }
    assert _written(acme) == []
    confirmed = await _save(setup, acme, running, saved, confirm=True)
    assert isinstance(confirmed, Published), confirmed
    assert (confirmed.grading_changed, confirmed.changes) == (False, ())
    assert (list(confirmed.effect.scores), list(confirmed.effect.reading)) == (scores, reading)


async def test_a_save_of_what_is_published_asks_nothing_and_a_draft_save_asks_nothing(
    setup: Setup, acme: Acme, running: TaskId
) -> None:
    same = await _save(setup, acme, running, {"statement.md": b"Add two numbers.\n"})
    kept = await _save(setup, acme, running, {"statement.md": b"Later.\n"}, keep_as_draft=True)
    back = await _save(setup, acme, running, {"statement.md": b"Add two numbers.\n"})

    assert isinstance(same, Published), same
    assert same.effect.reading == ("Nothing entrants read changes.",)
    assert isinstance(kept, Draft)
    # Saved back to what the latest publication holds: nothing new, so
    # nothing asks, and what it publishes changes nothing.
    assert isinstance(back, Published), back
    assert back.effect.reading == ("Nothing entrants read changes.",)


async def test_a_grading_change_kept_as_a_draft_is_written_and_not_published(
    setup: Setup, acme: Acme, running: TaskId
) -> None:
    kept = await _save(
        setup, acme, running, _with(acme, b"time_limit: 2", b"time_limit: 1"), keep_as_draft=True
    )

    assert isinstance(kept, Draft)
    assert (kept.errors, kept.held_back) == ((), ("The inputs of the step solution changed.",))
    assert _written(acme) == ["save_files"]
    (saved,) = acme.fake.calls_to("save_files")
    assert saved.arguments["paths"] == ["task.yaml"]
    state = await tasks.state(setup, acme.ada, running)
    assert (state.draft, state.errors) == (True, ())
    published = await task_save.save(setup, acme.ada, running, {}, confirm=True)
    assert isinstance(published, Published)
    assert published.changes == ("The inputs of the step solution changed.",)


@pytest.mark.parametrize("confirm", [False, True], ids=["alone", "with-confirm"])
async def test_a_save_kept_as_a_draft_never_publishes_whatever_else_holds(
    setup: Setup, acme: Acme, sum_task: TaskId, confirm: bool
) -> None:
    first = await _save(
        setup, acme, sum_task, {"statement.md": b"New.\n"}, keep_as_draft=True, confirm=confirm
    )
    await _published(setup, acme, sum_task)
    acme.fake.reset_calls()
    later = await _save(
        setup, acme, sum_task, {"statement.md": b"Newer.\n"}, keep_as_draft=True, confirm=confirm
    )

    assert isinstance(first, Draft) and isinstance(later, Draft)
    assert (first.errors, first.held_back) == ((), ())
    assert (later.errors, later.held_back) == ((), ())
    assert _written(acme) == ["save_files"]
    assert [
        entry.number for entry in await task_save.list_publications(setup, acme.ada, sum_task)
    ] == [1]
    state = await tasks.state(setup, acme.ada, sum_task)
    assert (state.draft, state.errors) == (True, ())


@pytest.mark.parametrize("state", ["draft", "archived"])
async def test_outside_a_started_contest_a_grading_change_publishes_without_asking(
    setup: Setup, acme: Acme, sum_task: TaskId, state: str
) -> None:
    await write_contest(acme.fake, RUNNING.format(state=state))
    await _published(setup, acme, sum_task)

    result = await _save(setup, acme, sum_task, _with(acme, b"time_limit: 2", b"time_limit: 1"))

    assert isinstance(result, Published)
    assert result.grading_changed is True
    contest_reads = [
        call.identity
        for call in acme.fake.calls_to("read_file")
        if call.arguments["place"] == ContestId("acme/spring")
    ]
    assert PLATFORM in contest_reads
    assert set(contest_reads) == {PLATFORM}


async def _graded(setup: Setup, acme: Acme, entered: Entered, key: str = "key-0001-aaaa") -> None:
    """bob's submission to the task, its grading done."""
    made = await upload(setup, acme.fake, entered.session, entered.task, b"print(3)\n")
    await submissions.submit(
        setup,
        entered.session,
        entered.task,
        {
            "code": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key=key,
    )
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading).where(Grading.pipeline != "save").values(status=GradingStatus.DONE)
        )
    acme.fake.reset_calls()


async def _attempts(setup: Setup) -> list[tuple[int, int, str, GradingStatus]]:
    async with setup.unit_of_work() as ctx:
        rows = (
            await ctx.db.execute(
                select(Grading)
                .where(Grading.pipeline != "save")
                .order_by(Grading.submission_number, Grading.attempt)
            )
        ).scalars()
        return [
            (row.submission_number, row.attempt, row.publication_id, GradingStatus(row.status))
            for row in rows
        ]


async def test_the_confirmation_counts_the_submissions_the_save_regrades_and_no_cancelled_one(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _graded(setup, acme, entered)
    clock.advance(timedelta(minutes=1))
    await _graded(setup, acme, entered, key="key-0002-bbbb")
    clock.advance(timedelta(minutes=1))
    await _graded(setup, acme, entered, key="key-0003-cccc")
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading)
            .where(Grading.submission_number == 1)
            .values(status=GradingStatus.CANCELLED, cancel_reason="The machine broke.")
        )
    change = _with(acme, b"time_limit: 2", b"time_limit: 1")

    with pytest.raises(ConfirmationRequired) as asked:
        await _save(setup, acme, entered.task, change)
    confirmed = await _save(setup, acme, entered.task, change, confirm=True)

    assert asked.value.extra == {
        "asked": ANY,
        "changes": ["The inputs of the step solution changed."],
        "regrades": 2,
        "results_change": True,
        "summary": "Results change.",
        "runs_again": [
            "Once the save's check passes, 2 submissions are graded again, running solution and "
            "compare again: the inputs of the step solution changed."
        ],
        "scores": ["No score changes."],
        "reading": ["Nothing entrants read changes."],
    }
    assert isinstance(confirmed, Published)
    assert confirmed.effect.runs_again == (
        "Once the save's check passes, 2 submissions are graded again, running solution and "
        "compare again: the inputs of the step solution changed.",
    )
    # Nothing is graded again until the save's check passes and its
    # publication takes over, which grades the two again.
    assert (confirmed.regraded, confirmed.regrade, confirmed.check) == (0, None, "checking")
    async with setup.unit_of_work() as ctx:
        queued = await ctx.db.scalar(
            select(func.count()).select_from(Grading).where(Grading.regrade_id.is_not(None))
        )
    assert queued == 2


async def test_a_grading_change_with_every_submission_cancelled_says_none_is_graded_again(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _graded(setup, acme, entered)
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading)
            .where(Grading.pipeline != "save")
            .values(status=GradingStatus.CANCELLED, cancel_reason="The machine broke.")
        )

    with pytest.raises(ConfirmationRequired) as asked:
        await _save(setup, acme, entered.task, _with(acme, b"time_limit: 2", b"time_limit: 1"))

    assert asked.value.extra["runs_again"] == [
        "No submission runs again: none of those made is graded again.",
        "The inputs of the step solution changed.",
    ]


async def test_a_latest_publication_whose_settings_no_longer_read_is_not_called_the_first(
    setup: Setup, acme: Acme, running: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A publication's settings stop reading when a later forge reads them
    # more strictly than the one that published them.
    async def unread(*_: object) -> None:
        return None

    monkeypatch.setattr(task_save.sources, "_definition_of", unread)

    with pytest.raises(ConfirmationRequired) as asked:
        await _save(setup, acme, running, {"statement.md": b"Typo fixed.\n"})

    assert (asked.value.extra["runs_again"], asked.value.extra["scores"][0]) == (
        ["No submission runs again: the tests and how they run are the same."],
        "The latest publication's settings no longer read, so its scores cannot be compared: "
        "every result is scored as this save says.",
    )


async def test_a_save_that_picks_up_a_moved_image_names_it_beside_the_regrade_count(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _graded(setup, acme, entered)
    repo = acme.fake.state.repos[("unicon", "exec.primitive")]
    declaration = acme.fake.state.version_files(repo, "v1")["primitive.yaml"]
    moved = declaration.replace(PLACEHOLDER_DIGEST.encode(), b"sha256:" + b"7" * 64)
    assert moved != declaration
    acme.fake.state.commit(repo, {"primitive.yaml": moved}, "Release 2.0.1", None)
    repo.versions["v1"] = repo.head
    statement = {"statement.md": b"Add two numbers, now said more plainly.\n"}

    with pytest.raises(ConfirmationRequired) as asked:
        await _save(setup, acme, entered.task, statement)

    assert asked.value.extra == {
        "asked": ANY,
        "changes": ["A release updated exec@v1 in place."],
        "regrades": 1,
        "results_change": True,
        "summary": "Results change.",
        "runs_again": [
            "Once the save's check passes, 1 submission is graded again, running "
            "solution and compare again: a release updated exec@v1 in place."
        ],
        "scores": ["No score changes."],
        "reading": ["The statement changes."],
    }


TOTAL = b"values:\n    total: {block: count, over: {runs: all}}\n"


async def test_a_published_grading_change_regrades_every_submission_and_a_points_one_none(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _graded(setup, acme, entered)
    (graded,) = await _attempts(setup)

    points = await _save(setup, acme, entered.task, _with(acme, b"values:\n", TOTAL), confirm=True)
    unchanged = await _attempts(setup)
    grading = await _save(
        setup, acme, entered.task, _with(acme, b"time_limit: 2", b"time_limit: 1"), confirm=True
    )

    assert isinstance(points, Published) and isinstance(grading, Published)
    assert unchanged == [graded]
    # Nothing runs again, though every result is scored as the save says.
    assert (points.effect.results_change, points.effect.runs_again) == (
        True,
        ("No submission runs again: the tests and how they run are the same.",),
    )
    first, again = await _attempts(setup)
    assert first == graded
    assert again[:3] == (1, 2, grading.publication)
    assert again[3] in (GradingStatus.QUEUED, GradingStatus.DISPATCHED)


class _Stopped(Exception):
    """The process stopping in the middle of a save."""


async def test_a_grading_change_whose_take_over_stopped_before_its_regrade_is_regraded_once(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The save's check passed, but its take-over stopped before the
    regrade: the take-over is undone whole, and the next one grades the
    submissions again, once.
    """
    await _graded(setup, acme, entered)
    (graded,) = await _attempts(setup)

    async def stopped(*_args: Any, **_kwargs: Any) -> None:
        raise _Stopped

    monkeypatch.setattr(gradings, "regrade", stopped)
    with pytest.raises(_Stopped):
        await _save(
            setup, acme, entered.task, _with(acme, b"time_limit: 2", b"time_limit: 1"), confirm=True
        )
    monkeypatch.undo()
    assert await _attempts(setup) == [graded]

    await checked(setup, entered.task)
    await checked(setup, entered.task)

    first, regraded = await _attempts(setup)
    assert first == graded
    assert regraded[:2] == (1, 2)


DUE = RUNNING + "    due: 2026-09-26T14:00:00Z\n"


async def test_a_task_left_giving_no_points_may_keep_its_due(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)
    await write_contest(acme.fake, DUE.format(state="draft"))

    current = _task_yaml(acme)
    no_points = current[: current.index(b"scoring:")] + b"scoring: {}\n"
    result = await _save(setup, acme, sum_task, {"task.yaml": no_points})

    # Any task may have a due (stage 5, phase 5.7).
    assert isinstance(result, Published), result


async def test_the_task_form_reads_the_inputs_its_workflow_declares(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    form = await task_save.workflow_form(setup, acme.ada, sum_task)

    assert (form.workflow, form.problem, form.text_values) == ("unicon/classic@v3", None, ())
    assert [(field.id, field.type) for field in form.inputs] == [
        ("code", "folder"),
        ("language", "enum"),
        ("input", "file"),
        ("answer", "file"),
        ("time_limit", "number"),
        ("memory_limit", "number"),
    ]
    assert form.inputs[1].options == ("c", "cpp", "java", "python")
    assert [(field.title, field.unit) for field in form.inputs] == [
        ("Solution", None),
        ("Language", None),
        ("Input", None),
        ("Answer", None),
        ("Time limit", "seconds"),
        ("Memory limit", "MB"),
    ]
    reads = [*acme.fake.calls_to("workflow_version"), *acme.fake.calls_to("read_workflow_at")]
    assert reads and {call.identity for call in reads} == {acme.ada.identity}


async def test_a_task_on_a_version_made_with_a_unit_on_a_folder_saves_and_its_form_reads(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    # A version made before a unit was refused on anything but a number.
    stray = WORKFLOW.replace(b"title: Solution}", b"title: Solution, unit: seconds}")
    assert stray != WORKFLOW
    await _workflow(acme, "united", stray)

    saved = await _save(
        setup, acme, sum_task, _with(acme, b"unicon/classic@v3", b"unicon/united@v1")
    )
    form = await task_save.workflow_form(setup, acme.ada, sum_task)

    assert isinstance(saved, Published), saved
    assert form.problem is None


async def test_the_task_form_names_a_newer_version_and_the_task_keeps_its_own(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    assert (await task_save.workflow_form(setup, acme.ada, sum_task)).newer is None
    classic = acme.fake.state.repos[("unicon", "classic.workflow")]
    acme.fake.state.create_version(PLATFORM, classic, "v4")

    form = await task_save.workflow_form(setup, acme.ada, sum_task)

    assert (form.workflow, form.newer) == ("unicon/classic@v3", "v4")
    assert len(form.inputs) == 6


async def test_a_draft_still_shows_its_workflows_inputs(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    draft = await _save(setup, acme, sum_task, _with(acme, b"scoring:", b"credit: 7\nscoring:"))
    assert isinstance(draft, Draft)

    form = await task_save.workflow_form(setup, acme.ada, sum_task)

    assert form.problem is None
    assert len(form.inputs) == 6


@pytest.mark.parametrize(
    ("workflow", "problem"),
    [
        (b"", "does not name a workflow"),
        (b"workflow: [\n", "could not read the task's settings"),
        (b"workflow: classic\n", "must end in @ and a version"),
        (b"workflow: unicon/nothing@v1\n", "Unicon cannot open the workflow"),
    ],
    ids=["none", "not yaml", "not a reference", "unreadable"],
)
async def test_a_task_form_without_a_workflow_it_can_read_says_why(
    setup: Setup, acme: Acme, sum_task: TaskId, workflow: bytes, problem: str
) -> None:
    await _save(setup, acme, sum_task, {"task.yaml": b"name: Sum\n" + workflow})

    form = await task_save.workflow_form(setup, acme.ada, sum_task)

    assert form.inputs == ()
    assert form.problem is not None and problem in form.problem


async def test_a_workflow_in_an_old_format_gives_the_form_its_reason(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _workflow(acme, "old", b"inputs: {}\nstages: []\n")
    await _save(setup, acme, sum_task, _with(acme, b"unicon/classic@v3", b"unicon/old@v1"))

    form = await task_save.workflow_form(setup, acme.ada, sum_task)

    assert form.workflow == "unicon/old@v1"
    assert form.problem is not None and "an older form that Unicon no longer reads" in form.problem
    elsewhere = Scope("acme", "autumn")
    stranger = Organiser(
        user=acme.ada.user,
        grants=(RoleGrant(elsewhere, Role.OBSERVER),),
        scope=elsewhere,
        role=Role.OBSERVER,
        identity=acme.ada.identity,
    )
    with pytest.raises(Forbidden):
        await task_save.workflow_form(setup, stranger, sum_task)


async def test_a_workflow_gives_the_form_its_inputs_with_their_titles_and_units(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _workflow(
        acme,
        "run",
        b"inputs:\n  code: {type: folder, title: Code}\n  seed: {type: number, unit: s}\n"
        b"steps:\n  - {id: a, use: unicon/compile@v3}\n",
    )
    await _save(setup, acme, sum_task, _with(acme, b"unicon/classic@v3", b"unicon/run@v1"))

    form = await task_save.workflow_form(setup, acme.ada, sum_task)

    assert (form.workflow, form.problem) == ("unicon/run@v1", None)
    assert [(each.id, each.type, each.title, each.unit) for each in form.inputs] == [
        ("code", "folder", "Code", None),
        ("seed", "number", None, "s"),
    ]


async def test_a_submit_in_flight_while_a_grading_change_publishes_is_graded_against_it(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The save waits for a submit that holds the task, or the submit reads
    the publication the save made: either way no submission is left graded
    against a plan a published change replaced."""
    record = submissions._record
    saving: list[asyncio.Task[Published | Draft]] = []

    async def record_then_save(*args: Any, **kwargs: Any) -> Any:
        recorded = await record(*args, **kwargs)
        change = _with(acme, b"time_limit: 2", b"time_limit: 1")
        saving.append(asyncio.create_task(_save(setup, acme, entered.task, change, confirm=True)))
        await asyncio.wait(saving, timeout=3)
        return recorded

    monkeypatch.setattr(submissions, "_record", record_then_save)
    made = await upload(setup, acme.fake, entered.session, entered.task, b"print(3)\n")
    await submissions.submit(
        setup,
        entered.session,
        entered.task,
        {
            "code": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key="key-0001-aaaa",
    )
    published = await saving[0]

    assert isinstance(published, Published) and published.grading_changed
    attempts = await _attempts(setup)
    assert attempts[-1][2] == str(published.publication), attempts


async def test_a_save_of_another_task_never_holds_up_a_submit(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    """A save holds the contest's rules and its own task for as long as the
    forge takes; a submit to another task of the contest waits for neither."""
    other = TaskId(f"{entered.task.rsplit('/', 1)[0]}/other")
    holding = asyncio.Event()
    done = asyncio.Event()

    async def save_held_open() -> None:
        async with setup.unit_of_work() as ctx:
            await timelines.hold_rules(ctx, contest_id_of(task_scope(entered.task)))
            await timelines.hold_task(ctx, other)
            holding.set()
            await done.wait()

    saving = asyncio.create_task(save_held_open())
    await holding.wait()
    try:
        made = await upload(setup, acme.fake, entered.session, entered.task, b"print(3)\n")
        submitted = await asyncio.wait_for(
            submissions.submit(
                setup,
                entered.session,
                entered.task,
                {
                    "code": SubmittedInput(uploads=(made.id,)),
                    "language": SubmittedInput(value="python"),
                },
                idempotency_key="key-0001-aaaa",
            ),
            timeout=10,
        )
    finally:
        done.set()
        await saving

    assert submitted.number == 1


async def test_a_submission_is_stamped_when_it_was_checked_open_not_when_the_forge_recorded_it(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forge may take seconds to record a submission, past the close;
    the submission is at the moment it was checked open."""
    clock = setup.clock
    assert isinstance(clock, FakeClock)
    checked = clock.now()
    record = submissions._record

    async def slow_record(*args: Any, **kwargs: Any) -> Any:
        done = await record(*args, **kwargs)
        clock.advance(timedelta(hours=1))
        return done

    monkeypatch.setattr(submissions, "_record", slow_record)
    made = await upload(setup, acme.fake, entered.session, entered.task, b"print(3)\n")
    submitted = await submissions.submit(
        setup,
        entered.session,
        entered.task,
        {
            "code": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key="key-0001-aaaa",
    )

    assert submitted.submitted_at == checked


async def test_a_rejudge_in_flight_and_a_saves_regrade_give_each_submission_its_own_attempt(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejudge holds the task alone, as a save's take-over does, so a save
    that publishes a grading change regrades after it, from every attempt
    the rejudge made, rather than from rows read before it committed."""
    await _graded(setup, acme, entered)
    regrade = gradings.regrade
    calls = 0

    async def slow_first(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        done = await regrade(*args, **kwargs)
        if calls == 1:
            await asyncio.sleep(1)
        return done

    held = asyncio.Event()
    hold_task = timelines.hold_task

    async def hold_then_say(*args: Any, **kwargs: Any) -> None:
        await hold_task(*args, **kwargs)
        held.set()

    monkeypatch.setattr(gradings, "regrade", slow_first)
    monkeypatch.setattr(gradings.regrading, "regrade", slow_first)
    monkeypatch.setattr(timelines, "hold_task", hold_then_say)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring", "sum"), Role.MANAGER)
    rejudging = asyncio.create_task(gradings.rejudge(setup, manager, entered.task))
    # The save starts only once the rejudge holds the task, so it is the save
    # that waits.
    await held.wait()
    change = _with(acme, b"time_limit: 2", b"time_limit: 1")

    saved = await _save(setup, acme, entered.task, change, confirm=True)
    await rejudging

    assert isinstance(saved, Published)
    assert [(number, attempt) for number, attempt, *_ in await _attempts(setup)] == [
        (1, 1),
        (1, 2),
        (1, 3),
    ]
