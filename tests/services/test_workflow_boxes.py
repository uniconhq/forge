"""A run workflow using another as a step: the version expands every box
into primitive steps, refuses a workflow that would hold itself, and counts
the cap across the whole expansion.
"""

from pathlib import Path

import pytest

from forge.domain.errors import NotFound
from forge.domain.identity import AsUser
from forge.domain.run_workflow import WorkflowFiles, check_run_workflow
from forge.domain.sessions import Session
from forge.domain.workflow_boxes import flatten, placed
from forge.domain.workflow_definition import RunWorkflow, read_workflow
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import sessions, workflows
from forge.services.workflow_boxes import Expanded, expand
from forge.services.workflows import Box, BoxInput, BoxOutput
from forge.testing import PLACEHOLDER_DIGEST
from tests.conftest import sibling
from tests.services.conftest import Acme, signed_in

CLASSIC = sibling("deploy", "workflows/classic/v3")

BUILD = """\
inputs:
  code: {type: folder}
  language: {type: enum, options: [c, cpp, java, python]}
steps:
  - id: compile
    use: unicon/compile@v3
    with:
      source: ${{ inputs.code }}
      language: ${{ inputs.language }}
outputs:
  program: ${{ steps.compile.program }}
  log: ${{ steps.compile.log }}
"""

JUDGE = """\
inputs:
  output: {type: file}
  answer: {type: file}
steps:
  - id: diff
    use: unicon/compile@v3
    with:
      source: ${{ workflow.diff/ }}
      language: python
  - id: compare
    use: unicon/exec@v1
    with:
      program: ${{ steps.diff.program }}
      manifest: ${{ workflow.diff/program.yaml }}
      time_limit: 3
      memory_limit: 256
      files:
        output: ${{ inputs.output }}
        answer: ${{ inputs.answer }}
outputs:
  score: ${{ steps.compare.score }}
"""

OUTER = """\
inputs:
  code: {type: folder, title: Solution}
  language: {type: enum, options: [c, cpp, java, python], title: Language}
  input: {type: file, title: Input}
  answer: {type: file, title: Answer}
  time_limit: {type: number, title: Time limit, unit: seconds}
  memory_limit: {type: number, title: Memory limit, unit: MB}
steps:
  - id: build
    use: bob/build@v1
    with:
      code: ${{ inputs.code }}
      language: ${{ inputs.language }}
  - id: solution
    use: unicon/exec@v1
    with:
      program: ${{ steps.build.program }}
      stdin: ${{ inputs.input }}
      time_limit: ${{ inputs.time_limit }}
      memory_limit: ${{ inputs.memory_limit }}
  - id: judge
    use: bob/judge@v1
    with:
      output: ${{ steps.solution.output }}
      answer: ${{ inputs.answer }}
outputs:
  score: ${{ steps.judge.score }}
  time_ms: {from: "${{ steps.solution.time_ms }}", title: Run time, unit: ms}
  memory_kb: {from: "${{ steps.solution.memory_kb }}", title: Memory used, unit: KB}
  log: {from: "${{ steps.build.log }}", title: Compile log}
"""

WRAP = """\
inputs:
  output: {type: file}
  answer: {type: file}
steps:
  - id: wrap
    use: bob/judge@v1
    with:
      output: ${{ inputs.output }}
      answer: ${{ inputs.answer }}
outputs:
  score: ${{ steps.wrap.score }}
"""

SCORED = """\
inputs:
  output: {type: file}
  answer: {type: file}
steps:
  - id: box
    use: bob/wrap@v1
    with:
      output: ${{ inputs.output }}
      answer: ${{ inputs.answer }}
outputs:
  score: ${{ steps.box.score }}
"""

OPTIONAL = """\
inputs:
  code: {type: folder}
  language: {type: enum, options: [python]}
  note: {type: text, optional: true}
steps:
  - id: whole
    use: unicon/compile@v3
    with:
      source: ${{ inputs.code }}
      language: ${{ inputs.language }}
      entry: ${{ inputs.note }}
"""

TOLD = """\
inputs:
  code: {type: folder}
  language: {type: enum, options: [python]}
  note: {type: text}
steps:
  - id: told
    use: unicon/compile@v3
    with:
      source: ${{ inputs.code }}
      language: ${{ inputs.language }}
      entry: "f${{ inputs.note }}"
"""

COMP = """\
inputs:
  code: {type: folder}
  language: {type: enum, options: [python]}
  note: {type: text}
steps:
  - id: compile
    use: unicon/compile@v3
    with:
      source: ${{ inputs.code }}
      language: ${{ inputs.language }}
      entry: "src/${{ inputs.note }}"
"""

PLAY = """\
inputs:
  program: {type: file}
  judge: {type: file}
  manifest: {type: file}
  time_limit: {type: number}
  judge_time_limit: {type: number}
steps:
  - id: game
    use: unicon/exec@v1
    with:
      programs:
        entrant:
          program: ${{ inputs.program }}
          time_limit: ${{ inputs.time_limit }}
          memory_limit: 256
        judge:
          program: ${{ inputs.judge }}
          manifest: ${{ inputs.manifest }}
          time_limit: ${{ inputs.judge_time_limit }}
          memory_limit: 256
      links: [entrant -> judge, judge -> entrant]
"""

NOOP = (
    f"image: ghcr.io/uniconhq/primitive-noop@{PLACEHOLDER_DIGEST}\n"
    "schema_version: 6\n"
    "limits: {time_ms: 1, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1, gpus: 0}\n"
    "outputs:\n"
    "  outcome: outcome\n"
).encode()


def _held(folder: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def _run_primitives(acme: Acme) -> None:
    for name in ("exec", "compile"):
        text = sibling(f"primitive-{name}", "primitive.yaml").read_bytes()
        image = f"image: ghcr.io/uniconhq/primitive-{name}@{PLACEHOLDER_DIGEST}\n".encode()
        acme.fake.primitives.add(name, {"v1" if name == "exec" else "v3": image + text})


def _noop(acme: Acme, versions: int) -> None:
    acme.fake.primitives.add("noop", {f"v{number}": NOOP for number in range(1, versions + 1)})


async def _own(setup: Setup, acme: Acme) -> Session:
    bob = await signed_in(setup, acme.fake, 8)
    return bob


async def _expanded(setup: Setup, bob: Session, text: str) -> Expanded:
    async with setup.unit_of_work() as ctx:
        reader = AsUser(bob.user_id, await sessions.credential_for(ctx, bob.id))
        return await expand(ctx, reader, _run(text), own=None)


async def _version(
    setup: Setup,
    acme: Acme,
    bob: Session,
    name: str,
    text: str,
    extra: dict[str, bytes] | None = None,
    version: str = "v1",
) -> None:
    if ("bob", f"{name}.workflow") not in acme.fake.state.repos:
        await workflows.create(setup, bob, "bob", name)
    repo = acme.fake.state.repos[("bob", f"{name}.workflow")]
    acme.fake.state.commit(repo, {"workflow.yaml": text.encode(), **(extra or {})}, "copy", 8)
    await workflows.create_version(setup, bob, "bob", name, version)


def _run(text: str) -> RunWorkflow:
    found = read_workflow(text.encode())
    assert isinstance(found, RunWorkflow)
    return found


async def test_a_boxed_classic_matches_the_flat_one_but_for_paths_and_the_pin(
    setup: Setup, acme: Acme
) -> None:
    _run_primitives(acme)
    bob = await _own(setup, acme)
    await _version(setup, acme, bob, "build", BUILD)
    judge_files = {
        path: content for path, content in _held(CLASSIC).items() if path != "workflow.yaml"
    }
    await _version(setup, acme, bob, "judge", JUDGE, judge_files)
    await _version(setup, acme, bob, "outer", OUTER)

    expanded = await _expanded(setup, bob, OUTER)
    assert expanded.problems == []
    assert expanded.flat is not None
    flat = expanded.flat
    assert [step.id for step in flat.workflow.steps] == [
        "build/compile",
        "solution",
        "judge/diff",
        "judge/compare",
    ]
    assert flat.pins == ("bob/build@v1", "bob/judge@v1")
    diff = flat.workflow.steps[2].with_
    assert diff["source"] == "${{ workflow@2.diff/ }}"
    assert flat.workflow.steps[3].with_["manifest"] == "${{ workflow@2.diff/program.yaml }}"
    assert flat.workflow.steps[1].with_["program"] == "${{ steps.build/compile.program }}"
    assert flat.workflow.outputs["score"] == "${{ steps.judge/compare.score }}"

    classic = _held(CLASSIC)
    classic_files = WorkflowFiles(
        frozenset(classic),
        {path: content for path, content in classic.items() if path.endswith("program.yaml")},
    )
    classic_check = check_run_workflow(
        _run(classic["workflow.yaml"].decode()), expanded.primitives, classic_files
    )
    boxed = placed(
        check_run_workflow(
            flat.workflow,
            expanded.primitives,
            None,
            inner_files=expanded.inner_files,
            boxes=flat.boxes,
            flat=True,
        ),
        flat,
    )
    assert classic_check.problems == []
    assert boxed.problems == []
    renamed = {"compile": "build/compile", "diff": "judge/diff", "compare": "judge/compare"}
    for name, report in classic_check.reports.items():
        got = boxed.reports[name]
        assert got.step == renamed.get(report.step, report.step)
        assert got.output == report.output
        assert got.port == report.port
        assert got.title == report.title
        assert got.unit == report.unit


async def test_an_output_read_through_two_boxes_keeps_the_ports_meaning(
    setup: Setup, acme: Acme
) -> None:
    _run_primitives(acme)
    bob = await _own(setup, acme)
    judge_files = {
        path: content for path, content in _held(CLASSIC).items() if path != "workflow.yaml"
    }
    await _version(setup, acme, bob, "judge", JUDGE, judge_files)
    await _version(setup, acme, bob, "wrap", WRAP)
    await _version(setup, acme, bob, "scored", SCORED)

    expanded = await _expanded(setup, bob, SCORED)
    assert expanded.flat is not None and expanded.problems == []
    checked = check_run_workflow(
        expanded.flat.workflow,
        expanded.primitives,
        None,
        inner_files=expanded.inner_files,
        boxes=expanded.flat.boxes,
        flat=True,
    )
    assert checked.problems == []
    classic = _held(CLASSIC)
    classic_files = WorkflowFiles(
        frozenset(classic),
        {path: content for path, content in classic.items() if path.endswith("program.yaml")},
    )
    classic_check = check_run_workflow(
        _run(classic["workflow.yaml"].decode()), expanded.primitives, classic_files
    )
    assert checked.outputs["score"].port == classic_check.reports["score"].port
    assert checked.outputs["score"].step == "box/wrap/compare"


async def test_an_optional_input_left_out_drops_the_port_and_reads_empty_in_text(
    setup: Setup, acme: Acme
) -> None:
    _run_primitives(acme)
    bob = await _own(setup, acme)
    await _version(setup, acme, bob, "optional", OPTIONAL)
    outer = """\
inputs:
  code: {type: folder}
steps:
  - id: build
    use: bob/optional@v1
    with:
      code: ${{ inputs.code }}
      language: python
"""
    await _version(setup, acme, bob, "caller", outer)
    expanded = await _expanded(setup, bob, outer)
    assert expanded.flat is not None and expanded.problems == []
    assert "entry" not in expanded.flat.workflow.steps[0].with_


def test_an_optional_input_left_out_of_text_reads_empty() -> None:
    """The check refuses an optional input written into text, so a version
    cannot hold this inner file. The expansion still reads it as empty.
    """
    inner = _run(TOLD)
    note = inner.inputs["note"].model_copy(update={"optional": True})
    inner = RunWorkflow.model_construct(
        inputs={**inner.inputs, "note": note}, steps=inner.steps, outputs={}
    )
    outer = _run(
        "inputs:\n  code: {type: folder}\nsteps:\n  - id: build\n    use: bob/told@v1\n"
        "    with:\n      code: ${{ inputs.code }}\n      language: python\n"
    )
    flat = flatten(outer, {"bob/told@v1": inner}, {"bob/told@v1": 1})
    assert not isinstance(flat, list)
    assert flat.workflow.steps[0].with_["entry"] == "f"


async def test_a_loop_of_two_is_refused_at_the_outer_box_naming_the_path(
    setup: Setup, acme: Acme
) -> None:
    _noop(acme, 1)
    bob = await _own(setup, acme)
    await _version(setup, acme, bob, "a", "steps:\n  - id: run\n    use: unicon/noop@v1\n")
    await _version(setup, acme, bob, "b", "steps:\n  - id: back\n    use: bob/a@v1\n", version="v2")
    closing = "steps:\n  - id: inner\n    use: bob/b@v2\n"
    repo = acme.fake.state.repos[("bob", "a.workflow")]
    acme.fake.state.commit(repo, {"workflow.yaml": closing.encode()}, "loop", 8)

    with pytest.raises(InvalidDefinition) as refused:
        await workflows.create_version(setup, bob, "bob", "a", "v2")

    assert refused.value.errors == [
        {
            "path": "steps[0].use",
            "message": (
                "`inner/back` uses bob/a@v1, and bob/a is already on that path, so the "
                "workflow would hold itself."
            ),
            "step": "inner/back",
        }
    ]


async def test_a_workflow_holding_itself_is_refused_when_the_check_names_it(
    setup: Setup, acme: Acme
) -> None:
    _noop(acme, 1)
    bob = await _own(setup, acme)
    made = await workflows.create(setup, bob, "bob", "held")
    await _version(setup, acme, bob, "held", "steps:\n  - id: run\n    use: unicon/noop@v1\n")
    draft = "steps:\n  - id: again\n    use: bob/held@v1\n"
    assert await workflows.check(setup, bob, draft) == ()

    found = await workflows.check(setup, bob, draft, workflow=made.id)
    message = (
        "`again` uses bob/held@v1, and bob/held is already on that path, so the workflow "
        "would hold itself."
    )
    assert found == ({"path": "steps[0].use", "message": message, "step": "again"},)
    repo = acme.fake.state.repos[("bob", "held.workflow")]
    acme.fake.state.commit(repo, {"workflow.yaml": draft.encode()}, "self", 8)
    with pytest.raises(InvalidDefinition) as refused:
        await workflows.create_version(setup, bob, "bob", "held", "v2")
    assert refused.value.errors == [{"path": "steps[0].use", "message": message, "step": "again"}]


async def test_a_box_that_cannot_be_opened_or_is_graded_test_by_test_is_refused(
    setup: Setup, acme: Acme
) -> None:
    _noop(acme, 1)
    bob = await _own(setup, acme)
    missing = "steps:\n  - id: box\n    use: bob/missing@v1\n"
    opened = await workflows.check(setup, bob, missing)
    assert opened == (
        {
            "path": "steps[0].use",
            "message": (
                "Unicon cannot open bob/missing@v1: there is no such primitive or workflow "
                "at that version, or it is not shared with you."
            ),
        },
    )
    graded = "steps:\n  - id: box\n    use: unicon/classic@v2\n"
    assert await workflows.check(setup, bob, graded) == (
        {
            "path": "steps[0].use",
            "message": (
                "unicon/classic@v2 is a workflow in the format graded test by test, "
                "and a box can only use a run workflow."
            ),
        },
    )


async def test_a_box_with_is_refused_for_an_unknown_key_a_missing_one_and_a_bad_value(
    setup: Setup, acme: Acme
) -> None:
    _run_primitives(acme)
    bob = await _own(setup, acme)
    await _version(setup, acme, bob, "build", BUILD)

    unknown = """\
inputs:
  code: {type: folder}
steps:
  - id: build
    use: bob/build@v1
    with:
      code: ${{ inputs.code }}
      language: python
      extra: 1
"""
    missing = """\
inputs:
  code: {type: folder}
steps:
  - id: build
    use: bob/build@v1
    with:
      language: python
"""
    unfit = """\
inputs:
  code: {type: folder}
steps:
  - id: build
    use: bob/build@v1
    with:
      code: 1
      language: python
"""
    assert await workflows.check(setup, bob, unknown) == (
        {
            "path": "steps[0].with.extra",
            "message": "extra is not an input of bob/build@v1, whose inputs are code, language.",
        },
    )
    assert await workflows.check(setup, bob, missing) == (
        {
            "path": "steps[0].with.code",
            "message": "build gives no value for code, which bob/build@v1 needs.",
        },
    )
    assert await workflows.check(setup, bob, unfit) == (
        {
            "path": "steps[0].with.code",
            "message": "This takes a folder, not a number.",
        },
        {
            "path": "steps[0]",
            "message": "In `build/compile`: This takes a folder, not a number.",
            "step": "build/compile",
        },
    )


async def test_a_problem_inside_a_box_names_the_step_and_the_outer_box(
    setup: Setup, acme: Acme
) -> None:
    _run_primitives(acme)
    bob = await _own(setup, acme)
    await _version(setup, acme, bob, "comp", COMP)
    outer = """\
inputs:
  code: {type: folder}
steps:
  - id: build
    use: bob/comp@v1
    with:
      code: ${{ inputs.code }}
      language: python
      note: ${{ steps.nope.program }}
"""
    found = await workflows.check(setup, bob, outer)
    inside = [problem for problem in found if problem.get("step") == "build/compile"]
    assert inside == [
        {
            "path": "steps[0]",
            "message": (
                "In `build/compile`: A step's output can be given only whole, "
                "not written into text."
            ),
            "step": "build/compile",
        }
    ]


async def test_a_written_workflow_pin_is_refused(setup: Setup, acme: Acme) -> None:
    _run_primitives(acme)
    bob = await _own(setup, acme)
    text = """\
steps:
  - id: compile
    use: unicon/compile@v3
    with:
      source: ${{ workflow@1.src/ }}
      language: python
"""
    found = await workflows.check(setup, bob, text)
    assert [problem["path"] for problem in found] == ["steps[0].with.source"]
    assert "workflow@1.src/" in found[0]["message"]


async def test_an_exec_programs_step_inside_a_box_keeps_its_programs(
    setup: Setup, acme: Acme
) -> None:
    _run_primitives(acme)
    bob = await _own(setup, acme)
    await _version(setup, acme, bob, "play", PLAY)
    outer = """\
inputs:
  code: {type: folder}
  judge: {type: folder}
  manifest: {type: file}
  time_limit: {type: number}
  judge_time_limit: {type: number}
steps:
  - id: compile
    use: unicon/compile@v3
    with: {source: "${{ inputs.code }}", language: python}
  - id: judged
    use: unicon/compile@v3
    with: {source: "${{ inputs.judge }}", language: python}
  - id: play
    use: bob/play@v1
    with:
      program: ${{ steps.compile.program }}
      judge: ${{ steps.judged.program }}
      manifest: ${{ inputs.manifest }}
      time_limit: ${{ inputs.time_limit }}
      judge_time_limit: ${{ inputs.judge_time_limit }}
"""
    await _version(setup, acme, bob, "match", outer)
    expanded = await _expanded(setup, bob, outer)
    assert expanded.flat is not None and expanded.problems == []
    game = expanded.flat.workflow.steps[2]
    assert game.id == "play/game"
    assert set(game.with_["programs"]) == {"entrant", "judge"}
    assert game.with_["links"] == ["entrant -> judge", "judge -> entrant"]
    assert game.with_["programs"]["entrant"]["program"] == "${{ steps.compile.program }}"


async def test_the_cap_counts_every_use_of_the_expansion(setup: Setup, acme: Acme) -> None:
    _noop(acme, 32)
    bob = await _own(setup, acme)
    steps = "\n".join(
        f"  - id: n{number}\n    use: unicon/noop@v{number}" for number in range(1, 32)
    )
    await _version(setup, acme, bob, "wide", f"steps:\n{steps}\n")
    outer = "steps:\n  - id: box\n    use: bob/wide@v1\n  - id: extra\n    use: unicon/noop@v32\n"
    found = await workflows.check(setup, bob, outer)
    assert found == (
        {
            "path": "steps",
            "message": (
                "Expanded, this workflow uses 33 different primitive and workflow versions, "
                "and at most 32 are allowed. Use fewer."
            ),
        },
    )


TIMED = """\
inputs:
  code: {type: folder, title: Solution}
  language: {type: enum, options: [python], title: Language}
  note: {type: text, optional: true, title: Note}
steps:
  - id: compile
    use: unicon/compile@v3
    with:
      source: ${{ inputs.code }}
      language: ${{ inputs.language }}
      entry: ${{ inputs.note }}
  - id: run
    use: unicon/exec@v1
    with:
      program: ${{ steps.compile.program }}
      time_limit: 1
      memory_limit: 256
outputs:
  program: ${{ steps.compile.program }}
  time_ms: ${{ steps.run.time_ms }}
"""


async def test_a_run_workflow_version_is_a_box_with_its_inputs_and_outputs(
    setup: Setup, acme: Acme
) -> None:
    _run_primitives(acme)
    bob = await _own(setup, acme)
    await _version(setup, acme, bob, "timed", TIMED)
    await _version(setup, acme, bob, "timed", TIMED + "\n", version="v2")

    face = await workflows.box(setup, bob, "bob/timed@v1")
    later = await workflows.box(setup, bob, "bob/timed@v2")

    assert face == Box(
        True,
        (
            BoxInput("code", "folder", None, False, "Solution", None),
            BoxInput("language", "enum", ("python",), False, "Language", None),
            BoxInput("note", "text", None, True, "Note", None),
        ),
        (
            BoxOutput("program", "file", None, None, None, None, None),
            BoxOutput("time_ms", "number", None, None, "lower", 0, None),
        ),
        "v2",
    )
    assert later.newer_version is None


async def test_a_test_format_workflow_is_not_a_run_box(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)

    face = await workflows.box(setup, bob, "unicon/classic@v2")

    assert face == Box(False, (), (), None)


async def test_a_box_the_caller_cannot_read_is_not_found(setup: Setup, acme: Acme) -> None:
    _run_primitives(acme)
    bob = await _own(setup, acme)
    await _version(setup, acme, bob, "timed", TIMED)
    ada = await signed_in(setup, acme.fake, 7)

    with pytest.raises(NotFound, match="There is no workflow bob/timed@v1 that you can read"):
        await workflows.box(setup, ada, "bob/timed@v1")
