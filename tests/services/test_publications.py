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
nor is left unsaid on a new group. A task left giving no points is refused
while its contest's entry gives it a worth or a due.
"""

import asyncio
import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

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
from forge.domain.plans import PLAN_PATH, Plan
from forge.domain.roles import Role, RoleGrant, Scope, contest_id_of, task_scope
from forge.domain.save_effect import SaveEffect
from forge.domain.submissions import SubmittedInput
from forge.domain.workflows import Visibility
from forge.runtime.setup import Setup
from forge.services import gradings, publications, submissions, tasks, timelines
from forge.services.access import Organiser
from forge.services.publications import Draft, Published
from forge.testing import CLASSIC, PLACEHOLDER_DIGEST, PRIMITIVES, FakeClock
from tests.services.conftest import Acme, Entered, organiser, upload, write_contest

RUNNING = """\
name: Spring 2026
start: 2026-09-26T10:00:00Z
end: 2026-09-26T15:00:00Z
state: {state}
visibility: everyone
tasks:
  - id: sum
"""

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
    return await publications.save(
        setup, who or acme.ada, task, await _edits(acme, task, contents), **options
    )


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
    """A public workflow at `v1` with `content` as its `workflow.yaml`."""
    made = await acme.fake.workflows.create_workflow(
        PLATFORM, owner, name, {"workflow.yaml": content}, Visibility.PUBLIC
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, made, "v1")


async def test_the_starters_first_save_publishes_its_plan_and_the_plan_keeps_the_contract(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    result = await _save(setup, acme, sum_task, {"statement.md": b"Add two numbers.\n"})

    assert isinstance(result, Published)
    assert (result.number, result.grading_changed, result.changes) == (1, False, ())
    # The save says what it did, in the words a running contest asks with.
    assert result.notes == ()
    assert result.effect == SaveEffect(
        results_change=False,
        summary="Nobody's result changes.",
        runs_again=("No submission runs again: this is the task's first publication.",),
        scores=("Points: main is worth up to 100.", "sum now counts on the leaderboard Standings."),
        reading=("Entrants read the task from its release.",),
    )
    (saved,) = acme.fake.calls_to("save_files")
    # The platform writes it, with ada as its author (#443).
    assert (saved.identity, saved.arguments["author"]) == (PLATFORM, acme.ada.user.id)
    assert saved.arguments["paths"] == ["plans/plan.json", "statement.md"]
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    head = repo.history[-1]
    assert (head.author_id, head.message) == (7, "Save")
    assert repo.versions == {"published/1": head.version}
    assert sorted(path for path in repo.files if path.startswith("plans/")) == ["plans/plan.json"]
    document = json.loads(repo.files["plans/plan.json"])
    assert violation(document, "plan") is None
    plan = Plan.from_bytes(repo.files["plans/plan.json"])
    assert plan.schema_version == 5
    assert [step.primitive for step in plan.steps] == [
        "unicon/compile@v2",
        "unicon/sandbox-run@v2",
        "unicon/diff-check@v2",
    ]
    assert plan.tests == ("main/1",)
    assert set(plan.contestant) == {"submission", "language"}
    # The workflow's declaration; the task's narrower options are its form's.
    assert plan.contestant["language"].options == ("c", "cpp", "java", "python")
    assert set(plan.report) == {"time_ms", "memory_kb", "log"}
    assert plan.task_paths() == ("tests/main/1/answer", "tests/main/1/input")
    assert plan.harness_image == setup.settings.harness_image
    reads = acme.fake.calls_to("read_declaration")
    assert {call.arguments["primitive"] for call in reads} == {
        "compile",
        "sandbox-run",
        "diff-check",
    }
    assert all(call.identity == acme.ada.identity for call in reads)
    (published,) = acme.fake.calls_to("publish")
    assert published.identity == PLATFORM
    (listed,) = await publications.list(setup, acme.ada, sum_task)
    assert (listed.id, listed.number, listed.version) == (result.publication, 1, head.version)
    assert (listed.grading_changed, listed.changes) == (False, ())
    assert set(listed.workflows) == {"unicon/classic"}
    # Each reported value's words are frozen with it, in the report's order.
    assert [(each.name, each.title, each.unit) for each in listed.meanings] == [
        ("time_ms", "Run time", "ms"),
        ("memory_kb", "Memory used", "KB"),
        ("log", "Compile log", None),
    ]


async def test_each_publication_is_numbered_after_the_last_and_none_touches_the_ci(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    first = await _save(setup, acme, sum_task, {"statement.md": b"One.\n"})
    second = await _save(setup, acme, sum_task, {"statement.md": b"Two.\n"})

    assert isinstance(first, Published) and isinstance(second, Published)
    assert (first.number, second.number) == (1, 2)
    assert acme.fake.calls_to("activate") == []


async def test_a_save_that_changes_nothing_since_the_publication_publishes_nothing_new(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    first = await _published(setup, acme, sum_task)

    again = await publications.save(setup, acme.ada, sum_task, {})

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
        await publications.save(setup, acme.ada, sum_task, stale)
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
    assert saved.arguments["paths"] == ["plans/default.json", "plans/plan.json", "statement.md"]
    assert sorted(path for path in repo.files if path.startswith("plans/")) == ["plans/plan.json"]


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
        ({"task.yaml": (b"test_groups:", b"submissions: {max: 5}\ntest_groups:")}, ["submissions"]),
        (
            {
                "task.yaml": (
                    b"test_groups:",
                    b"submissions:\n  rate: {count: 2, per: 30}\ntest_groups:",
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
                b"main: {each: 100}", b"main: {pass: 100, show: verdict}"
            ),
            "statement.md": statement,
            "tests/main/2/input": b"2 2\n",
            "tests/main/2/answer": b"4\n",
            "public/notes.txt": b"Read me.\n",
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
    assert await publications.list(setup, bob, sum_task) == ()


async def test_a_group_with_no_tests_is_a_draft_and_the_state_recomputes_its_errors(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    published = await _published(setup, acme, sum_task)
    broken = _with(acme, b"  main: {each: 100}\n", b"  samples: {}\n  main: {each: 100}\n")

    draft = await _save(setup, acme, sum_task, broken)

    assert isinstance(draft, Draft)
    assert draft.errors == (
        {
            "path": "test_groups.samples",
            "message": "The test group samples has no tests. Add a test to it.",
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

    fixed = await _save(
        setup, acme, sum_task, {"tests/samples/1/input": b"1 2\n", "tests/samples/1/answer": b"3\n"}
    )
    assert isinstance(fixed, Published)
    assert fixed.number == 2
    later = await tasks.state(setup, acme.ada, sum_task)
    assert (later.draft, later.errors) == (False, ())


@pytest.mark.parametrize(
    ("files", "path", "said"),
    [
        (
            {"tests/main/2/input": b"2 2\n"},
            "tests/main/2/",
            "The test main/2 has no answer. Add it.",
        ),
        (
            {"tests/main/1/notes": b"x\n"},
            "tests/main/1/notes",
            "The test main/1 has notes, which is none of the workflow's test fields. Rename it "
            "after one, or remove it.",
        ),
        (
            {"tests/main/1/answer.txt": b"3\n"},
            "tests/main/1/",
            "The test main/1 has answer more than once: answer, answer.txt. Keep one.",
        ),
        (
            {"tests/other/1/input": b"1 2\n", "tests/other/1/answer": b"3\n"},
            "tests/other/",
            "other holds tests but is not one of the task's test groups. Add a test group "
            "called other, or move its tests.",
        ),
    ],
    ids=["missing-field", "no-such-field", "field-twice", "unlisted-group"],
)
async def test_a_test_out_of_its_layout_is_a_draft_at_its_folder(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    files: dict[str, bytes],
    path: str,
    said: str,
) -> None:
    draft = await _save(setup, acme, sum_task, files)

    assert isinstance(draft, Draft)
    assert [(error["path"], error["message"]) for error in draft.errors] == [(path, said)]
    assert acme.fake.calls_to("publish") == []


SECOND_TEST = {"tests/main/2/input": b"1 1\n", "tests/main/2/answer": b"2\n"}


async def test_a_removed_test_is_gone_from_the_change_and_the_grading_comparison_names_it(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)
    added = await _save(setup, acme, sum_task, SECOND_TEST)
    assert isinstance(added, Published), added
    acme.fake.reset_calls()

    removed = await _save(setup, acme, sum_task, dict.fromkeys(SECOND_TEST))

    assert isinstance(removed, Published), removed
    assert (removed.number, removed.grading_changed) == (3, True)
    assert removed.changes == (
        "The way the task grades changed.",
        "The answer of test 2 in the group main was removed.",
        "The input of test 2 in the group main was removed.",
    )
    (saved,) = acme.fake.calls_to("save_files")
    assert saved.arguments["paths"] == [
        "plans/plan.json",
        "tests/main/2/answer",
        "tests/main/2/input",
    ]
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    assert not any(path.startswith("tests/main/2/") for path in repo.files)
    assert Plan.from_bytes(repo.files["plans/plan.json"]).tests == ("main/1",)


async def test_a_test_is_replaced_by_one_with_other_entry_names_in_one_save(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)

    result = await _save(
        setup,
        acme,
        sum_task,
        {
            "tests/main/1/input": None,
            "tests/main/1/answer": None,
            "tests/main/1/input.txt": b"4 5\n",
            "tests/main/1/answer.txt": b"9\n",
        },
    )

    assert isinstance(result, Published), result
    assert result.changes == (
        "The way the task grades changed.",
        "The answer of test 1 in the group main changed.",
        "The input of test 1 in the group main changed.",
    )
    plan = Plan.from_bytes(acme.fake.state.repos[("acme", "spring.sum.task")].files[PLAN_PATH])
    assert plan.task_paths() == ("tests/main/1/answer.txt", "tests/main/1/input.txt")


async def test_removing_a_groups_last_test_is_a_draft_at_the_group(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    samples = {"tests/samples/1/input": b"1 2\n", "tests/samples/1/answer": b"3\n"}
    first = await _save(
        setup,
        acme,
        sum_task,
        {
            **_with(acme, b"  main: {each: 100}\n", b"  samples: {}\n  main: {each: 100}\n"),
            **samples,
        },
    )
    assert isinstance(first, Published), first
    acme.fake.reset_calls()

    draft = await _save(setup, acme, sum_task, dict.fromkeys(samples))

    assert isinstance(draft, Draft)
    assert [(error["path"], error["message"]) for error in draft.errors] == [
        ("test_groups.samples", "The test group samples has no tests. Add a test to it.")
    ]
    assert acme.fake.calls_to("publish") == []
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    assert not any(path.startswith("tests/samples/") for path in repo.files)
    assert len(repo.versions) == 1


async def test_removing_every_test_is_a_draft_saying_the_task_has_none(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    draft = await _save(
        setup, acme, sum_task, {"tests/main/1/input": None, "tests/main/1/answer": None}
    )

    assert isinstance(draft, Draft)
    assert [(error["path"], error["message"]) for error in draft.errors] == [
        ("test_groups", "The task has no tests yet. Add a test to each test group.")
    ]


async def test_removing_one_field_of_a_test_is_a_draft_at_the_test(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    draft = await _save(setup, acme, sum_task, {"tests/main/1/answer": None})

    assert isinstance(draft, Draft)
    assert [(error["path"], error["message"]) for error in draft.errors] == [
        ("tests/main/1/", "The test main/1 has no answer. Add it.")
    ]


FIXED_ANSWER = (
    CLASSIC.replace(b"  answer: file\n", b"")
    .replace(b"expected: ${{ test.answer }}", b"expected: ${{ inputs.expected }}")
    .replace(b"\ntest:\n", b"\n  expected:\n    type: file\ntest:\n")
)
"""Classic with one answer for every test, a file the task names."""


async def test_removing_a_file_an_input_names_is_a_draft_at_that_input(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _workflow(acme, "fixed", FIXED_ANSWER)
    task_yaml = (
        _task_yaml(acme)
        .replace(b"unicon/classic@v2", b"unicon/fixed@v1")
        .replace(b"  time_limit: 2\n", b"  time_limit: 2\n  expected: data/answer.txt\n")
    )
    first = await _save(
        setup,
        acme,
        sum_task,
        {"task.yaml": task_yaml, "data/answer.txt": b"3\n", "tests/main/1/answer": None},
    )
    assert isinstance(first, Published), first
    acme.fake.reset_calls()

    draft = await _save(setup, acme, sum_task, {"data/answer.txt": None})

    assert isinstance(draft, Draft)
    assert [(error["path"], error["message"]) for error in draft.errors] == [
        ("inputs.expected", "The task has no file data/answer.txt. Add it, or give another file.")
    ]
    assert acme.fake.calls_to("publish") == []


async def test_a_removal_with_a_stale_token_is_a_conflict_and_nothing_is_written(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)
    await _save(setup, acme, sum_task, {"public/hint.txt": b"One.\n"})
    stale = await _edits(acme, sum_task, {"public/hint.txt": None})
    await _save(setup, acme, sum_task, {"public/hint.txt": b"Two.\n"})
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    head = repo.head
    acme.fake.reset_calls()

    with pytest.raises(Conflict):
        await publications.save(setup, acme.ada, sum_task, stale)

    assert acme.fake.calls_to("publish") == []
    assert (repo.head, repo.files["public/hint.txt"]) == (head, b"Two.\n")


async def test_a_removal_of_a_file_that_is_not_there_is_a_conflict(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)
    token = (await acme.fake.content.list_files(PLATFORM, sum_task)).tokens["statement.md"]
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    head = repo.head

    with pytest.raises(Conflict):
        await publications.save(setup, acme.ada, sum_task, {"public/gone.txt": Edit(None, token)})

    assert acme.fake.calls_to("publish") == []
    assert repo.head == head


async def test_a_removal_without_its_token_is_refused_naming_it(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    with pytest.raises(InvalidInputs) as refused:
        await publications.save(
            setup,
            acme.ada,
            sum_task,
            {"tests/main/1/input": Edit(None), "statement.md": Edit(b"x\n")},
        )

    assert refused.value.extra["errors"] == [
        {"input": "tests/main/1/input", "message": "Reload the files, then remove this one again."}
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
    await _save(setup, acme, sum_task, {**SECOND_TEST, "public/hint.txt": b"Add.\n"})
    acme.fake.reset_calls()

    result = await _save(
        setup, acme, sum_task, {**dict.fromkeys(SECOND_TEST), "public/hint.txt": None}, manager
    )

    assert isinstance(result, Published), result
    (saved,) = acme.fake.calls_to("save_files")
    assert (saved.identity, saved.arguments["author"]) == (PLATFORM, manager.user.id)
    files = acme.fake.state.repos[("acme", "spring.sum.task")].files
    assert "public/hint.txt" not in files and "tests/main/2/input" not in files


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ((b"workflow: unicon/classic@v2", b"workflow: unicon/classic@v9"), "unicon/classic@v9"),
        ((b"workflow: unicon/classic@v2", b"workflow: bob/mine@v1"), "bob/mine@v1"),
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
        bob, "bob", "mine", {"workflow.yaml": CLASSIC}, Visibility.PRIVATE
    )
    await acme.fake.workflows.create_workflow_version(bob, mine, "v1")

    draft = await _save(setup, acme, sum_task, _with(acme, *change))

    assert isinstance(draft, Draft)
    (error,) = draft.errors
    assert error["path"] == "workflow"
    assert "Unicon cannot open the workflow" in error["message"]
    assert named in error["message"]
    assert acme.fake.calls_to("publish") == []
    reads = acme.fake.calls_to("read_workflow_file")
    assert {call.identity for call in reads} == {acme.ada.identity}


async def test_a_workflow_in_an_old_format_is_refused_at_its_line_for_its_owner_to_retag(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _workflow(acme, "old", OLD_CLASSIC)

    draft = await _save(
        setup,
        acme,
        sum_task,
        _with(acme, b"workflow: unicon/classic@v2", b"workflow: unicon/old@v1"),
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
        acme, "broken", CLASSIC.replace(b"steps.compile.binary", b"steps.compile.nothing")
    )

    draft = await _save(
        setup,
        acme,
        sum_task,
        _with(acme, b"workflow: unicon/classic@v2", b"workflow: unicon/broken@v1"),
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
        (
            b"unicon/classic@v2",
            "In the workflow unicon/custom@v1, the step compile cannot run: unicon/classic@v2 is "
            "a workflow, and a step can only use a primitive.",
        ),
    ],
    ids=["no-such-version", "a-workflow"],
)
async def test_a_step_whose_use_is_no_primitive_the_organiser_reads_is_an_error_naming_it(
    setup: Setup, acme: Acme, sum_task: TaskId, use: bytes, said: str
) -> None:
    await _workflow(acme, "custom", CLASSIC.replace(b"use: unicon/compile@v2", b"use: " + use))

    draft = await _save(
        setup,
        acme,
        sum_task,
        _with(acme, b"workflow: unicon/classic@v2", b"workflow: unicon/custom@v1"),
    )

    assert isinstance(draft, Draft)
    assert [(error["path"], error["message"]) for error in draft.errors] == [("workflow", said)]
    reads = acme.fake.calls_to("read_declaration")
    assert reads and {call.identity for call in reads} == {acme.ada.identity}
    assert acme.fake.calls_to("publish") == []


async def test_a_primitive_in_an_old_format_is_refused_naming_it(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    acme.fake.primitives.add("odd", {"v1": OLD_COMPILE, "v2": PRIMITIVES["compile"]})
    await _workflow(
        acme, "custom", CLASSIC.replace(b"use: unicon/compile@v2", b"use: unicon/odd@v1")
    )

    draft = await _save(
        setup,
        acme,
        sum_task,
        _with(acme, b"workflow: unicon/classic@v2", b"workflow: unicon/custom@v1"),
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
    given = _with(acme, b"  submission: {label: Your solution}", b"  submission: main.py")
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
        ["inputs.submission"],
        ["inputs.speed"],
    ]
    assert acme.fake.calls_to("publish") == []


async def _secret_workflow(acme: Acme) -> None:
    """`unicon/args@v1`: classic with a text input handed to the run."""
    await _workflow(
        acme,
        "args",
        CLASSIC.replace(b"\ntest:\n", b"\n  args: text\ntest:\n").replace(
            b"      memory_limit: ${{ inputs.memory_limit }}\n",
            b"      memory_limit: ${{ inputs.memory_limit }}\n      args: ${{ inputs.args }}\n",
        ),
    )


async def test_a_secret_is_refused_since_the_org_holds_none_and_a_plain_text_publishes(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _secret_workflow(acme)
    task_yaml = _task_yaml(acme).replace(
        b"workflow: unicon/classic@v2", b"workflow: unicon/args@v1"
    )
    secret = task_yaml.replace(b"  time_limit: 2\n", b"  time_limit: 2\n  args: {secret: k}\n")
    plain = task_yaml.replace(b"  time_limit: 2\n", b"  time_limit: 2\n  args: '-v'\n")

    refused = await _save(setup, acme, sum_task, {"task.yaml": secret})
    published = await _save(setup, acme, sum_task, {"task.yaml": plain})

    assert isinstance(refused, Draft)
    assert [(error["path"], error["message"]) for error in refused.errors] == [
        ("inputs.args", "The org has no secret called k. Choose one it has, or add it to the org.")
    ]
    assert isinstance(published, Published), published


async def test_scoring_changes_never_change_grading_and_a_test_or_a_limit_does(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)

    text = await _save(setup, acme, sum_task, {"statement.md": b"Clearer.\n"})
    rules = await _save(setup, acme, sum_task, _with(acme, b"{each: 100}", b"{pass: 100}"))
    caps = await _save(
        setup,
        acme,
        sum_task,
        _with(
            acme, b"test_groups:", b"submissions: {max: 9, rate: {count: 2, per: 60}}\ntest_groups:"
        ),
    )
    public = await _save(setup, acme, sum_task, {"public/hint.txt": b"Add.\n"})
    limit = await _save(setup, acme, sum_task, _with(acme, b"time_limit: 2", b"time_limit: 3"))
    edited = await _save(setup, acme, sum_task, {"tests/main/1/input": b"2 3\n"})
    same = await _save(setup, acme, sum_task, {"tests/main/1/input": b"2 3\n"})
    added = await _save(
        setup, acme, sum_task, {"tests/main/2/input": b"1 1\n", "tests/main/2/answer": b"2\n"}
    )

    results = [
        result
        for result in (text, rules, caps, public, limit, edited, same, added)
        if isinstance(result, Published)
    ]
    assert [(result.grading_changed, result.changes) for result in results] == [
        (False, ()),
        (False, ()),
        (False, ()),
        (False, ()),
        (True, ("The way the task grades changed.",)),
        (True, ("The input of test 1 in the group main changed.",)),
        (False, ()),
        (
            True,
            (
                "The way the task grades changed.",
                "The answer of test 2 in the group main was added.",
                "The input of test 2 in the group main was added.",
            ),
        ),
    ]
    history = await publications.list(setup, acme.ada, sum_task)
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
    assert history[5].changes == ("The way the task grades changed.",)


@pytest.fixture
async def running(setup: Setup, acme: Acme, sum_task: TaskId) -> TaskId:
    """The task published once, in a contest that has started."""
    await write_contest(acme.fake, RUNNING.format(state="published"))
    await _published(setup, acme, sum_task)
    return sum_task


@pytest.mark.parametrize(
    ("change", "listed"),
    [
        ((b"time_limit: 2", b"time_limit: 1"), ["The way the task grades changed."]),
        (None, ["The input of test 1 in the group main changed."]),
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
    files = _with(acme, *change) if change else {"tests/main/1/input": b"5 5\n"}

    with pytest.raises(ConfirmationRequired) as asked:
        await _save(setup, acme, running, files)

    assert asked.value.code == "confirmation_required"
    assert asked.value.extra == {
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


@pytest.mark.parametrize(
    ("files", "scores", "reading"),
    [
        (
            (b"{each: 100}", b"{each: 60, pass: 50}"),
            [
                "Points: main is now worth up to 110 (was 100).",
                "How main earns its points: 60 shared over its tests and 50 when every test "
                "passes (was 100 shared over its tests).",
                "Every result is scored again.",
            ],
            ["Nothing entrants read changes."],
        ),
        (
            (b"test_groups:", b"submissions: {max: 3, rate: {count: 2, per: 600}}\ntest_groups:"),
            [
                "Submission limit: 3 per entrant (was 50).",
                "Submission rate: 2 every 10 minutes (was 1 every 30 seconds).",
            ],
            ["Nothing entrants read changes."],
        ),
        (
            {"statement.md": b"Typo fixed.\n", "public/figure.png": b"\x89PNG"},
            ["No score changes."],
            [
                "The statement changes.",
                "The pictures and files beside the statement change.",
            ],
        ),
    ],
    ids=["points", "limits", "statement"],
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
    assert (kept.errors, kept.held_back) == ((), ("The way the task grades changed.",))
    assert _written(acme) == ["save_files"]
    (saved,) = acme.fake.calls_to("save_files")
    assert saved.arguments["paths"] == ["task.yaml"]
    state = await tasks.state(setup, acme.ada, running)
    assert (state.draft, state.errors) == (True, ())
    published = await publications.save(setup, acme.ada, running, {}, confirm=True)
    assert isinstance(published, Published)
    assert published.changes == ("The way the task grades changed.",)


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
    assert acme.fake.calls_to("activate") == []
    assert [entry.number for entry in await publications.list(setup, acme.ada, sum_task)] == [1]
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
            "submission": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key=key,
    )
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Grading).values(status=GradingStatus.DONE))
    acme.fake.reset_calls()


@pytest.mark.parametrize(
    ("before", "after", "refused"),
    [
        (b"{each: 100}", b"{each: 100, show: after_close}", "main as soon as a grading ends"),
        (b"{each: 100}", b"{pass: 100, show: verdict}", "main as soon as a grading ends"),
        (
            b"{pass: 100, show: verdict}",
            b"{pass: 100, show: after_close}",
            "main as an outcome before the reveal",
        ),
        (b"{each: 100, show: after_close}", b"{each: 100}", None),
        (b"{each: 100, show: after_close}", b"{pass: 100, show: verdict}", None),
        (b"{pass: 100, show: verdict}", b"{pass: 100}", None),
    ],
    ids=[
        "always-to-after-close",
        "always-to-verdict",
        "verdict-to-after-close",
        "after-close-to-always",
        "after-close-to-verdict",
        "verdict-to-always",
    ],
)
async def test_once_graded_what_was_shown_is_not_hidden_again_and_the_other_way_is_allowed(
    setup: Setup,
    acme: Acme,
    entered: Entered,
    before: bytes,
    after: bytes,
    refused: str | None,
) -> None:
    if before != b"{each: 100}":
        first = await _save(
            setup, acme, entered.task, _with(acme, b"{each: 100}", before), confirm=True
        )
        assert isinstance(first, Published), first
    await _graded(setup, acme, entered)

    result = await _save(setup, acme, entered.task, _with(acme, before, after), confirm=True)

    if refused is None:
        assert isinstance(result, Published), result
        return
    assert isinstance(result, Draft)
    assert [(error["path"], error["message"]) for error in result.errors] == [
        (
            "test_groups.main.show",
            f"Entrants have seen the results of {refused}, and what they have seen cannot be "
            "hidden again.",
        )
    ]
    assert acme.fake.calls_to("publish") == []


async def test_before_any_grading_is_done_a_group_may_be_hidden(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    result = await _save(
        setup,
        acme,
        entered.task,
        _with(acme, b"{each: 100}", b"{each: 100, show: after_close}"),
        confirm=True,
    )

    assert isinstance(result, Published), result


NEW_GROUP = {"tests/large/1/input": b"5 6\n", "tests/large/1/answer": b"11\n"}


async def test_once_graded_a_new_group_must_say_its_show(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    before = await publications.workflow_form(setup, acme.ada, entered.task)
    await _graded(setup, acme, entered)
    # The form hears of it, so it asks for the show of a group the save adds.
    after = await publications.workflow_form(setup, acme.ada, entered.task)
    assert (before.graded, after.graded) == (False, True)
    unsaid = _with(acme, b"  main: {each: 100}\n", b"  main: {each: 100}\n  large: {each: 50}\n")

    draft = await _save(setup, acme, entered.task, {**unsaid, **NEW_GROUP})
    said = await _save(
        setup,
        acme,
        entered.task,
        {"task.yaml": unsaid["task.yaml"].replace(b"{each: 50}", b"{each: 50, show: always}")},
        confirm=True,
    )

    assert isinstance(draft, Draft)
    assert [(error["path"], error["message"]) for error in draft.errors] == [
        (
            "test_groups.large",
            "large is a new test group, and the task has graded submissions, so choose when "
            "entrants see its results.",
        )
    ]
    assert isinstance(said, Published), said


async def test_before_any_grading_is_done_a_new_group_needs_no_show(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    added = _with(acme, b"  main: {each: 100}\n", b"  main: {each: 100}\n  large: {each: 50}\n")

    result = await _save(setup, acme, entered.task, {**added, **NEW_GROUP}, confirm=True)

    assert isinstance(result, Published), result


async def _removed(acme: Acme, task: TaskId, paths: list[str]) -> None:
    """`paths` taken out of the task at the forge, as the file editor's
    removal leaves them.
    """
    head = await acme.fake.content.list_files(PLATFORM, task)
    await acme.fake.content.save_files(
        PLATFORM,
        task,
        dict.fromkeys(paths),
        expected={path: head.tokens[path] for path in paths},
        message="Remove",
    )


async def test_what_any_graded_publication_showed_is_not_hidden_when_its_group_comes_back(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    with_large = _with(
        acme, b"  main: {each: 100}\n", b"  main: {each: 100}\n  large: {each: 50}\n"
    )
    first = await _save(setup, acme, entered.task, {**with_large, **NEW_GROUP}, confirm=True)
    assert isinstance(first, Published), first
    await _graded(setup, acme, entered)
    await _removed(acme, entered.task, sorted(NEW_GROUP))
    without = await _save(
        setup,
        acme,
        entered.task,
        _with(acme, b"  large: {each: 50}\n", b""),
        confirm=True,
    )
    assert isinstance(without, Published), without
    hidden = _with(
        acme,
        b"  main: {each: 100}\n",
        b"  main: {each: 100}\n  large: {each: 50, show: after_close}\n",
    )

    refused = await _save(setup, acme, entered.task, {**hidden, **NEW_GROUP}, confirm=True)
    shown = await _save(
        setup,
        acme,
        entered.task,
        {"task.yaml": hidden["task.yaml"].replace(b"after_close", b"always"), **NEW_GROUP},
        confirm=True,
    )

    assert isinstance(refused, Draft)
    assert [(error["path"], error["message"]) for error in refused.errors] == [
        (
            "test_groups.large.show",
            "Entrants have seen the results of large as soon as a grading ends, and what they "
            "have seen cannot be hidden again.",
        )
    ]
    assert isinstance(shown, Published), shown


async def _attempts(setup: Setup) -> list[tuple[int, int, str, GradingStatus]]:
    async with setup.unit_of_work() as ctx:
        rows = (
            await ctx.db.execute(
                select(Grading).order_by(Grading.submission_number, Grading.attempt)
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
        "changes": ["The way the task grades changed."],
        "regrades": 2,
        "results_change": True,
        "summary": "Results change.",
        "runs_again": ["2 submissions are graded again: the way the task grades changed."],
        "scores": ["No score changes."],
        "reading": ["Nothing entrants read changes."],
    }
    assert isinstance(confirmed, Published)
    assert confirmed.regraded == 2
    assert confirmed.effect.runs_again == (
        "2 submissions are graded again: the way the task grades changed.",
    )


async def test_a_grading_change_with_every_submission_cancelled_says_none_is_graded_again(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _graded(setup, acme, entered)
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading).values(
                status=GradingStatus.CANCELLED, cancel_reason="The machine broke."
            )
        )

    with pytest.raises(ConfirmationRequired) as asked:
        await _save(setup, acme, entered.task, _with(acme, b"time_limit: 2", b"time_limit: 1"))

    assert asked.value.extra["runs_again"] == [
        "No submission runs again: none of those made is graded again.",
        "The way the task grades changed.",
    ]


async def test_a_latest_publication_whose_settings_no_longer_read_is_not_called_the_first(
    setup: Setup, acme: Acme, running: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A publication's settings stop reading when a later forge reads them
    # more strictly than the one that published them.
    async def unread(*_: object) -> None:
        return None

    monkeypatch.setattr(publications, "_definition_of", unread)

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
    repo = acme.fake.state.repos[("unicon", "sandbox-run.primitive")]
    declaration = acme.fake.state.version_files(repo, "v2")["primitive.yaml"]
    moved = declaration.replace(PLACEHOLDER_DIGEST.encode(), b"sha256:" + b"7" * 64)
    assert moved != declaration
    acme.fake.state.commit(repo, {"primitive.yaml": moved}, "Release 2.0.1", None)
    repo.versions["v2"] = repo.head
    statement = {"statement.md": b"Add two numbers, now said more plainly.\n"}

    with pytest.raises(ConfirmationRequired) as asked:
        await _save(setup, acme, entered.task, statement)

    assert asked.value.extra == {
        "changes": [
            "The way the task grades changed.",
            "A release updated sandbox-run@v2 in place.",
        ],
        "regrades": 1,
        "results_change": True,
        "summary": "Results change.",
        "runs_again": [
            "1 submission is graded again, for these changes:",
            "The way the task grades changed.",
            "A release updated sandbox-run@v2 in place.",
        ],
        "scores": ["No score changes."],
        "reading": ["The statement changes."],
    }


async def test_a_published_grading_change_regrades_every_submission_and_a_scoring_one_none(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _graded(setup, acme, entered)
    (graded,) = await _attempts(setup)

    scoring = await _save(
        setup, acme, entered.task, _with(acme, b"{each: 100}", b"{pass: 100}"), confirm=True
    )
    unchanged = await _attempts(setup)
    grading = await _save(
        setup, acme, entered.task, _with(acme, b"time_limit: 2", b"time_limit: 1"), confirm=True
    )

    assert isinstance(scoring, Published) and isinstance(grading, Published)
    assert (scoring.regraded, unchanged) == (0, [graded])
    # Nothing runs again, but the graded submission's points move.
    assert (scoring.effect.results_change, scoring.effect.runs_again) == (
        True,
        ("No submission runs again: the tests and how they run are the same.",),
    )
    assert grading.regraded == 1
    first, again = await _attempts(setup)
    assert first == graded
    assert again[:3] == (1, 2, grading.publication)
    assert again[3] in (GradingStatus.QUEUED, GradingStatus.DISPATCHED)


WORTH = RUNNING + "    worth: 50\n"
DUE = RUNNING + "    due: 2026-09-26T14:00:00Z\n"


@pytest.mark.parametrize(
    ("contest", "line"),
    [
        (WORTH, "gives it a worth of 50"),
        (DUE, "gives it a due time"),
    ],
    ids=["worth", "due"],
)
async def test_a_task_left_giving_no_points_is_refused_while_its_contest_entry_scores_it(
    setup: Setup, acme: Acme, sum_task: TaskId, contest: str, line: str
) -> None:
    await _published(setup, acme, sum_task)
    await write_contest(acme.fake, contest.format(state="draft"))

    result = await _save(setup, acme, sum_task, _with(acme, b"{each: 100}", b"{}"))

    assert isinstance(result, Draft)
    assert [(error["path"], error["message"]) for error in result.errors] == [
        (
            "test_groups",
            f"No test group gives points, so sum gives no points, but the contest {line}. Remove "
            "that from the contest first.",
        )
    ]
    await write_contest(acme.fake, RUNNING.format(state="draft"))
    assert isinstance(await _save(setup, acme, sum_task, {}), Published)


async def test_the_task_form_reads_the_inputs_and_test_fields_its_workflow_declares(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    form = await publications.workflow_form(setup, acme.ada, sum_task)

    assert (form.workflow, form.problem) == ("unicon/classic@v2", None)
    assert [(field.id, field.type, field.contestant) for field in form.inputs] == [
        ("submission", "file", True),
        ("language", "enum", True),
        ("time_limit", "number", False),
        ("memory_limit", "number", False),
    ]
    assert form.inputs[1].options == ("c", "cpp", "java", "python")
    assert [(field.title, field.unit) for field in form.inputs] == [
        ("Solution", None),
        ("Language", None),
        ("Time limit", "seconds"),
        ("Memory limit", "MB"),
    ]
    assert form.inputs[3].help == "The memory the program may use on each test."
    assert [(field.name, field.type) for field in form.test] == [
        ("input", "file"),
        ("answer", "file"),
    ]
    reads = acme.fake.calls_to("read_workflow_file")
    assert {call.identity for call in reads} == {acme.ada.identity}


async def test_a_task_on_a_version_made_with_a_unit_on_a_file_saves_and_reads_without_it(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    # A version made before a unit was refused on anything but a number.
    stray = CLASSIC.replace(b"    title: Solution\n", b"    title: Solution\n    unit: seconds\n")
    assert stray != CLASSIC
    await _workflow(acme, "united", stray)
    current = acme.fake.state.repos[("acme", "spring.sum.task")].files["task.yaml"]

    saved = await _save(
        setup,
        acme,
        sum_task,
        {"task.yaml": current.replace(b"unicon/classic@v2", b"unicon/united@v1")},
    )
    form = await publications.workflow_form(setup, acme.ada, sum_task)

    assert isinstance(saved, Published), saved
    assert form.problem is None
    assert [(field.id, field.unit) for field in form.inputs][:1] == [("submission", None)]


async def test_the_task_form_names_a_newer_version_and_the_task_keeps_its_own(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    assert (await publications.workflow_form(setup, acme.ada, sum_task)).newer is None
    classic = acme.fake.state.repos[("unicon", "classic.workflow")]
    acme.fake.state.create_version(PLATFORM, classic, "v3")

    form = await publications.workflow_form(setup, acme.ada, sum_task)

    assert (form.workflow, form.newer) == ("unicon/classic@v2", "v3")
    assert len(form.inputs) == 4


async def test_a_draft_still_shows_its_workflows_inputs(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    draft = await _save(
        setup, acme, sum_task, _with(acme, b"test_groups:", b"credit: 7\ntest_groups:")
    )
    assert isinstance(draft, Draft)

    form = await publications.workflow_form(setup, acme.ada, sum_task)

    assert form.problem is None
    assert len(form.inputs) == 4


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

    form = await publications.workflow_form(setup, acme.ada, sum_task)

    assert (form.inputs, form.test) == ((), ())
    assert form.problem is not None and problem in form.problem


async def test_a_workflow_in_an_old_format_gives_the_form_its_reason(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _workflow(acme, "old", b"inputs: {}\nstages: []\n")
    await _save(setup, acme, sum_task, _with(acme, b"unicon/classic@v2", b"unicon/old@v1"))

    form = await publications.workflow_form(setup, acme.ada, sum_task)

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
        await publications.workflow_form(setup, stranger, sum_task)


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
            "submission": SubmittedInput(uploads=(made.id,)),
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
                    "submission": SubmittedInput(uploads=(made.id,)),
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
            "submission": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key="key-0001-aaaa",
    )

    assert submitted.submitted_at == checked


async def test_a_rejudge_in_flight_and_a_saves_regrade_give_each_submission_its_own_attempt(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejudge holds the task alone, as a save does, so a save that
    publishes a grading change regrades after it, from every attempt the
    rejudge made, rather than from rows read before it committed."""
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
    monkeypatch.setattr(timelines, "hold_task", hold_then_say)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring", "sum"), Role.MANAGER)
    rejudging = asyncio.create_task(gradings.rejudge(setup, manager, entered.task))
    # The save starts only once the rejudge holds the task, so it is the save
    # that waits.
    await held.wait()
    change = _with(acme, b"time_limit: 2", b"time_limit: 1")

    saved = await _save(setup, acme, entered.task, change, confirm=True)
    await rejudging

    assert isinstance(saved, Published) and saved.regraded == 1
    assert [(number, attempt) for number, attempt, *_ in await _attempts(setup)] == [
        (1, 1),
        (1, 2),
        (1, 3),
    ]
