"""The save that publishes. A valid save writes the organiser's files and one
plan per stage as one change and publishes exactly that change, numbered
after the last, with a note that reads back. A save another save
landed under is kept as a draft, and so, always, is a save asked to be. A
save inside `plans/` and a manager's change to an admin-only setting are
refused with nothing written. A save that does not check is a draft: its
files written, no publication, and its errors recomputed whenever the task's
state is read. What changed how the task grades is named, and while the
contest runs such a save asks to be confirmed.
"""

from collections.abc import Mapping
from typing import Any

import pytest

from forge.domain.content import Edit
from forge.domain.errors import (
    AdminOnly,
    ConfirmationRequired,
    Conflict,
    Forbidden,
    ReservedPath,
)
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import ContestId, TaskId
from forge.domain.plans import Plan
from forge.domain.roles import Role, Scope
from forge.domain.workflows import Visibility
from forge.runtime.setup import Setup
from forge.services import publications, tasks
from forge.services.access import Organiser
from forge.services.publications import Draft, Published
from forge.testing import CLASSIC, PRIMITIVES
from tests.services.conftest import SPRING, Acme, organiser

RUNNING = b"""\
name: Spring 2026
start: 2026-09-26T10:00:00Z
end: 2026-09-26T15:00:00Z
state: published
visibility: public
"""


async def _edits(acme: Acme, task: TaskId, contents: Mapping[str, bytes]) -> dict[str, Edit]:
    """Each file with the token it has now, as an editor would send it."""
    head = await acme.fake.content.list_files(PLATFORM, task)
    return {path: Edit(content, head.tokens.get(path)) for path, content in contents.items()}


async def _task_yaml(acme: Acme, task: TaskId) -> bytes:
    return acme.fake.state.repos[("acme", "spring.sum.task")].files["task.yaml"] if task else b""


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
    assert isinstance(result, Published)
    acme.fake.reset_calls()
    return result


async def test_a_valid_save_is_one_change_with_its_plans_and_that_change_is_published(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    result = await _save(setup, acme, sum_task, {"statement.md": b"Add two numbers.\n"})

    assert isinstance(result, Published)
    assert (result.number, result.grading_changed, result.changes) == (1, False, ())
    (saved,) = acme.fake.calls_to("save_files")
    assert saved.identity == acme.ada.identity
    assert saved.arguments["paths"] == ["plans/default.json", "statement.md"]
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    head = repo.history[-1]
    assert (head.author_id, head.message) == (7, "Save")
    assert repo.versions == {"published/1": head.version}
    plan = Plan.from_bytes(repo.files["plans/default.json"])
    assert [step.primitive for step in plan.steps] == [
        "compile@v1",
        "sandbox-run@v1",
        "diff-check@v1",
    ]
    assert plan.tests == ("1",)
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


async def test_a_stage_the_task_no_longer_has_loses_its_plan_in_the_same_change(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)
    staged = (
        await _task_yaml(acme, sum_task)
        + b"""
stages:
  - id: public
  - id: final
    trigger: at_end
"""
    )

    result = await _save(setup, acme, sum_task, {"task.yaml": staged})

    assert isinstance(result, Published)
    (saved,) = acme.fake.calls_to("save_files")
    assert saved.arguments["paths"] == [
        "plans/default.json",
        "plans/final.json",
        "plans/public.json",
        "task.yaml",
    ]
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    assert sorted(path for path in repo.files if path.startswith("plans/")) == [
        "plans/final.json",
        "plans/public.json",
    ]
    assert result.changes == (
        "plans/default.json removed",
        "plans/final.json added",
        "plans/public.json added",
    )


async def test_a_save_inside_plans_is_refused_and_writes_nothing(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    with pytest.raises(ReservedPath) as refused:
        await _save(
            setup,
            acme,
            sum_task,
            {"plans/default.json": b"{}", "statement.md": b"x", "plans": b"x"},
        )

    assert refused.value.code == "reserved_path"
    assert refused.value.extra["paths"] == ["plans", "plans/default.json"]
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
        ({"task.yaml": (b"submissions: 50", b"submissions: 5")}, ["limits"]),
        (
            {"task.yaml": (b"rate: 1 per 30s", b"rate: 2 per 30s"), "statement.md": b"Mine.\n"},
            ["limits", "statement.md"],
        ),
    ],
    ids=["statement", "name", "limits", "both"],
)
async def test_a_managers_admin_only_change_is_refused_naming_it_and_an_admins_is_not(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    manager: Organiser,
    contents: dict[str, Any],
    keys: list[str],
) -> None:
    task_yaml = await _task_yaml(acme, sum_task)
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
    task_yaml = await _task_yaml(acme, sum_task)
    statement = acme.fake.state.repos[("acme", "spring.sum.task")].files["statement.md"]

    result = await _save(
        setup,
        acme,
        sum_task,
        {
            "task.yaml": task_yaml.replace(b"value: 2.0", b"value: 3.0") + b"hidden: true\n",
            "statement.md": statement,
            "data/testcases/1.in": b"1 2\n",
        },
        manager,
    )

    assert isinstance(result, Published)
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


async def test_a_save_naming_a_missing_file_is_a_draft_and_the_state_recomputes_its_errors(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    published = await _published(setup, acme, sum_task)
    task_yaml = await _task_yaml(acme, sum_task)
    broken = task_yaml.replace(b"value: data/testcases/", b"value: data/hidden/")

    draft = await _save(setup, acme, sum_task, {"task.yaml": broken})

    assert isinstance(draft, Draft)
    assert draft.errors == (
        {
            "path": "inputs.setter[0].value",
            "message": "There is no file under data/hidden/ in the task.",
        },
    )
    assert draft.held_back == ()
    assert _written(acme) == ["save_files"]
    (saved,) = acme.fake.calls_to("save_files")
    assert saved.arguments["paths"] == ["task.yaml"]
    repo = acme.fake.state.repos[("acme", "spring.sum.task")]
    assert repo.files["task.yaml"] == broken
    assert repo.head == draft.version
    assert len(repo.versions) == 1

    state = await tasks.state(setup, acme.ada, sum_task)
    assert (state.draft, state.head, state.errors) == (True, draft.version, draft.errors)
    assert state.latest is not None
    assert state.latest.id == published.publication

    fixed = await _save(
        setup, acme, sum_task, {"data/hidden/1.in": b"1 2\n", "data/hidden/1.ans": b"3\n"}
    )
    assert isinstance(fixed, Published)
    assert fixed.number == 2
    later = await tasks.state(setup, acme.ada, sum_task)
    assert (later.draft, later.errors) == (False, ())


@pytest.mark.parametrize(
    ("change", "path", "named"),
    [
        (
            (b"workflow: unicon/classic@v1", b"workflow: unicon/classic@v9"),
            "workflow",
            "unicon/classic@v9",
        ),
        ((b"workflow: unicon/classic@v1", b"workflow: bob/mine@v1"), "workflow", "bob/mine@v1"),
        (
            (b"limits:", b"stages:\n  - id: one\n    workflow: unicon/gone@v1\nlimits:"),
            None,
            "unicon/gone@v1",
        ),
    ],
    ids=["no-such-version", "not-shared", "stage-override"],
)
async def test_a_workflow_the_organiser_cannot_read_is_an_error_at_its_path_naming_it(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    change: tuple[bytes, bytes],
    path: str | None,
    named: str,
) -> None:
    bob = AsUser(8, acme.fake.mint(8))
    mine = await acme.fake.workflows.create_workflow(
        bob, "bob", "mine", {"workflow.yaml": CLASSIC}, Visibility.PRIVATE
    )
    await acme.fake.workflows.create_workflow_version(bob, mine, "v1")
    task_yaml = await _task_yaml(acme, sum_task)

    draft = await _save(setup, acme, sum_task, {"task.yaml": task_yaml.replace(*change)})

    assert isinstance(draft, Draft)
    (error,) = draft.errors
    assert error["path"] == (path or "stages[0].workflow")
    assert "cannot be read" in error["message"]
    assert named in error["message"]
    assert acme.fake.calls_to("publish") == []
    reads = acme.fake.calls_to("read_workflow_file")
    assert {call.identity for call in reads} == {acme.ada.identity}


@pytest.mark.parametrize(
    ("use", "said"),
    [
        (
            b"unicon/compile@v7",
            "In unicon/custom@v1, steps[0].use: unicon/compile@v7 cannot be read: there is no "
            "such primitive or workflow at that version, or it is not shared with you.",
        ),
        (
            b"unicon/classic@v1",
            "In unicon/custom@v1, steps[0].use: unicon/classic@v1 is a workflow; using a "
            "workflow as a step comes with feature 10.",
        ),
        (
            b"unicon/odd@v1",
            "In unicon/custom@v1, steps[0].use: The primitive unicon/odd@v1 declares itself as "
            "unicon/compile@v1.",
        ),
    ],
    ids=["no-such-version", "a-workflow", "declares-another"],
)
async def test_a_step_whose_use_is_no_primitive_the_organiser_reads_is_an_error_naming_it(
    setup: Setup, acme: Acme, sum_task: TaskId, use: bytes, said: str
) -> None:
    acme.fake.primitives.add("odd", {"v1": PRIMITIVES["compile"]})
    custom = CLASSIC.replace(b"name: unicon/classic", b"name: unicon/custom").replace(
        b"use: unicon/compile@v1", b"use: " + use
    )
    made = await acme.fake.workflows.create_workflow(
        PLATFORM, "unicon", "custom", {"workflow.yaml": custom}, Visibility.PUBLIC
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, made, "v1")
    task_yaml = (await _task_yaml(acme, sum_task)).replace(
        b"workflow: unicon/classic@v1", b"workflow: unicon/custom@v1"
    )

    draft = await _save(setup, acme, sum_task, {"task.yaml": task_yaml})

    assert isinstance(draft, Draft)
    assert [(error["path"], error["message"]) for error in draft.errors] == [("workflow", said)]
    reads = acme.fake.calls_to("read_declaration")
    assert reads and {call.identity for call in reads} == {acme.ada.identity}
    assert acme.fake.calls_to("publish") == []


async def test_an_invalid_task_yaml_and_an_uncovered_input_are_drafts_with_their_paths(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    task_yaml = await _task_yaml(acme, sum_task)
    invalid = task_yaml.replace(b"type: number\n      value: 2.0", b"type: number\n      value: x")
    uncovered = task_yaml.replace(
        b"    - id: memory_limit\n      type: number\n      value: 256\n", b""
    )

    first = await _save(setup, acme, sum_task, {"task.yaml": invalid})
    second = await _save(setup, acme, sum_task, {"task.yaml": uncovered})

    assert isinstance(first, Draft) and isinstance(second, Draft)
    assert [error["path"] for error in first.errors] == ["inputs.setter[1].value"]
    assert [error["path"] for error in second.errors] == ["inputs.setter"]
    assert "memory_limit is given by neither side" in second.errors[0]["message"]
    assert acme.fake.calls_to("publish") == []


async def test_a_statement_only_save_leaves_the_flag_off_and_a_plan_change_sets_it(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)
    task_yaml = await _task_yaml(acme, sum_task)

    text = await _save(setup, acme, sum_task, {"statement.md": b"Clearer.\n"})
    plan = await _save(
        setup, acme, sum_task, {"task.yaml": task_yaml.replace(b"value: 2.0", b"value: 3.0")}
    )

    assert isinstance(text, Published) and isinstance(plan, Published)
    assert (text.grading_changed, text.changes) == (False, ())
    assert (plan.grading_changed, plan.changes) == (True, ("plans/default.json changed",))
    history = await publications.list(setup, acme.ada, sum_task)
    assert [(entry.number, entry.grading_changed) for entry in history] == [
        (1, False),
        (2, False),
        (3, True),
    ]
    assert history[2].changes == ("plans/default.json changed",)


async def test_a_named_data_file_and_a_limit_each_set_the_flag(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)

    added = await _save(
        setup, acme, sum_task, {"data/testcases/2.in": b"1 2\n", "data/testcases/2.ans": b"3\n"}
    )
    same = await _save(setup, acme, sum_task, {"data/testcases/2.in": b"1 2\n"})
    changed = await _save(setup, acme, sum_task, {"data/testcases/2.in": b"2 3\n"})
    elsewhere = await _save(setup, acme, sum_task, {"notes/idea.md": b"later\n"})
    task_yaml = await _task_yaml(acme, sum_task)
    limit = await _save(
        setup,
        acme,
        sum_task,
        {"task.yaml": task_yaml.replace(b"submissions: 50", b"submissions: 9")},
    )

    assert isinstance(added, Published) and isinstance(changed, Published)
    assert isinstance(same, Published) and isinstance(elsewhere, Published)
    assert isinstance(limit, Published)
    assert added.changes == (
        "plans/default.json changed",
        "data/testcases/2.ans added",
        "data/testcases/2.in added",
    )
    assert same.grading_changed is False
    assert changed.changes == ("data/testcases/2.in changed",)
    assert elsewhere.grading_changed is False
    assert limit.changes == ("limits.submissions changed",)


@pytest.fixture
async def running(setup: Setup, acme: Acme, sum_task: TaskId) -> TaskId:
    """The task published once, in a contest that is running now."""
    current = await acme.fake.content.read_file(PLATFORM, SPRING, "contest.yaml")
    await acme.fake.content.write_file(
        PLATFORM, SPRING, "contest.yaml", RUNNING, message="Run", expected=current.token
    )
    await _published(setup, acme, sum_task)
    return sum_task


async def test_a_grading_change_while_the_contest_runs_asks_first_and_writes_nothing(
    setup: Setup, acme: Acme, running: TaskId
) -> None:
    task_yaml = await _task_yaml(acme, running)
    faster = {"task.yaml": task_yaml.replace(b"value: 2.0", b"value: 1.0")}

    with pytest.raises(ConfirmationRequired) as asked:
        await _save(setup, acme, running, faster)

    assert asked.value.code == "confirmation_required"
    assert asked.value.extra["changes"] == ["plans/default.json changed"]
    assert _written(acme) == []
    confirmed = await _save(setup, acme, running, faster, confirm=True)
    assert isinstance(confirmed, Published)
    assert (confirmed.number, confirmed.grading_changed) == (2, True)


async def test_a_grading_change_kept_as_a_draft_is_written_and_not_published(
    setup: Setup, acme: Acme, running: TaskId
) -> None:
    task_yaml = await _task_yaml(acme, running)

    kept = await _save(
        setup,
        acme,
        running,
        {"task.yaml": task_yaml.replace(b"value: 2.0", b"value: 1.0")},
        keep_as_draft=True,
    )

    assert isinstance(kept, Draft)
    assert (kept.errors, kept.held_back) == ((), ("plans/default.json changed",))
    assert _written(acme) == ["save_files"]
    (saved,) = acme.fake.calls_to("save_files")
    assert saved.arguments["paths"] == ["task.yaml"]
    state = await tasks.state(setup, acme.ada, running)
    assert (state.draft, state.errors) == (True, ())
    published = await publications.save(setup, acme.ada, running, {}, confirm=True)
    assert isinstance(published, Published)
    assert published.changes == ("plans/default.json changed",)


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


async def test_a_text_only_save_while_the_contest_runs_publishes_without_asking(
    setup: Setup, acme: Acme, running: TaskId
) -> None:
    result = await _save(setup, acme, running, {"statement.md": b"Typo fixed.\n"})

    assert isinstance(result, Published)
    assert result.grading_changed is False


async def test_outside_a_running_contest_a_grading_change_publishes_without_asking(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task)
    task_yaml = await _task_yaml(acme, sum_task)

    result = await _save(
        setup, acme, sum_task, {"task.yaml": task_yaml.replace(b"value: 2.0", b"value: 1.0")}
    )

    assert isinstance(result, Published)
    assert result.grading_changed is True
    contest_reads = [
        call.identity
        for call in acme.fake.calls_to("read_file")
        if call.arguments["place"] == ContestId("acme/spring")
    ]
    assert contest_reads == [PLATFORM]
