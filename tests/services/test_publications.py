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

import json
from collections.abc import Mapping
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
    ReservedPath,
)
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import ContestId, TaskId
from forge.domain.plans import Plan
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.submissions import SubmittedInput
from forge.domain.workflows import Visibility
from forge.runtime.setup import Setup
from forge.services import publications, submissions, tasks
from forge.services.access import Organiser
from forge.services.publications import Draft, Published
from forge.testing import CLASSIC, PRIMITIVES
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


async def _edits(acme: Acme, task: TaskId, contents: Mapping[str, bytes]) -> dict[str, Edit]:
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
    contents: Mapping[str, bytes],
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
    """The task's first publication, a save of the starter as it is."""
    result = await _save(setup, acme, task, {"statement.md": b"Add two numbers.\n"})
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
    assert (result.number, result.grading_changed, result.changes, result.notes) == (
        1,
        False,
        (),
        (),
    )
    (saved,) = acme.fake.calls_to("save_files")
    assert saved.identity == acme.ada.identity
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
    assert saved.identity == manager.identity


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
            "message": "There is no folder tests/samples/ with a test in it.",
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
        ({"tests/main/2/input": b"2 2\n"}, "tests/main/2/", "The test main/2 has no answer."),
        (
            {"tests/main/1/notes": b"x\n"},
            "tests/main/1/notes",
            "The test main/1 has an entry for no field: notes.",
        ),
        (
            {"tests/main/1/answer.txt": b"3\n"},
            "tests/main/1/",
            "The test main/1 gives answer twice: answer, answer.txt.",
        ),
        (
            {"tests/other/1/input": b"1 2\n", "tests/other/1/answer": b"3\n"},
            "tests/other/",
            "other is a folder of tests/ but not a group in test_groups: list it, or move its "
            "tests.",
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
    assert "cannot be read" in error["message"]
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
        "The workflow unicon/old@v1 is not in the current format, so its owner tags a new version: "
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
    assert all(error["message"].startswith("In unicon/broken@v1, ") for error in draft.errors)


@pytest.mark.parametrize(
    ("use", "said"),
    [
        (
            b"unicon/compile@v7",
            "In unicon/custom@v1, steps[0].use: unicon/compile@v7 cannot be read: there is no "
            "such primitive or workflow at that version, or it is not shared with you.",
        ),
        (
            b"unicon/classic@v2",
            "In unicon/custom@v1, steps[0].use: unicon/classic@v2 is not a primitive: a step "
            "uses a primitive, never a workflow.",
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
        "In unicon/custom@v1, steps[0].use: The primitive unicon/odd@v1 is not in the current "
        "format; use a later version of it: "
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
        CLASSIC.replace(b"  time_limit: number\n", b"  time_limit: number\n  args: text\n").replace(
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
        ("inputs.args", "The org holds no secret named k.")
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
        (True, ("plans/plan.json changed",)),
        (True, ("tests/main/1/input changed",)),
        (False, ()),
        (
            True,
            (
                "plans/plan.json changed",
                "tests/main/2/answer added",
                "tests/main/2/input added",
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
    assert history[5].changes == ("plans/plan.json changed",)


@pytest.fixture
async def running(setup: Setup, acme: Acme, sum_task: TaskId) -> TaskId:
    """The task published once, in a contest that has started."""
    await write_contest(acme.fake, RUNNING.format(state="published"))
    await _published(setup, acme, sum_task)
    return sum_task


@pytest.mark.parametrize(
    ("change", "listed"),
    [
        ((b"time_limit: 2", b"time_limit: 1"), ["plans/plan.json changed"]),
        (None, ["tests/main/1/input changed"]),
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
    assert asked.value.extra["changes"] == listed
    assert _written(acme) == []
    confirmed = await _save(setup, acme, running, files, confirm=True)
    assert isinstance(confirmed, Published)
    assert (confirmed.number, confirmed.grading_changed, list(confirmed.changes)) == (
        2,
        True,
        listed,
    )


async def test_a_scoring_change_once_the_contest_has_started_publishes_without_asking(
    setup: Setup, acme: Acme, running: TaskId
) -> None:
    rules = await _save(setup, acme, running, _with(acme, b"{each: 100}", b"{each: 60, pass: 40}"))
    caps = await _save(
        setup, acme, running, _with(acme, b"test_groups:", b"submissions: {max: 3}\ntest_groups:")
    )
    text = await _save(setup, acme, running, {"statement.md": b"Typo fixed.\n"})

    for result in (rules, caps, text):
        assert isinstance(result, Published), result
        assert (result.grading_changed, result.changes) == (False, ())


async def test_a_grading_change_kept_as_a_draft_is_written_and_not_published(
    setup: Setup, acme: Acme, running: TaskId
) -> None:
    kept = await _save(
        setup, acme, running, _with(acme, b"time_limit: 2", b"time_limit: 1"), keep_as_draft=True
    )

    assert isinstance(kept, Draft)
    assert (kept.errors, kept.held_back) == ((), ("plans/plan.json changed",))
    assert _written(acme) == ["save_files"]
    (saved,) = acme.fake.calls_to("save_files")
    assert saved.arguments["paths"] == ["task.yaml"]
    state = await tasks.state(setup, acme.ada, running)
    assert (state.draft, state.errors) == (True, ())
    published = await publications.save(setup, acme.ada, running, {}, confirm=True)
    assert isinstance(published, Published)
    assert published.changes == ("plans/plan.json changed",)


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


async def _graded(setup: Setup, acme: Acme, entered: Entered) -> None:
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
        idempotency_key="key-0001-aaaa",
    )
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Grading).values(status=GradingStatus.DONE))
    acme.fake.reset_calls()


@pytest.mark.parametrize(
    ("before", "after", "refused"),
    [
        (b"{each: 100}", b"{each: 100, show: after_close}", "main was shown always"),
        (b"{each: 100}", b"{pass: 100, show: verdict}", "main was shown always"),
        (
            b"{pass: 100, show: verdict}",
            b"{pass: 100, show: after_close}",
            "main was shown verdict",
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
        first = await _save(setup, acme, entered.task, _with(acme, b"{each: 100}", before))
        assert isinstance(first, Published), first
    await _graded(setup, acme, entered)

    result = await _save(setup, acme, entered.task, _with(acme, before, after))

    if refused is None:
        assert isinstance(result, Published), result
        return
    assert isinstance(result, Draft)
    assert [(error["path"], error["message"]) for error in result.errors] == [
        ("test_groups.main.show", f"{refused}: what was shown cannot be hidden again.")
    ]
    assert acme.fake.calls_to("publish") == []


async def test_before_any_grading_is_done_a_group_may_be_hidden(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    result = await _save(
        setup, acme, entered.task, _with(acme, b"{each: 100}", b"{each: 100, show: after_close}")
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
            "large is new and states no show: say always to show it at once, or verdict or "
            "after_close.",
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
            "large was shown always: what was shown cannot be hidden again.",
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


async def test_a_published_grading_change_regrades_every_submission_and_a_scoring_one_none(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _graded(setup, acme, entered)
    (graded,) = await _attempts(setup)

    scoring = await _save(setup, acme, entered.task, _with(acme, b"{each: 100}", b"{pass: 100}"))
    unchanged = await _attempts(setup)
    grading = await _save(
        setup, acme, entered.task, _with(acme, b"time_limit: 2", b"time_limit: 1"), confirm=True
    )

    assert isinstance(scoring, Published) and isinstance(grading, Published)
    assert (scoring.regraded, unchanged) == (0, [graded])
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
        (WORTH, "contest.yaml tasks[0].worth gives sum worth 50"),
        (DUE, "contest.yaml tasks[0].due gives sum a due"),
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
            f"No group has a rule weight, so sum gives no points, and {line}; remove it first.",
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
    assert [(field.name, field.type) for field in form.test] == [
        ("input", "file"),
        ("answer", "file"),
    ]
    reads = acme.fake.calls_to("read_workflow_file")
    assert {call.identity for call in reads} == {acme.ada.identity}


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
        (b"", "names no workflow"),
        (b"workflow: [\n", "does not read"),
        (b"workflow: classic\n", "must end in @ and a version"),
        (b"workflow: unicon/nothing@v1\n", "cannot be read"),
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
    assert form.problem is not None and "not in the current format" in form.problem
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
