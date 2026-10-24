"""A task whose workflow holds boxes (stage 6A.3): it saves and plans as the
flat classic task does, but for step keys and pins; a box's steps split
across the pipelines; taint and the time ceiling are counted on the flat
list; a new inner version asks first; the door serves an inner file from
its own pin; and a renamed inner workflow is refused.
"""

import hashlib
import json
from collections.abc import Mapping

import pytest

from forge.db.tables import Grading
from forge.domain.content import Edit
from forge.domain.errors import ConfirmationRequired
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId, WorkflowId
from forge.domain.plan_basics import PLAN_PATH, Snapshot
from forge.domain.run_plan import RunPlan, steps_run_again
from forge.domain.step_data import item_key
from forge.domain.workflows import Visibility
from forge.runtime.setup import Setup
from forge.services import steps, task_save
from forge.services.task_save import Draft, Published
from forge.testing import PLACEHOLDER_DIGEST
from tests.conftest import sibling
from tests.services.conftest import Acme, make_task, write_contest
from tests.services.test_run_task_saves import DRAFT, ISLANDS_DATA, _primitives, checked
from tests.services.test_steps import (
    Islands,
    _batch,
    _compiled,
    _download,
    _grading,
    _job,
    _mint,
    _report,
    _row,
    _step,
)
from tests.services.test_workflow_boxes import BUILD, JUDGE, OUTER
from tests.test_run_task import ISLANDS

DIFF = sibling("deploy", "workflows/classic/v3")
TASK = ISLANDS.replace("unicon/classic@v3", "bob/outer@v1")
BANNER = OUTER.replace(
    "steps:\n  - id: build\n",
    "steps:\n"
    "  - id: banner\n"
    "    use: unicon/compile@v3\n"
    "    with:\n"
    "      source: ${{ inputs.code }}\n"
    "      language: ${{ inputs.language }}\n"
    "  - id: build\n",
).replace("program: ${{ steps.build.program }}", "program: ${{ steps.banner.program }}")
LINK = (
    f"image: ghcr.io/uniconhq/primitive-link@{PLACEHOLDER_DIGEST}\n"
    "schema_version: 6\n"
    "limits: {time_ms: 10000, cpu_ms: 10000, memory_mb: 512, pids: 64, output_mb: 64, gpus: 0}\n"
    "inputs:\n"
    "  source: {type: folder, runs: true}\n"
    "  grader: {type: folder, runs: false}\n"
    "outputs:\n"
    "  program: file\n"
    "  outcome: outcome\n"
).encode()
JUDGE_LINK = """\
inputs:
  code: {type: folder}
  grader: {type: folder}
steps:
  - id: compare
    use: unicon/link@v1
    with:
      source: ${{ inputs.code }}
      grader: ${{ inputs.grader }}
outputs:
  program: ${{ steps.compare.program }}
"""
OUTER_LINK = """\
inputs:
  code: {type: folder, title: Solution}
  grader: {type: folder, title: Grader}
  input: {type: file, title: Input}
steps:
  - id: judge
    use: bob/judge@v1
    with:
      code: ${{ inputs.code }}
      grader: ${{ inputs.grader }}
  - id: run
    use: unicon/exec@v1
    with:
      program: ${{ steps.judge.program }}
      stdin: ${{ inputs.input }}
      time_limit: 1
      memory_limit: 64
reports:
  time_ms: ${{ steps.run.time_ms }}
"""
LINK_TASK = """\
name: Linked
workflow: bob/outer@v1
inputs:
  code: entrant
  grader: grader/
repeat:
  - inputs: [input]
    tuples: [samples/1.in, tests/2.in]
scoring:
  groups:
    shown: {input: [samples/1.in]}
  values:
    fast: {block: max, over: {runs: shown}, of: {run: [shown, time_ms]}}
  show:
    now: [shown, samples/]
    after_close: [fast, all]
"""
SLOW = """\
inputs:
  program: {type: file}
  input: {type: file}
  time_limit: {type: number}
steps:
  - id: solution
    use: unicon/exec@v1
    with:
      program: ${{ inputs.program }}
      stdin: ${{ inputs.input }}
      time_limit: ${{ inputs.time_limit }}
      memory_limit: 64
outputs:
  time_ms: ${{ steps.solution.time_ms }}
"""
SLOW_OUTER = """\
inputs:
  code: {type: folder, title: Solution}
  input: {type: file, title: Input}
  time_limit: {type: number, title: Time limit, unit: seconds}
steps:
  - id: compile
    use: unicon/compile@v3
    with:
      source: ${{ inputs.code }}
      language: python
  - id: run
    use: bob/slow@v1
    with:
      program: ${{ steps.compile.program }}
      input: ${{ inputs.input }}
      time_limit: ${{ inputs.time_limit }}
reports:
  time_ms: ${{ steps.run.time_ms }}
"""
SLOW_TASK = """\
name: Slow
workflow: bob/outer@v1
inputs:
  code: entrant
  time_limit: 400
repeat:
  - inputs: [input]
    tuples: [tests/1.in, tests/2.in]
scoring:
  show:
    after_close: [all]
"""


def _diff() -> dict[str, bytes]:
    return {
        "diff/diff.py": (DIFF / "diff/diff.py").read_bytes(),
        "diff/program.yaml": (DIFF / "diff/program.yaml").read_bytes(),
    }


async def _public(
    acme: Acme, name: str, text: str, extra: Mapping[str, bytes] | None = None, version: str = "v1"
) -> WorkflowId:
    made = await acme.fake.workflows.create_workflow(
        PLATFORM,
        "bob",
        name,
        {"workflow.yaml": text.encode(), **(extra or {})},
        Visibility.PUBLIC,
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, made, version)
    return made


def _plan(acme: Acme, task: TaskId) -> RunPlan:
    repo = acme.fake.state.repos[("acme", f"spring.{task.rsplit('/', 1)[1]}.task")]
    return RunPlan.from_bytes(repo.files[PLAN_PATH])


def _woodpecker(plan: RunPlan) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
    """Each pipeline's Woodpecker steps: the name, and the names it waits for."""
    return {
        kind: [
            (
                plan.woodpecker_name(step.name),
                tuple(plan.woodpecker_name(name) for name in step.depends_on),
            )
            for step in steps
        ]
        for kind, steps in plan.pipelines.items()
    }


async def _publish(
    setup: Setup, acme: Acme, name: str, text: str, files: Mapping[str, bytes]
) -> tuple[TaskId, Published]:
    await write_contest(acme.fake, DRAFT)
    task = await make_task(setup, acme, name)
    head = await acme.fake.content.list_files(PLATFORM, task)
    edits = {
        "task.yaml": Edit(text.encode(), head.tokens.get("task.yaml")),
        **{path: Edit(content, None) for path, content in files.items()},
    }
    try:
        found = await task_save.save(setup, acme.ada, task, edits, confirm=True)
    except ConfirmationRequired as asked:
        found = await task_save.save(
            setup, acme.ada, task, edits, confirm=True, asked=asked.extra["asked"]
        )
    assert isinstance(found, Published), found
    return task, found


async def _classic(setup: Setup, acme: Acme, name: str = "boxed") -> tuple[TaskId, Published]:
    _primitives(acme)
    await _public(acme, "build", BUILD)
    await _public(acme, "judge", JUDGE, _diff())
    await _public(acme, "outer", OUTER)
    return await _publish(setup, acme, name, TASK, ISLANDS_DATA)


async def test_a_boxed_classic_task_saves_and_plans_as_the_flat_one(
    setup: Setup, acme: Acme, spring: str
) -> None:
    task, _ = await _classic(setup, acme)

    plan = _plan(acme, task)
    assert [
        plan.steps[name].woodpecker
        for name in (
            "build/compile",
            "solution",
            "judge/diff",
            "judge/compare",
        )
    ] == ["s1", "s2", "s3", "s4"]
    assert _woodpecker(plan) == {
        "save": [("s3", ())],
        "early": [("s1", ()), ("s2", ("s1",)), ("s4", ("s2",))],
        "late": [("s2", ()), ("s4", ("s2",))],
    }
    # The checker's compile is the save, and its compare the grading, so one
    # box's steps are split across the three pipelines.
    assert [step.step for step in plan.pipeline("save")] == ["judge/diff"]
    assert [step.step for step in plan.pipeline("early")] == [
        "build/compile",
        "solution",
        "judge/compare",
    ]
    assert [step.step for step in plan.pipeline("late")] == ["solution", "judge/compare"]
    assert [pin.ref for pin in plan.inner_workflows] == ["bob/build@v1", "bob/judge@v1"]
    assert plan.workflow.files == {}
    assert set(plan.inner_workflows[1].files) == {"diff/diff.py", "diff/program.yaml"}
    compare = (plan.steps["judge/compare"].items or {})["r1"].inputs
    assert compare["manifest"] == {"workflow": "diff/program.yaml", "pin": 2}
    diff = plan.steps["judge/diff"].inputs
    compile_step = plan.steps["build/compile"].inputs
    assert diff is not None and compile_step is not None
    assert diff["source"] == {"workflow": "diff/", "pin": 2}
    assert compile_step == {
        "source": {"entrant": "code"},
        "language": {"entrant": "language"},
    }
    assert plan.reports["score"].step == "judge/compare"
    (publication,) = await acme.fake.workspaces.list_publications(task)
    assert set(publication.workflows) == {"bob/outer", "bob/build", "bob/judge"}


async def test_taint_through_a_box_names_the_flat_step(
    setup: Setup, acme: Acme, spring: str
) -> None:
    _primitives(acme)
    acme.fake.primitives.add("link", {"v1": LINK})
    await _public(acme, "judge", JUDGE_LINK)
    await _public(acme, "outer", OUTER_LINK)
    await write_contest(acme.fake, DRAFT)
    task = await make_task(setup, acme, "linked")
    head = await acme.fake.content.list_files(PLATFORM, task)
    found = await task_save.save(
        setup,
        acme.ada,
        task,
        {
            "task.yaml": Edit(LINK_TASK.encode(), head.tokens.get("task.yaml")),
            "grader/grader.h": Edit(b"int grade();\n", None),
            "samples/1.in": Edit(b"1\n", None),
            "tests/2.in": Edit(b"2\n", None),
        },
    )

    assert isinstance(found, Draft), found
    assert any("judge/compare" in error["message"] for error in found.errors)


async def test_a_step_added_at_the_top_regrades_only_what_reads_it(
    setup: Setup, acme: Acme, spring: str
) -> None:
    task, _ = await _classic(setup, acme)
    first = _plan(acme, task).to_bytes()
    repo = acme.fake.state.repos[("bob", "outer.workflow")]
    acme.fake.state.commit(repo, {"workflow.yaml": BANNER.encode()}, "banner", 8)
    await acme.fake.workflows.create_workflow_version(PLATFORM, WorkflowId("bob/outer"), "v2")
    await _publish_over(setup, acme, task, TASK.replace("bob/outer@v1", "bob/outer@v2"))

    plan = _plan(acme, task)
    again = steps_run_again(
        Snapshot(plans={PLAN_PATH: first}, data={}),
        Snapshot(plans={PLAN_PATH: plan.to_bytes()}, data={}),
    )
    assert again == ("banner", "solution", "judge/compare")
    before = RunPlan.from_bytes(first)
    assert before.steps["build/compile"].inputs == plan.steps["build/compile"].inputs
    assert before.steps["judge/diff"].inputs == plan.steps["judge/diff"].inputs
    assert item_key(
        before.steps["build/compile"].image, before.steps["build/compile"].inputs or {}
    ) == item_key(plan.steps["build/compile"].image, plan.steps["build/compile"].inputs or {})
    assert before.steps["build/compile"].woodpecker == "s1"
    assert plan.steps["build/compile"].woodpecker == "s2"
    assert plan.steps["solution"].woodpecker == "s3"
    assert set(plan.steps) == set(before.steps) | {"banner"}


async def test_a_new_inner_version_asks_first_and_names_that_workflow(
    setup: Setup, acme: Acme, spring: str
) -> None:
    task, published = await _classic(setup, acme)
    await checked(setup, task)
    row = await _grading(setup, acme, Islands(task, published.publication, WorkflowId("bob/outer")))
    async with setup.unit_of_work() as ctx:
        found = await ctx.db.get(Grading, row)
        assert found is not None
        found.pipeline = "early"
        found.status = GradingStatus.DONE
    judge = acme.fake.state.repos[("bob", "judge.workflow")]
    changed = _diff()
    changed["diff/diff.py"] += b"# moved\n"
    acme.fake.state.commit(judge, {"workflow.yaml": JUDGE.encode(), **changed}, "moved", 8)
    await acme.fake.workflows.create_workflow_version(PLATFORM, WorkflowId("bob/judge"), "v2")
    outer = OUTER.replace("bob/judge@v1", "bob/judge@v2")
    repo = acme.fake.state.repos[("bob", "outer.workflow")]
    acme.fake.state.commit(repo, {"workflow.yaml": outer.encode()}, "judge", 8)
    await acme.fake.workflows.create_workflow_version(PLATFORM, WorkflowId("bob/outer"), "v2")
    head = await acme.fake.content.list_files(PLATFORM, task)
    edits = {
        "task.yaml": Edit(
            TASK.replace("bob/outer@v1", "bob/outer@v2").encode(), head.tokens["task.yaml"]
        )
    }

    with pytest.raises(ConfirmationRequired) as asked:
        await task_save.save(setup, acme.ada, task, edits)

    assert task_save.PIN_MOVED in asked.value.detail
    assert any("bob/judge@v2" in change for change in asked.value.extra["changes"])


async def test_the_door_serves_an_inner_workflows_file_from_its_pin(
    setup: Setup, acme: Acme, spring: str
) -> None:
    task, published = await _classic(setup, acme, "door")
    held = Islands(task, published.publication, WorkflowId("bob/outer"))
    plan = _plan(acme, task)
    judge = plan.inner_workflows[1]
    saved = await _grading(setup, acme, held)
    await _step(setup, acme, saved, "save", "judge/diff", _compiled())
    status = await _report(setup, saved, "save")
    assert status is GradingStatus.DONE, (await _row(setup, saved)).error
    row = await _grading(setup, acme, held)
    await _step(setup, acme, row, "early", "build/compile", _compiled())
    await _step(setup, acme, row, "early", "solution", _batch())
    acme.fake.reset_calls()
    access = await _mint(setup, row, "early", "judge/compare")
    job = await _job(setup, row, "judge/compare", access)
    compare = job["inputs"]["batch"][0]["inputs"]
    manifest = _download(job, compare["manifest"]["file"])
    sha256 = manifest["url"].rpartition("/files/")[2]
    served = await steps.step_file(
        setup,
        row,
        access.data_url.rstrip("/").rsplit("/", 1)[-1],
        sha256,
        f"Bearer {access.token}",
    )

    reads = [
        call
        for call in acme.fake.calls_to("read_workflow_at")
        if call.arguments["path"] == "diff/program.yaml"
    ]
    assert reads
    assert all(call.arguments["workflow"] == judge.id for call in reads)
    assert all(str(call.arguments["at"]) == judge.commit for call in reads)
    assert hashlib.sha256(served).hexdigest() == sha256
    assert "reports" in json.loads(served)


async def test_an_inner_workflow_renamed_under_its_owner_is_refused(
    setup: Setup, acme: Acme, spring: str
) -> None:
    task, _ = await _classic(setup, acme, "renamed")
    await checked(setup, task)
    (publication,) = await acme.fake.workspaces.list_publications(task)
    assert "bob/judge" in publication.workflows
    del acme.fake.state.repos[("bob", "judge.workflow")]
    await _public(acme, "judge", JUDGE, _diff())
    head = await acme.fake.content.list_files(PLATFORM, task)
    current = await acme.fake.content.read_file(PLATFORM, task, "task.yaml")
    again = await task_save.save(
        setup, acme.ada, task, {"task.yaml": Edit(current.content, head.tokens["task.yaml"])}
    )

    assert isinstance(again, Draft), again
    assert any(
        error["path"] == "workflow" and "bob/judge is now a different workflow" in error["message"]
        for error in again.errors
    )


async def test_the_time_ceiling_counts_steps_inside_a_box(
    setup: Setup, acme: Acme, spring: str
) -> None:
    _primitives(acme)
    await _public(acme, "slow", SLOW)
    await _public(acme, "outer", SLOW_OUTER)
    await write_contest(acme.fake, DRAFT)
    task = await make_task(setup, acme, "slow")
    head = await acme.fake.content.list_files(PLATFORM, task)
    found = await task_save.save(
        setup,
        acme.ada,
        task,
        {
            "task.yaml": Edit(SLOW_TASK.encode(), head.tokens.get("task.yaml")),
            "tests/1.in": Edit(b"1\n", None),
            "tests/2.in": Edit(b"2\n", None),
        },
    )

    assert isinstance(found, Draft), found
    messages = [error["message"] for error in found.errors]
    assert any("run/solution" in message for message in messages), messages
    assert any("at most 25" in message for message in messages)


async def _publish_over(setup: Setup, acme: Acme, task: TaskId, text: str) -> Published:
    head = await acme.fake.content.list_files(PLATFORM, task)
    edits = {"task.yaml": Edit(text.encode(), head.tokens["task.yaml"])}
    try:
        found = await task_save.save(setup, acme.ada, task, edits, confirm=True)
    except ConfirmationRequired as asked:
        found = await task_save.save(
            setup, acme.ada, task, edits, confirm=True, asked=asked.extra["asked"]
        )
    assert isinstance(found, Published), found
    return found
