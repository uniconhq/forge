"""A run task's plan (SIMPLER-CORE.md 11.1 to 11.4, 15.4 and 15.5): islands
reads step by step as written; each step is placed by what it reads; a
grading is an early and a late pipeline, never mixed; taint follows a
hidden file into the steps that run the entrant's code beside it, and the
save refuses or notes what is shown during the contest over such runs; each
container holds the files it copies; and what a save changes is said.
"""

import hashlib
from collections.abc import Mapping
from typing import Any

import pytest

from forge.domain import exact_json
from forge.domain.content import named_by
from forge.domain.contracts import violation
from forge.domain.definitions import DEFAULT_MAX_SIZE, parse_task
from forge.domain.grading import PLATFORM_MACHINE, Machine, Slot, run_machine
from forge.domain.plans import PLAN_PATH, Snapshot, StepLimits
from forge.domain.primitives import PrimitiveDeclaration, parse_primitive
from forge.domain.run_plan import (
    PRIMITIVE_ROOM_MB,
    FileFact,
    PipelineStep,
    RunPlan,
    RunPlanCheck,
    files_named,
    order_problems,
    plan_runs,
    run_plan_changes,
    side_by_side,
    slot_of,
    steps_run_again,
)
from forge.domain.run_task import check_run_task
from forge.domain.run_workflow import WorkflowFiles, check_run_workflow
from forge.domain.workflow_definition import parse_run_workflow
from forge.testing import PLACEHOLDER_DIGEST
from tests.test_program_manifest import primitive
from tests.test_run_task import (
    CARTPOLE,
    CARTPOLE_FILES,
    CARTPOLE_TASK,
    ISLANDS,
    ISLANDS_PATHS,
    JUDGE_MANIFEST,
    JUDGED,
    JUDGED_PATHS,
    JUDGED_TASK,
    TOUR,
    TOUR_FILES,
    TOUR_PATHS,
    TOUR_TASK,
    deployed,
    mirrored,
)

COMMIT = "c" * 40


def _declared(name: str, inputs: str, outputs: str) -> PrimitiveDeclaration:
    return parse_primitive(
        f"""\
schema_version: 6
image: ghcr.io/acme/{name}@{PLACEHOLDER_DIGEST}
limits: {{time_ms: 10000, cpu_ms: 10000, memory_mb: 512, pids: 64, output_mb: 64, gpus: 0}}
inputs:
{inputs}
outputs:
{outputs}
"""
    )


LINK = _declared(
    "link",
    "  source: {type: folder, runs: true}\n  grader: {type: folder, runs: false}\n",
    "  program: file\n  outcome: outcome\n",
)
"""A compile that links the entrant's source with a grader beside it."""
JUDGE = _declared(
    "judge",
    "  program: {type: file, runs: true}\n"
    "  key: {type: file, runs: false, secret: true, optional: true}\n"
    "  data: {type: file, runs: false, optional: true}\n",
    "  score: number\n  outcome: outcome\n",
)
"""A primitive that runs a program and keeps one file secret from it."""
LINKC = _declared(
    "linkc",
    "  source: {type: folder, runs: true}\n  grader: {type: folder, runs: false}\n",
    "  program: file\n"
    "  outcome: {type: outcome, outcomes: {accepted: it links, compile_error: it does not}}\n",
)
"""`LINK`, which can give a compile error."""


@pytest.fixture(scope="module")
def primitives() -> dict[str, PrimitiveDeclaration]:
    return {
        "unicon/exec@v1": mirrored("exec"),
        "unicon/interact@v2": mirrored("interact"),
        "unicon/compile@v3": mirrored("compile"),
        "acme/link@v1": LINK,
        "acme/judge@v1": JUDGE,
        "acme/linkc@v1": LINKC,
    }


def planned(
    primitives: Mapping[str, PrimitiveDeclaration],
    task: str,
    workflow: str,
    files: Mapping[str, bytes],
    paths: frozenset[str],
    *,
    sizes: Mapping[str, int] | None = None,
    manifests: Mapping[str, bytes] | None = None,
    machine: Machine = PLATFORM_MACHINE,
) -> RunPlanCheck:
    """The plan of `task` over `workflow` and its `files`, each task file
    in `paths` 3 bytes long but those `sizes` gives, and hashed as if it held
    its path.
    """
    definition = parse_task(task, runs=True)
    parsed = parse_run_workflow(workflow)
    checked = check_run_workflow(parsed, primitives, WorkflowFiles(frozenset(files), files))
    assert checked.problems == []
    found = check_run_task(
        definition,
        parsed,
        checked,
        paths,
        manifests or {},
        previous=None,
        submitted=False,
        secrets=frozenset(),
        machine=machine,
    )
    assert found.problems == [] and found.task is not None
    named = files_named(parsed, found.task)
    task_files = {
        path: FileFact(size=(sizes or {}).get(path, 3), sha256=_sha256(path.encode()))
        for entry in named.task
        for path in named_by(paths, entry)
    }
    workflow_files = {
        path: FileFact(size=len(files[path]), sha256=_sha256(files[path]))
        for entry in named.workflow
        for path in named_by(files, entry)
    }
    return plan_runs(
        definition,
        parsed,
        checked,
        found.task,
        ref=str(definition.workflow),
        workflow_id="w1",
        commit=COMMIT,
        task_files=task_files,
        workflow_files=workflow_files,
        machine=machine,
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def ok(found: RunPlanCheck) -> RunPlan:
    assert found.problems == []
    assert found.plan is not None
    return found.plan


def refused(found: RunPlanCheck) -> dict[str, list[str]]:
    assert found.plan is None
    problems: dict[str, list[str]] = {}
    for problem in found.problems:
        problems.setdefault(problem["path"], []).append(problem["message"])
    return problems


def islands(
    primitives: Mapping[str, PrimitiveDeclaration], task: str = ISLANDS, **kw: Any
) -> RunPlanCheck:
    workflow, files = deployed("unicon/classic@v3")
    return planned(primitives, task, workflow, files, ISLANDS_PATHS, **kw)


def laid_out(plan: RunPlan) -> dict[str, list[tuple[str, tuple[str, ...] | None, tuple[str, ...]]]]:
    """Each pipeline's Woodpecker steps: name, runs and what each waits for."""
    return {
        kind: [(step.name, step.runs, step.depends_on) for step in steps]
        for kind, steps in plan.pipelines.items()
    }


def held(plan: RunPlan, name: str, kind: str = "late") -> PipelineStep:
    return next(step for step in plan.pipeline(kind) if step.name == name)  # type: ignore[arg-type]


# Islands, step by step


def test_islands_reads_as_written_step_by_step(primitives: dict[str, PrimitiveDeclaration]) -> None:
    plan = ok(islands(primitives))

    assert {step: each.class_ for step, each in plan.steps.items()} == {
        "compile": "grading_once",
        "solution": "grading_per_run",
        "diff": "save_once",
        "compare": "grading_per_run",
    }
    assert laid_out(plan) == {
        "save": [("diff", None, ())],
        "early": [
            ("compile", None, ()),
            ("solution", ("r1", "r2"), ("compile",)),
            ("compare", ("r1", "r2"), ("solution",)),
        ],
        "late": [
            ("solution", ("r3", "r4"), ()),
            ("compare", ("r3", "r4"), ("solution",)),
        ],
    }
    assert plan.steps["diff"].inputs == {
        "source": {"workflow": "diff/"},
        "language": {"value": "python"},
    }
    assert plan.steps["compile"].inputs == {
        "source": {"entrant": "code"},
        "language": {"entrant": "language"},
    }
    solution = plan.steps["solution"].items or {}
    assert solution["r3"].inputs == {
        "program": {"step": "compile", "output": "program"},
        "stdin": {"task": "tests/3.in"},
        "time_limit": {"value": 2},
        "memory_limit": {"value": 256},
    }
    compare = plan.steps["compare"].items or {}
    assert compare["r1"].inputs == {
        "program": {"step": "diff", "output": "program"},
        "manifest": {"workflow": "diff/program.yaml"},
        "time_limit": {"value": 3},
        "memory_limit": {"value": 256},
        "files": {
            "files": {
                "output": {"step": "solution", "output": "output"},
                "answer": {"task": "samples/1.ans"},
            }
        },
    }
    # The samples' runs see only what entrants are shown; the tests' runs
    # run the entrant's code on their own hidden input.
    assert solution["r1"].tainted is None and compare["r1"].tainted is None
    assert solution["r3"].tainted == {"beside": {"task": "tests/3.in"}}
    assert compare["r3"].tainted == {"reads": "solution"}
    assert plan.steps["compile"].tainted is None
    assert plan.notes == ()
    assert [(run.id, run.early) for run in plan.runs] == [
        ("r1", True),
        ("r2", True),
        ("r3", False),
        ("r4", False),
    ]
    assert plan.workflow.commit == COMMIT
    assert set(plan.workflow.files) == {"diff/diff.py", "diff/program.yaml"}
    assert set(plan.task_files) == ISLANDS_PATHS - {"task.yaml", "statement.md"}
    assert plan.entrant["code"].max_size == 1048576
    assert plan.reports["score"].step == "compare"
    assert plan.steps["solution"].boxes == 1 and plan.steps["compile"].boxes is None
    assert violation(exact_json.loads(plan.to_bytes()), "plan") is None
    assert RunPlan.from_bytes(plan.to_bytes()) == plan


@pytest.mark.parametrize("name", ["exec", "interact"])
def test_the_room_kept_for_a_boxed_primitive_is_what_it_keeps(name: str) -> None:
    assert primitive(name).STEP_MEMORY_KB == PRIMITIVE_ROOM_MB * 1024


def test_only_a_boxed_primitive_reads_the_cache(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    assert ok(islands(primitives)).steps["solution"].cache
    cached = primitives["unicon/compile@v3"].model_copy(update={"cache": True})

    found = islands({**primitives, "unicon/compile@v3": cached})

    assert refused(found) == {
        "workflow": [
            f"The step {step} uses unicon/compile@v3, which reads the task's files through "
            "the machine's cache, but only primitives that run programs in boxes may, such as "
            "unicon/exec."
            for step in ("compile", "diff")
        ]
    }


def test_a_batch_sums_its_runs_time_and_holds_the_most_of_the_rest(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    plan = ok(islands(primitives))
    solution = held(plan, "solution", "early")

    # Each run: 2 s gives 2 * 2000 + 3000 ms; 256 MB gives exec's 512 MB,
    # the box's 256 MB and exec's room, with the entrant's program, which
    # compile may write up to 64 MB of, and the run's stdin, under a page
    # and so a megabyte rounded up, on top.
    assert (solution.limits.time_ms, solution.limits.cpu_ms) == (14000, 14000)
    assert solution.limits.memory_mb == 256 + 256 + 64
    assert plan.items_of(solution) == [
        ("r1", (plan.steps["solution"].items or {})["r1"].inputs),
        ("r2", (plan.steps["solution"].items or {})["r2"].inputs),
    ]
    assert held(plan, "compile", "early").limits.memory_mb == 1024


def test_each_container_holds_the_files_it_copies(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    plan = ok(islands(primitives, sizes={"tests/3.ans": 300 * 1024 * 1024}))

    # The box's 256 MB and exec's room of 256 MB, with the diff script and
    # the solution's output, each up to 64 MB, and the 300 MB answer on
    # top; the samples' runs copy the small answers.
    assert held(plan, "compare").limits.memory_mb == 256 + 256 + 64 + 64 + 300
    assert held(plan, "compare", "early").limits.memory_mb == 256 + 256 + 64 + 64 + 1


def test_an_entrant_file_is_copied_at_the_most_its_input_may_hold(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    plan = ok(
        planned(primitives, CARTPOLE_TASK, CARTPOLE, CARTPOLE_FILES, frozenset({"task.yaml"}))
    )
    task = CARTPOLE_TASK.replace(
        "  weights: entrant\n", "  weights: {entrant: {max_size: 500MB}}\n"
    )
    larger = ok(planned(primitives, task, CARTPOLE, CARTPOLE_FILES, frozenset({"task.yaml"})))

    # The box's 1024 MB and exec's room, with the runner's program, up to
    # 64 MB, and the weights, up to 10 MB, or 500 MB, on top.
    assert plan.entrant["weights"].max_size == DEFAULT_MAX_SIZE
    assert held(plan, "episode", "early").limits.memory_mb == 1024 + 256 + 64 + 10
    assert held(larger, "episode", "early").limits.memory_mb == 1024 + 256 + 64 + 500


def test_interact_needs_a_box_per_program(primitives: dict[str, PrimitiveDeclaration]) -> None:
    task = JUDGED_TASK.replace("show: {now: [moves]}", "show: {after_close: [moves]}")
    plan = ok(
        planned(
            primitives,
            task,
            JUDGED,
            {"workflow.yaml": JUDGED.encode()},
            JUDGED_PATHS,
            manifests={"judge/program.yaml": JUDGE_MANIFEST},
        )
    )

    assert plan.steps["game"].boxes == 2
    # Both programs' 256 MB and interact's room of 512 MB, with both
    # programs, each up to 64 MB, copied in on top.
    assert held(plan, "game").limits.memory_mb == 256 + 256 + 512 + 64 + 64
    assert plan.steps["game"].inputs == {
        "programs": {
            "programs": {
                "entrant": {
                    "program": {"step": "compile", "output": "program"},
                    "time_limit": {"value": 1},
                    "memory_limit": {"value": 256},
                },
                "judge": {
                    "program": {"step": "judge", "output": "program"},
                    "manifest": {"task": "judge/program.yaml"},
                    "time_limit": {"value": 3},
                    "memory_limit": {"value": 256},
                },
            }
        },
        "links": {"links": ["entrant -> judge", "judge -> entrant"]},
    }


def test_what_a_step_reads_is_listed_for_its_pipeline(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    plan = ok(islands(primitives))

    read = plan.files_read(held(plan, "compare"))

    assert read.task == {"tests/3.ans", "tests/4.ans"}
    assert read.workflow == {"diff/program.yaml"}
    assert read.outputs == {("diff", "program"), ("solution", "output")}
    assert plan.files_read(held(plan, "diff", "save")).workflow == {
        "diff/diff.py",
        "diff/program.yaml",
    }
    assert plan.files_read(held(plan, "compile", "early")).entrant == {
        ("code", None),
        ("language", None),
    }


# The three tasks


def test_cartpole_is_one_early_pipeline(primitives: dict[str, PrimitiveDeclaration]) -> None:
    plan = ok(
        planned(primitives, CARTPOLE_TASK, CARTPOLE, CARTPOLE_FILES, frozenset({"task.yaml"}))
    )

    assert laid_out(plan) == {
        "save": [("runner", None, ())],
        "early": [("episode", ("r1", "r2", "r3", "r4", "r5", "r6"), ())],
    }
    episode = plan.steps["episode"]
    assert episode.self_reported is True
    assert (episode.items or {})["r4"].inputs["self_reported"] == {"value": True}
    assert (episode.items or {})["r4"].inputs["args"] == {
        "value": "--env cartpole-wind --seed 1 --episodes 100"
    }
    assert [(each.column, each.inputs) for each in plan.confirmations] == [
        ("passed", ("weights",)),
        ("reward", ("weights",)),
        ("verdict", ("weights",)),
    ]


def test_tour_gives_each_run_its_own_part(primitives: dict[str, PrimitiveDeclaration]) -> None:
    plan = ok(planned(primitives, TOUR_TASK, TOUR, TOUR_FILES, TOUR_PATHS))

    items = plan.steps["check"].items or {}
    assert items["r1"].inputs["files"] == {
        "files": {"input": {"task": "parts/1.in"}, "route": {"entrant": "route", "part": 1}}
    }
    assert items["r2"].inputs["files"] == {
        "files": {"input": {"task": "parts/2.in"}, "route": {"entrant": "route", "part": 2}}
    }
    assert items["r1"].inputs["args"] == {"value": "1200"}
    assert plan.entrant["route"].parts == 2
    assert [run.key for run in plan.runs] == [
        {"input": "parts/1.in", "route": {"part": 1}},
        {"input": "parts/2.in", "route": {"part": 2}},
    ]
    assert set(plan.pipelines) == {"save", "early"}
    assert plan.files_read(held(plan, "check", "early")).entrant == {("route", 1), ("route", 2)}


def test_a_step_given_the_tasks_manifest_writes_every_report_of_its_files(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    workflow = TOUR.replace("  best: number\n", "  best: number\n  manifest: file\n").replace(
        "${{ workflow.checker/program.yaml }}", "${{ inputs.manifest }}"
    )
    task = (
        TOUR_TASK.replace("inputs: [input, route]", "inputs: [input, route, manifest]")
        .replace("entrant]", "entrant, check/one.yaml]", 1)
        .replace("[parts/2.in, entrant]", "[parts/2.in, entrant, check/two.yaml]")
    )
    reports = b"reports:\n  length: {at_least: 0}\n  score: {at_least: 0, at_most: 1}\n"
    plan = ok(
        planned(
            primitives,
            task,
            workflow,
            {**TOUR_FILES, "workflow.yaml": workflow.encode()},
            TOUR_PATHS | {"check/one.yaml", "check/two.yaml"},
            manifests={
                "check/one.yaml": reports + b"  moves: {type: text, optional: true}\n",
                "check/two.yaml": reports + b"  turns: {at_least: 0}\n",
            },
        )
    )

    # The checker writes what its file declares, so each report is an output
    # of the step, one some file leaves out optional.
    outputs = plan.steps["check"].outputs
    assert (outputs["length"], outputs["score"]) == ("number", "number")
    assert (outputs["moves?"], outputs["turns?"]) == ("text", "number")
    assert "turns" not in outputs


# Step classes


GENERATED = """\
inputs:
  code: folder
  seed: number
steps:
  - id: generator
    use: unicon/compile@v3
    with: {source: "${{ workflow.gen/ }}", language: python}
  - id: generate
    use: unicon/exec@v1
    with:
      program: ${{ steps.generator.program }}
      args: "--seed ${{ inputs.seed }}"
      time_limit: 1
      memory_limit: 64
  - id: compile
    use: unicon/compile@v3
    with: {source: "${{ inputs.code }}", language: python}
  - id: solution
    use: unicon/exec@v1
    with:
      program: ${{ steps.compile.program }}
      stdin: ${{ steps.generate.output }}
      time_limit: 1
      memory_limit: 64
reports:
  time_ms: ${{ steps.solution.time_ms }}
"""
GENERATED_FILES = {"workflow.yaml": GENERATED.encode(), "gen/gen.py": b"print(1)\n"}
GENERATED_TASK = """\
name: Generated
workflow: acme/generated@v1
inputs:
  code: entrant
repeat:
  - inputs: [seed]
    tuples: [1, 2]
scoring:
  values:
    total: {block: sum_runs, over: {runs: all}, of: {run: [all, time_ms]}}
  show:
    after_close: [total]
"""


def test_a_generator_reading_a_repeated_input_runs_at_the_save_once_per_run(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    plan = ok(
        planned(primitives, GENERATED_TASK, GENERATED, GENERATED_FILES, frozenset({"task.yaml"}))
    )

    assert plan.steps["generate"].class_ == "save_per_run"
    assert laid_out(plan) == {
        "save": [("generator", None, ()), ("generate", ("r1", "r2"), ("generator",))],
        "late": [("compile", None, ()), ("solution", ("r1", "r2"), ("compile",))],
    }
    assert (plan.steps["generate"].items or {})["r2"].inputs["args"] == {"value": "--seed 2"}


def test_a_step_reading_one_done_more_often_is_refused_naming_both() -> None:
    problems = order_problems(
        {"compile": "grading_once", "solution": "grading_per_run", "diff": "save_once"},
        {"compile": ["solution"], "solution": ["diff"], "diff": []},
    )

    assert problems == [
        {
            "path": "workflow",
            "message": "The step compile runs once per grading, and reads solution, which runs "
            "once per run of each grading, so compile would run before what it reads exists.",
        }
    ]


SELF_TESTED = """\
inputs:
  code: folder
  input: file
steps:
  - id: compile
    use: unicon/compile@v3
    with: {source: "${{ inputs.code }}", language: python}
  - id: selftest
    use: unicon/exec@v1
    with: {program: "${{ steps.compile.program }}", time_limit: 1, memory_limit: 64}
  - id: solution
    use: unicon/exec@v1
    with:
      program: ${{ steps.selftest.output }}
      stdin: ${{ inputs.input }}
      time_limit: 1
      memory_limit: 64
reports:
  time_ms: ${{ steps.solution.time_ms }}
"""
SELF_TESTED_TASK = """\
name: Self tested
workflow: acme/selftested@v1
inputs:
  code: entrant
repeat:
  - inputs: [input]
    tuples: [samples/1.in, tests/2.in]
scoring:
  groups:
    shown: {input: [samples/1.in]}
  show:
    now: [shown, samples/]
    after_close: [all]
"""


def test_a_once_step_read_only_by_a_once_step_an_early_run_reads_is_early(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    plan = ok(
        planned(
            primitives,
            SELF_TESTED_TASK,
            SELF_TESTED,
            {"workflow.yaml": SELF_TESTED.encode()},
            frozenset({"task.yaml", "samples/1.in", "tests/2.in"}),
        )
    )

    assert laid_out(plan) == {
        "early": [
            ("compile", None, ()),
            ("selftest", None, ("compile",)),
            ("solution", ("r1",), ("selftest",)),
        ],
        "late": [("solution", ("r2",), ())],
    }


# Pipelines


def test_a_batch_never_mixes_early_and_late_runs_nor_reads_a_late_runs_file(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    plan = ok(islands(primitives))
    early = {run.id for run in plan.runs if run.early}
    late_only = {"tests/3.in", "tests/3.ans", "tests/4.in", "tests/4.ans"}

    for step in plan.pipeline("early"):
        assert set(step.runs or ()) <= early
        assert not plan.files_read(step).task & late_only
    for step in plan.pipeline("late"):
        assert not set(step.runs or ()) & early


def test_a_value_shown_now_reading_only_compiled_makes_an_early_pipeline_of_the_compile(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    text = ISLANDS.replace(
        "    now: [sample_score, sample_time, sample_tests, samples/]\n",
        "    now: [built]\n",
    ).replace("  values:\n", "  values:\n    built: {fact: compiled}\n")

    plan = ok(islands(primitives, text))

    assert laid_out(plan)["early"] == [("compile", None, ())]
    assert [step.name for step in plan.pipeline("late")] == ["solution", "compare"]
    assert held(plan, "solution").runs == ("r1", "r2", "r3", "r4")


def test_nothing_shown_during_the_contest_is_one_late_pipeline(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    text = ISLANDS.replace(
        "    now: [sample_score, sample_time, sample_tests, samples/]\n"
        "    after_close: [st1, st2, total, all]\n",
        "    after_close: [sample_score, sample_time, sample_tests, samples/,\n"
        "      st1, st2, total, all]\n",
    )

    plan = ok(islands(primitives, text))

    assert set(plan.pipelines) == {"save", "late"}
    assert [step.name for step in plan.pipeline("late")] == ["compile", "solution", "compare"]


# Taint


LINKED = """\
inputs:
  code: folder
  grader: folder
  input: file
steps:
  - id: compile
    use: acme/link@v1
    with: {source: "${{ inputs.code }}", grader: "${{ inputs.grader }}"}
  - id: run
    use: unicon/exec@v1
    with:
      program: ${{ steps.compile.program }}
      stdin: ${{ inputs.input }}
      time_limit: 1
      memory_limit: 64
reports:
  time_ms: ${{ steps.run.time_ms }}
"""
LINKED_TASK = """\
name: Linked
workflow: acme/linked@v1
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
    fast: {block: max_runs, over: {runs: shown}, of: {run: [shown, time_ms]}}
  show:
    now: [shown, samples/]
    after_close: [fast, all]
"""
LINKED_PATHS = frozenset({"task.yaml", "grader/grader.h", "samples/1.in", "tests/2.in"})
WORDS = "every run reads `compile`, which runs the entrant's code beside `grader/`"


def linked(primitives: Mapping[str, PrimitiveDeclaration], task: str = LINKED_TASK) -> RunPlanCheck:
    return planned(primitives, task, LINKED, {"workflow.yaml": LINKED.encode()}, LINKED_PATHS)


def test_a_hidden_grader_with_a_group_shown_now_is_refused_with_its_words(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    problems = refused(linked(primitives))

    assert list(problems) == ["scoring.show.now[0]"]
    assert WORDS in problems["scoring.show.now[0]"][0]


def test_a_hidden_grader_with_nothing_shown_now_saves_as_one_late_pipeline(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = LINKED_TASK.replace(
        "    now: [shown, samples/]\n    after_close: [fast, all]\n",
        "    after_close: [shown, samples/, fast, all]\n",
    )

    plan = ok(linked(primitives, task))

    assert laid_out(plan) == {"late": [("compile", None, ()), ("run", ("r1", "r2"), ("compile",))]}
    assert plan.steps["compile"].tainted == {"beside": {"task": "grader/"}}
    # The program compile linked holds the grader, so each run runs the
    # entrant's code beside it too.
    assert {run: item.tainted for run, item in (plan.steps["run"].items or {}).items()} == {
        "r1": {"beside": {"task": "grader/"}},
        "r2": {"beside": {"task": "grader/"}},
    }


def test_on_release_makes_no_run_early_but_is_refused_as_if_shown(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = LINKED_TASK.replace(
        "    now: [shown, samples/]\n",
        "    now: [samples/]\n    on_release: [shown]\n",
    )

    problems = refused(linked(primitives, task))

    assert list(problems) == ["scoring.show.on_release[0]"]
    assert WORDS in problems["scoring.show.on_release[0]"][0]


def test_on_release_is_noted_over_runs_with_their_own_hidden_input(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    text = ISLANDS.replace(
        "    after_close: [st1, st2, total, all]\n",
        "    after_close: [st2, total, all]\n    on_release: [st1]\n",
    )

    plan = ok(islands(primitives, text))

    assert [run.id for run in plan.runs if run.early] == ["r1", "r2"]
    assert held(plan, "solution").runs == ("r3", "r4")
    assert plan.notes == (
        "st1 reads runs whose own steps run the entrant's code beside what entrants are not "
        "shown during the contest, such as `solution` beside `tests/3.in` in the run r3, so "
        "the entrant's code can signal a little about it through what st1 shows.",
    )


SECRET = """\
inputs:
  code: folder
  key: file
  input: file
steps:
  - id: compile
    use: unicon/compile@v3
    with: {source: "${{ inputs.code }}", language: python}
  - id: judge
    use: acme/judge@v1
    with: {program: "${{ steps.compile.program }}", key: "${{ inputs.key }}"}
  - id: run
    use: unicon/exec@v1
    with:
      program: ${{ steps.compile.program }}
      stdin: ${{ inputs.input }}
      time_limit: 1
      memory_limit: 64
reports:
  score: ${{ steps.judge.score }}
"""
SECRET_TASK = """\
name: Keyed
workflow: acme/keyed@v1
inputs:
  code: entrant
  key: hidden/key.txt
repeat:
  - inputs: [input]
    tuples: [samples/1.in]
scoring:
  values:
    total: {block: sum_runs, over: {runs: all}, of: {run: [all, score]}}
  show:
    now: [total, samples/]
"""
SECRET_PATHS = frozenset({"task.yaml", "hidden/key.txt", "samples/1.in"})


def test_a_hidden_file_on_a_secret_port_does_not_taint(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    plan = ok(
        planned(primitives, SECRET_TASK, SECRET, {"workflow.yaml": SECRET.encode()}, SECRET_PATHS)
    )

    assert plan.steps["judge"].tainted is None
    assert plan.steps["judge"].inputs == {
        "program": {"step": "compile", "output": "program"},
        "key": {"task": "hidden/key.txt"},
    }


def test_the_same_file_on_a_port_not_marked_secret_taints(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    workflow = SECRET.replace('key: "${{ inputs.key }}"', 'data: "${{ inputs.key }}"')

    problems = refused(
        planned(
            primitives, SECRET_TASK, workflow, {"workflow.yaml": workflow.encode()}, SECRET_PATHS
        )
    )

    assert (
        "every run reads `judge`, which runs the entrant's code beside `hidden/key.txt`"
        in (problems["scoring.show.now[0]"][0])
    )


def test_the_entrants_code_beside_a_workflow_file_is_tainted(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    plan = ok(
        planned(primitives, CARTPOLE_TASK, CARTPOLE, CARTPOLE_FILES, frozenset({"task.yaml"}))
    )

    assert (plan.steps["episode"].items or {})["r1"].tainted == {"beside": {"workflow": "runner/"}}
    assert plan.notes == (
        "reward reads runs whose own steps run the entrant's code beside what entrants are not "
        "shown during the contest, such as `episode` beside the workflow's `runner/` in the run "
        "r1, so the entrant's code can signal a little about it through what reward shows.",
    )
    # A step running only the workflow's own program is not.
    tour = ok(planned(primitives, TOUR_TASK, TOUR, TOUR_FILES, TOUR_PATHS))
    assert all(item.tainted is None for item in (tour.steps["check"].items or {}).values())


# What `compiled` shown during the contest needs


BUILT_TASK = LINKED_TASK.replace(
    "    fast: {block: max_runs",
    "    built: {fact: compiled}\n    fast: {block: max_runs",
).replace(
    "    now: [shown, samples/]\n    after_close: [fast, all]\n",
    "    now: [built, samples/]\n    after_close: [shown, fast, all]\n",
)
LINKC_FLOW = LINKED.replace("acme/link@v1", "acme/linkc@v1")


def built(
    primitives: Mapping[str, PrimitiveDeclaration],
    task: str = BUILT_TASK,
    workflow: str = LINKC_FLOW,
) -> RunPlanCheck:
    return planned(primitives, task, workflow, {"workflow.yaml": workflow.encode()}, LINKED_PATHS)


def test_compiled_from_a_compile_beside_a_hidden_grader_is_refused(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    problems = refused(built(primitives))

    assert problems == {
        "scoring.show.now[0]": [
            "built cannot be shown as soon as a grading ends: it reads `compiled`, from "
            "`compile`, which runs the entrant's code beside `grader/`, which entrants are not "
            "shown during the contest, so built could tell them about it. Show built after_close "
            "instead."
        ]
    }


def test_compiled_under_on_release_is_refused_as_if_shown(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = BUILT_TASK.replace("now: [built, samples/]", "now: [samples/]\n    on_release: [built]")

    problems = refused(built(primitives, task))

    assert list(problems) == ["scoring.show.on_release[0]"]
    assert (
        "from `compile`, which runs the entrant's code beside `grader/`"
        in (problems["scoring.show.on_release[0]"][0])
    )


def test_compiled_from_a_compile_beside_a_shown_grader_is_early(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = BUILT_TASK.replace("now: [built, samples/]", "now: [built, samples/, grader/]")

    plan = ok(built(primitives, task))

    assert laid_out(plan) == {
        "early": [("compile", None, ())],
        "late": [("run", ("r1", "r2"), ())],
    }


def test_compiled_with_no_step_done_once_that_can_fail_to_compile_is_refused(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    task = BUILT_TASK.replace("now: [built, samples/]", "now: [built, samples/, grader/]")

    problems = refused(built(primitives, task, LINKED))

    assert problems == {
        "scoring.show.now[0]": [
            "built cannot be shown as soon as a grading ends: it reads `compiled`, but no step "
            "done once per grading can give a compile error, so `compiled` is known only once "
            "the whole grading ends, its hidden runs too. Show built after_close instead."
        ]
    }


PER_LANGUAGE = """\
inputs:
  code: folder
  language: {type: enum, options: [c, python]}
  input: file
steps:
  - id: compile
    use: unicon/compile@v3
    with: {source: "${{ inputs.code }}", language: "${{ inputs.language }}"}
  - id: run
    use: unicon/exec@v1
    with:
      program: ${{ steps.compile.program }}
      stdin: ${{ inputs.input }}
      time_limit: 1
      memory_limit: 64
reports:
  time_ms: ${{ steps.run.time_ms }}
"""
PER_LANGUAGE_TASK = """\
name: Per language
workflow: acme/per-language@v1
inputs:
  code: entrant
repeat:
  - inputs: [language]
    tuples: [c, python]
  - inputs: [input]
    tuples: [samples/1.in, tests/2.in]
scoring:
  values:
    built: {fact: compiled}
  show:
    now: [built]
    after_close: [all]
"""


def test_compiled_from_a_compile_done_per_run_is_refused(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    found = planned(
        primitives,
        PER_LANGUAGE_TASK,
        PER_LANGUAGE,
        {"workflow.yaml": PER_LANGUAGE.encode()},
        frozenset({"task.yaml", "samples/1.in", "tests/2.in"}),
    )

    assert refused(found) == {
        "scoring.show.now[0]": [
            "built cannot be shown as soon as a grading ends: it reads `compiled`, but "
            "`compile`, which can give a compile error, runs once per run, so `compiled` is "
            "known only once every run has run, the hidden ones too. Show built after_close "
            "instead."
        ]
    }


# What a pipeline may take


WARMED = """\
inputs:
  code: folder
  warm_limit: number
  time_limit: number
  input: file
steps:
  - id: compile
    use: unicon/compile@v3
    with: {source: "${{ inputs.code }}", language: python}
  - id: warm
    use: unicon/exec@v1
    with:
      program: ${{ steps.compile.program }}
      time_limit: ${{ inputs.warm_limit }}
      memory_limit: 64
  - id: solution
    use: unicon/exec@v1
    with:
      program: ${{ steps.compile.program }}
      stdin: ${{ inputs.input }}
      time_limit: ${{ inputs.time_limit }}
      memory_limit: 64
reports:
  time_ms: ${{ steps.solution.time_ms }}
"""
WARMED_TASK = """\
name: Warmed
workflow: acme/warmed@v1
inputs:
  code: entrant
  warm_limit: 1
  time_limit: 400
repeat:
  - inputs: [input]
    tuples: [tests/1.in, tests/2.in]
scoring:
  show:
    after_close: [all]
"""


def test_a_pipeline_over_the_wall_is_refused_at_what_raises_its_longest_step(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    found = planned(
        primitives,
        WARMED_TASK,
        WARMED,
        {"workflow.yaml": WARMED.encode()},
        frozenset({"task.yaml", "tests/1.in", "tests/2.in"}),
    )

    # Two runs of 400 s give 2 * (400 * 2000 + 3000) ms, 27 minutes, and
    # the compile, the warm-up and each step's start make 30; `warm_limit`,
    # which raises a shorter step, is not where to look.
    assert refused(found) == {
        "inputs.time_limit": [
            "The steps of a grading's late pipeline take 30 minutes, but a pipeline may take at "
            "most 25. The longest, solution, takes 27."
        ]
    }


def test_the_wall_is_checked_per_pipeline(primitives: dict[str, PrimitiveDeclaration]) -> None:
    within = ok(islands(primitives, ISLANDS.replace("time_limit: 2", "time_limit: 300")))
    over = refused(islands(primitives, ISLANDS.replace("time_limit: 2", "time_limit: 400")))

    # Each grading pipeline holds two runs of 300 s, within the wall, though
    # all four together would not be.
    assert held(within, "solution").limits.time_ms == 2 * (300 * 2000 + 3000)
    assert [message.split(".")[0] for message in over["inputs.time_limit"]] == [
        "The steps of a grading's early pipeline take 30 minutes, but a pipeline may take at "
        "most 25",
        "The steps of a grading's late pipeline take 29 minutes, but a pipeline may take at "
        "most 25",
    ]


def test_a_step_over_the_machine_is_refused_at_what_gives_its_largest_file(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    problems = refused(islands(primitives, sizes={"tests/3.ans": 17 * 1024 * 1024 * 1024}))

    assert problems == {
        "repeat[0].inputs[1]": [
            "The step compare needs 18048 MB of memory, with the files it copies in beside its "
            "programs, the largest `tests/3.ans` at 17408 MB, but no machine this task may run "
            "on has more than 16384."
        ]
    }


# Slots


SLOTS = run_machine([Slot("big", 28672), Slot("small", 8192)])
SIDE_BY_SIDE = """\
inputs:
  code: folder
  input: file
  memory: number
steps:
  - id: compile
    use: unicon/compile@v3
    with: {source: "${{ inputs.code }}", language: python}
  - id: first
    use: unicon/exec@v1
    with:
      program: ${{ steps.compile.program }}
      stdin: ${{ inputs.input }}
      time_limit: 1
      memory_limit: ${{ inputs.memory }}
  - id: second
    use: unicon/exec@v1
    with:
      program: ${{ steps.compile.program }}
      stdin: ${{ inputs.input }}
      time_limit: 1
      memory_limit: 3000
  - id: third
    use: unicon/exec@v1
    with:
      program: ${{ steps.compile.program }}
      stdin: ${{ inputs.input }}
      time_limit: 1
      memory_limit: 3000
  - id: judge
    use: acme/judge@v1
    with: {program: "${{ steps.compile.program }}", data: "${{ inputs.input }}"}
reports:
  time_ms: ${{ steps.third.time_ms }}
"""
SIDE_BY_SIDE_TASK = """\
name: Side by side
workflow: acme/sidebyside@v1
inputs:
  code: entrant
  memory: 3000
repeat:
  - inputs: [input]
    tuples: [tests/1.in, tests/2.in]
scoring:
  show:
    after_close: [all]
"""


def side_by_side_plan(
    primitives: Mapping[str, PrimitiveDeclaration], memory: int = 3000
) -> RunPlanCheck:
    return planned(
        primitives,
        SIDE_BY_SIDE_TASK.replace("memory: 3000", f"memory: {memory}"),
        SIDE_BY_SIDE,
        {"workflow.yaml": SIDE_BY_SIDE.encode()},
        frozenset({"task.yaml", "tests/1.in", "tests/2.in"}),
        machine=SLOTS,
    )


def test_steps_side_by_side_are_chained_to_fit_the_smallest_slot(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    """Three boxed steps of 3320 MB each and a slot of 8192: the first two
    run side by side and the third waits for both; the compile and each
    run of the judge, which run no boxes, run beside nothing."""
    plan = ok(side_by_side_plan(primitives))
    late = plan.pipeline("late")

    assert [step.limits.memory_mb for step in late[1:4]] == [3320, 3320, 3320]
    assert laid_out(plan) == {
        "late": [
            ("compile", None, ()),
            ("first", ("r1", "r2"), ("compile",)),
            ("second", ("r1", "r2"), ("compile",)),
            ("third", ("r1", "r2"), ("compile", "first", "second")),
            ("judge.r1", ("r1",), ("compile", "third")),
            ("judge.r2", ("r2",), ("compile", "judge.r1")),
        ]
    }
    assert side_by_side(late) == 6640
    assert slot_of(late, SLOTS.slots) == Slot("small", 8192)


def test_a_step_too_big_for_the_small_slot_runs_its_pipeline_in_the_big_one(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    """The big slot holds all three boxed steps at once, so they wait for
    nothing but the compile, and the judge's first run waits for all
    three."""
    plan = ok(side_by_side_plan(primitives, memory=20000))
    late = plan.pipeline("late")

    assert [(step.name, step.depends_on) for step in late[1:5]] == [
        ("first", ("compile",)),
        ("second", ("compile",)),
        ("third", ("compile",)),
        ("judge.r1", ("compile", "first", "second", "third")),
    ]
    assert side_by_side(late) == 20320 + 3320 + 3320
    assert slot_of(late, SLOTS.slots) == Slot("big", 28672)


def test_without_slots_a_pipeline_is_laid_out_as_wired(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    plan = ok(
        planned(
            primitives,
            SIDE_BY_SIDE_TASK,
            SIDE_BY_SIDE,
            {"workflow.yaml": SIDE_BY_SIDE.encode()},
            frozenset({"task.yaml", "tests/1.in", "tests/2.in"}),
        )
    )

    assert all(step.depends_on in ((), ("compile",)) for step in plan.pipeline("late"))


def test_a_step_bigger_than_the_biggest_slot_is_refused(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    workflow = parse_run_workflow(SIDE_BY_SIDE)
    files = {"workflow.yaml": SIDE_BY_SIDE.encode()}
    checked = check_run_workflow(workflow, primitives, WorkflowFiles(frozenset(files), files))
    found = check_run_task(
        parse_task(SIDE_BY_SIDE_TASK.replace("memory: 3000", "memory: 30000"), runs=True),
        workflow,
        checked,
        frozenset({"task.yaml", "tests/1.in", "tests/2.in"}),
        {},
        previous=None,
        submitted=False,
        secrets=frozenset(),
        machine=SLOTS,
    )

    assert [problem["message"] for problem in found.problems] == [
        "With memory at 30000, the step first needs 30256 MB of memory, but no machine "
        "this task may run on has more than 28672."
    ]


def _step(name: str, memory: int, *waits: str) -> PipelineStep:
    limits = StepLimits(time_ms=1000, cpu_ms=1000, memory_mb=memory, pids=64, output_mb=1, gpus=0)
    return PipelineStep(name=name, step=name, depends_on=waits, limits=limits)


def test_side_by_side_counts_each_step_with_every_one_it_is_not_ordered_with() -> None:
    """a and b run together and c after both: at most c alone. d, ordered
    with none, is counted beside all three, more than can run at once,
    which the stages the planner makes never leave."""
    steps = [_step("a", 100), _step("b", 200), _step("c", 400, "a", "b"), _step("d", 50)]

    assert side_by_side(steps[:3]) == 400
    assert side_by_side(steps) == 750
    assert side_by_side([]) == 0
    assert slot_of(steps[:3], [Slot("tiny", 399), Slot("small", 400)]) == Slot("small", 400)
    assert slot_of(steps[:3], [Slot("tiny", 399)]) is None


# The schema's versions


@pytest.mark.parametrize("version", [7, "6"])
def test_a_plan_of_an_unknown_version_is_refused_at_its_version(
    primitives: dict[str, PrimitiveDeclaration], version: object
) -> None:
    document = exact_json.loads(ok(islands(primitives)).to_bytes())
    document["schema_version"] = version

    found = violation(document, "plan")

    assert found is not None and found.startswith("at schema_version: ")


# What a save changes


def _snapshot(plan: RunPlan, data: Mapping[str, str] | None = None) -> Snapshot:
    return Snapshot(plans={PLAN_PATH: plan.to_bytes()}, data=dict(data or {}))


def test_a_save_says_what_its_plan_changed(primitives: dict[str, PrimitiveDeclaration]) -> None:
    before = ok(islands(primitives))
    longer = ok(islands(primitives, ISLANDS.replace("time_limit: 2", "time_limit: 3")))
    shown = ok(
        islands(
            primitives,
            ISLANDS.replace(
                "    after_close: [st1, st2, total, all]\n", "    after_close: [st2, total, all]\n"
            ).replace("    now: [sample_score,", "    now: [st1, sample_score,"),
        )
    )

    assert run_plan_changes(None, _snapshot(before)) == ()
    assert run_plan_changes(_snapshot(before), _snapshot(before)) == ()
    assert run_plan_changes(_snapshot(before), _snapshot(longer)) == (
        "The inputs of the step solution changed.",
    )
    assert run_plan_changes(_snapshot(before), _snapshot(shown)) == (
        "What is shown during the contest moved, so other runs are graded early.",
    )
    assert run_plan_changes(
        _snapshot(before, {"tests/3.in": "a"}), _snapshot(before, {"tests/3.in": "b"})
    ) == ("The file tests/3.in changed.",)
    moved = before.model_copy(
        update={"workflow": before.workflow.model_copy(update={"commit": "d" * 40})}
    )
    assert run_plan_changes(_snapshot(before), _snapshot(moved)) == (
        "The workflow unicon/classic@v3 moved to another commit.",
    )
    assert run_plan_changes(Snapshot(plans={}, data={}), _snapshot(before)) == (
        "The way the task grades changed.",
    )


def test_a_save_says_which_steps_a_submission_runs_again(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    before = ok(islands(primitives))
    longer = ok(islands(primitives, ISLANDS.replace("time_limit: 2", "time_limit: 3")))
    shown = ok(
        islands(
            primitives,
            ISLANDS.replace(
                "    after_close: [st1, st2, total, all]\n", "    after_close: [st2, total, all]\n"
            ).replace("    now: [sample_score,", "    now: [st1, sample_score,"),
        )
    )
    other = FileFact(size=1, sha256="d" * 64)
    answer = before.model_copy(update={"task_files": {**before.task_files, "tests/3.ans": other}})
    script = before.model_copy(
        update={
            "workflow": before.workflow.model_copy(
                update={"files": {**before.workflow.files, "diff/diff.py": other}}
            )
        }
    )

    assert steps_run_again(None, _snapshot(before)) == ()
    assert steps_run_again(_snapshot(before), _snapshot(before)) == ()
    # Its output read by compare, the solution runs both again.
    assert steps_run_again(_snapshot(before), _snapshot(longer)) == ("solution", "compare")
    # Runs moved between the pipelines keep every key.
    assert steps_run_again(_snapshot(before), _snapshot(shown)) == ()
    assert steps_run_again(_snapshot(before), _snapshot(answer)) == ("compare",)
    # The checker is compiled at the save; each submission runs only compare.
    assert steps_run_again(_snapshot(before), _snapshot(script)) == ("compare",)
    assert steps_run_again(Snapshot(plans={}, data={}), _snapshot(before)) == (
        "compile",
        "solution",
        "compare",
    )
