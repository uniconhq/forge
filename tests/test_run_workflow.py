"""The run workflow (SIMPLER-CORE.md 7): a file with a test block is
refused, `unicon/classic@v3` and `unicon/interactive@v1` from deploy's checkout parse
and type against the contract 6 primitives beside this repo, a report of a
manifest is checked against its `program.yaml` and means what that file
says, the ports only contract 6 has take their own shapes, each refusal of
the format and the typing names its path, and what a task's save settles
later is recorded: who controls each program, the reports of a manifest the
task gives, the judge's time limit and every place an input is given.
"""

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from forge.domain.blocks.numbers import Better
from forge.domain.primitives import PrimitiveDeclaration, parse_primitive
from forge.domain.program_manifest import MAX_MANIFEST
from forge.domain.run_workflow import (
    MANIFEST_FROM_STEP,
    Deferred,
    InputManifest,
    RunWorkflowCheck,
    TimeLimitOrder,
    WorkflowFiles,
    WorkflowManifest,
    check_run_workflow,
    manifest_paths,
)
from forge.domain.types import Type
from forge.domain.workflow_definition import (
    PIPELINE_STEPS,
    SELF_REPORTED_GIVEN,
    BadReference,
    Reference,
    RunWorkflow,
    parse_reference,
    parse_run_workflow,
    reserved_message,
)
from forge.domain.yaml_models import InvalidDefinition, Problem, load_yaml, one_line
from forge.testing import PLACEHOLDER_DIGEST, RUN_WORKFLOWS
from tests.conftest import sibling

CLASSIC_V3 = "unicon/classic@v3"
INTERACTIVE_V1 = "unicon/interactive@v1"
DIGEST = "sha256:" + "a" * 64


def mirrored(name: str) -> PrimitiveDeclaration:
    """`primitive-<name>`'s own `primitive.yaml` from the checkout beside
    this one, with the image line bootstrap writes.
    """
    text = sibling(f"primitive-{name}", "primitive.yaml").read_text(encoding="utf-8")
    return parse_primitive(f"image: ghcr.io/uniconhq/primitive-{name}@{PLACEHOLDER_DIGEST}\n{text}")


PICK = parse_primitive(
    f"""\
schema_version: 6
image: ghcr.io/acme/pick@{DIGEST}
limits: {{time_ms: 1, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1, gpus: 0}}
inputs:
  files: {{type: named_files, runs: false, names: [input, answer]}}
outputs:
  outcome: outcome
"""
)
"""A contract 6 primitive whose named files must have one of two names."""


@pytest.fixture(scope="module")
def primitives() -> dict[str, PrimitiveDeclaration]:
    """The primitives the two workflows use."""
    return {
        "unicon/exec@v1": mirrored("exec"),
        "unicon/compile@v3": mirrored("compile"),
        "acme/pick@v1": PICK,
    }


def folder(ref: str) -> Path:
    return sibling("deploy", RUN_WORKFLOWS[ref])


def document(ref: str) -> dict[str, Any]:
    found = load_yaml((folder(ref) / "workflow.yaml").read_bytes())
    assert isinstance(found, dict)
    return found


def files(ref: str, extra: Mapping[str, bytes] | None = None) -> WorkflowFiles:
    """The workflow's files in deploy's checkout, with `extra` added by
    path, as a version's commit would hold them.
    """
    top = folder(ref)
    held = {
        str(path.relative_to(top)): path.read_bytes()
        for path in top.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    held.update(extra or {})
    return WorkflowFiles(frozenset(held), held)


def checked(
    primitives: dict[str, PrimitiveDeclaration],
    given: dict[str, Any],
    workflow_files: WorkflowFiles | None,
) -> RunWorkflowCheck:
    return check_run_workflow(parse_run_workflow(one_line(given)), primitives, workflow_files)


def problems(
    primitives: dict[str, PrimitiveDeclaration],
    given: dict[str, Any],
    ref: str = CLASSIC_V3,
) -> list[Problem]:
    return checked(primitives, given, files(ref)).problems


def refused(given: dict[str, Any]) -> list[Problem]:
    with pytest.raises(InvalidDefinition) as raised:
        parse_run_workflow(one_line(given))
    return raised.value.errors


def step(given: dict[str, Any], step_id: str) -> dict[str, Any]:
    found: dict[str, Any] = next(each for each in given["steps"] if each["id"] == step_id)
    return found


# The format


def test_a_file_with_a_test_block_is_refused() -> None:
    run = parse_run_workflow("steps:\n  - {id: a, use: unicon/compile@v3}\n")
    assert isinstance(run, RunWorkflow)
    assert run.outputs == {}
    given = document(CLASSIC_V3)
    given["test"] = {"input": "file"}

    [problem] = refused(given)

    assert problem["path"] == "test"
    assert "repeat" in problem["message"]


@pytest.mark.parametrize("ref", list(RUN_WORKFLOWS))
def test_each_built_in_run_workflow_parses_and_types(
    primitives: dict[str, PrimitiveDeclaration], ref: str
) -> None:
    workflow = parse_run_workflow((folder(ref) / "workflow.yaml").read_bytes())

    assert isinstance(workflow, RunWorkflow)
    assert check_run_workflow(workflow, primitives, files(ref)).problems == []


def test_references_read_a_workflows_files_and_a_programs_outputs() -> None:
    assert parse_reference("workflow.diff/") == Reference("workflow", "diff/")
    assert parse_reference("workflow.diff/program.yaml") == Reference(
        "workflow", "diff/program.yaml"
    )
    entrant = parse_reference("steps.game.entrant.time_ms")
    assert entrant == Reference("steps", "game", "time_ms", "entrant")
    assert entrant.port == "entrant.time_ms"
    assert str(entrant) == "${{ steps.game.entrant.time_ms }}"
    assert str(Reference("workflow", "diff/")) == "${{ workflow.diff/ }}"
    for bad in (
        "workflow.",
        "workflow./x",
        "workflow.a//b",
        "workflow.../x",
        "workflow.a/../b",
        "workflow.a\\b",
        "workflow.a\nb",
        "workflow.a\tb",
        "workflow.a\x7fb",
        "workflow. x",
        "workflow.x /y",
        "workflow.diff/ x",
    ):
        with pytest.raises(BadReference):
            parse_reference(bad)


def test_a_bad_reference_lists_the_forms_a_reference_has() -> None:
    for bad in ("secrets.key", "test.input"):
        with pytest.raises(BadReference) as raised:
            parse_reference(bad)
        assert str(raised.value) == (
            f"Unicon cannot read ${{{{ {bad} }}}}. Write ${{{{ inputs.<id> }}}}, "
            "${{ steps.<id>.<output> }}, ${{ steps.<id>.<program>.<output> }} or "
            "${{ workflow.<path> }}."
        )
    with pytest.raises(BadReference) as spaced:
        parse_reference("workflow.diff/ x")
    assert str(spaced.value) == (
        "Unicon cannot read ${{ workflow.diff/ x }}. No part of the path can start or end "
        "with a space."
    )


@pytest.mark.parametrize(
    ("path", "key"),
    [
        (("inputs", "code"), "contestant"),
        (("inputs", "code"), "per_test"),
        (("steps", 0), "per_test"),
        (("outputs", "time_ms"), "fold"),
        (("outputs", "time_ms"), "better"),
        (("outputs", "time_ms"), "at_least"),
    ],
)
def test_what_a_run_workflow_does_not_have_is_refused_at_its_path(
    path: tuple[str | int, ...], key: str
) -> None:
    given = document(CLASSIC_V3)
    container: Any = given
    for part in path:
        container = container[part]
    container[key] = True if key in ("contestant", "per_test") else 1

    [problem] = refused(given)

    where = ".".join(f"[{part}]" if isinstance(part, int) else part for part in path)
    assert problem["path"] == f"{where.replace('.[', '[')}.{key}"


@pytest.mark.parametrize("key", ["report", "reports"])
def test_a_reports_key_is_named_outputs(key: str) -> None:
    given = document(CLASSIC_V3)
    given[key] = given.pop("outputs")

    [problem] = refused(given)

    assert problem["path"] == key
    assert "Rename it outputs." in problem["message"]


def test_a_file_output_is_not_a_column(primitives: dict[str, PrimitiveDeclaration]) -> None:
    given = document(CLASSIC_V3)
    given["outputs"]["program"] = "${{ steps.compile.program }}"

    found = checked(primitives, given, files(CLASSIC_V3))

    assert found.problems == []
    assert "program" not in found.reports
    assert found.outputs["program"].port is not None
    assert found.outputs["program"].port.type is Type.FILE
    assert "score" in found.reports


@pytest.mark.parametrize("name", ["verdict", "passed", "time_limit"])
def test_a_report_named_verdict_passed_or_like_an_input_is_refused(name: str) -> None:
    given = document(CLASSIC_V3)
    given["outputs"][name] = "${{ steps.solution.time_ms }}"

    [problem] = refused(given)

    assert problem["path"] == f"outputs.{name}"
    if name == "time_limit":
        assert "an input of the workflow" in problem["message"]
    else:
        assert "what it gives every run" in problem["message"]


@pytest.mark.parametrize("name", ["verdict", "passed", "outcome", "points"])
def test_an_input_named_for_what_unicon_gives_is_refused(name: str) -> None:
    given = document(CLASSIC_V3)
    given["inputs"][name] = "number"

    [problem] = refused(given)

    assert problem == {"path": f"inputs.{name}", "message": reserved_message(name)}


@pytest.mark.parametrize("name", ["time-limit", "1st", "Code", "_x", "a" * 65])
def test_an_input_is_named_as_a_column_a_formula_names(name: str) -> None:
    given = document(CLASSIC_V3)
    given["inputs"][name] = "number"

    [problem] = refused(given)

    assert problem["path"] == f"inputs.{name}"
    assert problem["message"].startswith("An input's name may use at most 64 lower case letters")


def test_a_workflow_wiring_self_reported_is_refused() -> None:
    given = document(CLASSIC_V3)
    step(given, "compare")["with"]["self_reported"] = True

    assert refused(given) == [
        {"path": "steps[3].with.self_reported", "message": SELF_REPORTED_GIVEN}
    ]


@pytest.mark.parametrize("name", PIPELINE_STEPS)
def test_a_step_cannot_take_the_name_of_a_step_unicon_adds(name: str) -> None:
    given = document(CLASSIC_V3)
    step(given, "diff")["id"] = name

    assert refused(given) == [
        {
            "path": "steps[2].id",
            "message": f"{name} is the name of a step Unicon adds to every grading beside the "
            "workflow's own, so a workflow step cannot take it. Choose another id.",
        }
    ]


def test_a_program_given_self_reported_is_refused(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(INTERACTIVE_V1)
    step(given, "game")["with"]["programs"]["judge"]["self_reported"] = True

    assert problems(primitives, given, INTERACTIVE_V1) == [
        {"path": "steps[2].with.programs.judge.self_reported", "message": SELF_REPORTED_GIVEN}
    ]


def test_a_program_setting_named_by_a_number_is_refused_beside_unknown_names(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(INTERACTIVE_V1)
    judge = step(given, "game")["with"]["programs"]["judge"]
    judge[7] = "x"
    judge["zeta"] = "x"

    assert problems(primitives, given, INTERACTIVE_V1) == [
        {
            "path": "steps[2].with.programs.judge.7",
            "message": "7 is not an input's name. Remove it.",
        },
        {
            "path": "steps[2].with.programs.judge.zeta",
            "message": "unicon/exec@v1 has no input called zeta. Remove it.",
        },
    ]


# Steps and their ports


def test_a_run_workflow_has_no_test_fields(primitives: dict[str, PrimitiveDeclaration]) -> None:
    given = document(CLASSIC_V3)
    step(given, "solution")["with"]["stdin"] = "${{ test.input }}"

    [problem] = problems(primitives, given)

    assert problem["path"] == "steps[1].with.stdin"
    assert problem["message"].startswith("Unicon cannot read ${{ test.input }}.")


def test_a_step_output_cannot_raise_a_limit(primitives: dict[str, PrimitiveDeclaration]) -> None:
    given = document(INTERACTIVE_V1)
    step(given, "game")["with"]["programs"]["judge"]["memory_limit"] = (
        "${{ steps.compile.program }}"
    )
    step(given, "decide")["with"]["time_limit"] = "${{ steps.game.entrant.time_ms }}"

    found = problems(primitives, given, INTERACTIVE_V1)

    assert found == [
        {
            "path": "steps[2].with.programs.judge.memory_limit",
            "message": "This value raises a limit, so it must be known when the task is saved.",
        },
        {
            "path": "steps[4].with.time_limit",
            "message": "This value raises a limit, so it must be known when the task is saved.",
        },
    ]


def test_an_optional_output_reaches_only_an_optional_port(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(CLASSIC_V3)
    step(given, "compare")["with"]["args"] = "${{ steps.solution.message }}"
    step(given, "solution")["with"]["args"] = "${{ steps.compile.log }}"

    assert problems(primitives, given) == []
    step(given, "compare")["with"]["files"]["note"] = "${{ steps.solution.message }}"
    assert problems(primitives, given) == [
        {
            "path": "steps[3].with.files.note",
            "message": "A named file is one file, not text.",
        }
    ]


# Files of the workflow


def test_a_missing_file_of_the_workflow_is_refused(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(CLASSIC_V3)
    step(given, "compare")["with"]["manifest"] = "${{ workflow.missing }}"
    step(given, "diff")["with"]["source"] = "${{ workflow.missing/ }}"

    assert problems(primitives, given) == [
        {"path": "steps[2].with.source", "message": "The workflow has no folder missing/."},
        {"path": "steps[3].with.manifest", "message": "The workflow has no file missing."},
    ]


def test_a_folder_of_the_workflow_is_not_a_file(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(CLASSIC_V3)
    step(given, "compare")["with"]["manifest"] = "${{ workflow.diff }}"

    assert problems(primitives, given) == [
        {
            "path": "steps[3].with.manifest",
            "message": "diff is a folder of the workflow, not a file. A folder is written with "
            "a / at its end, ${{ workflow.diff/ }}, and given only where a folder is taken.",
        }
    ]
    step(given, "compare")["with"]["manifest"] = "${{ workflow.diff/ }}"
    assert problems(primitives, given) == [
        {"path": "steps[3].with.manifest", "message": "This takes a file, not a folder."}
    ]


def test_a_file_fits_a_folder_port_and_is_never_written_into_text(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(CLASSIC_V3)
    step(given, "diff")["with"]["source"] = "${{ workflow.diff/diff.py }}"
    assert problems(primitives, given) == []

    step(given, "solution")["with"]["args"] = "--data ${{ workflow.diff/diff.py }}"
    assert problems(primitives, given) == [
        {
            "path": "steps[1].with.args",
            "message": "A file of the workflow can be given only whole, not written into text.",
        }
    ]


def test_without_its_files_a_workflows_paths_are_checked_for_their_form_alone(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(CLASSIC_V3)
    step(given, "diff")["with"]["source"] = "${{ workflow.elsewhere/ }}"

    assert checked(primitives, given, None).problems == []


# Manifests and what they report


def test_a_report_of_the_diff_script_is_checked_against_its_program_yaml(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    found = checked(primitives, document(CLASSIC_V3), files(CLASSIC_V3))
    score, time_ms = found.reports["score"], found.reports["time_ms"]

    assert found.problems == []
    assert manifest_paths(parse_run_workflow(one_line(document(CLASSIC_V3)))) == {
        "diff/program.yaml"
    }
    assert found.steps["compare"].outputs["score"].type is Type.NUMBER
    compare = found.steps["compare"].program
    assert isinstance(compare.manifest, WorkflowManifest)
    assert compare.manifest.path == "diff/program.yaml"
    assert score.made_by == compare
    meaning = score.meaning("unicon/exec@v1")
    assert meaning is not None
    assert (meaning.made_by, meaning.better, meaning.at_least, meaning.at_most) == (
        "diff/program.yaml",
        Better.HIGHER,
        0,
        1,
    )
    meaning = time_ms.meaning("unicon/exec@v1")
    assert meaning is not None
    assert (meaning.made_by, meaning.title, meaning.unit, meaning.better, meaning.at_least) == (
        "unicon/exec@v1",
        "Run time",
        "ms",
        Better.LOWER,
        0,
    )
    assert time_ms.made_by is None


def test_an_output_neither_the_primitive_nor_the_manifest_gives_is_refused_naming_it(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(CLASSIC_V3)
    given["outputs"]["nope"] = "${{ steps.compare.nope }}"

    assert problems(primitives, given) == [
        {
            "path": "outputs.nope",
            "message": "The step compare has no output called nope: neither unicon/exec@v1 "
            "gives it nor diff/program.yaml reports it.",
        }
    ]


def test_a_program_yaml_that_does_not_read_is_refused_at_the_manifest_naming_the_file(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    broken = files(CLASSIC_V3, {"diff/program.yaml": b"reports:\n  verdict: {}\n"})
    found = checked(primitives, document(CLASSIC_V3), broken)

    assert found.problems == [
        {
            "path": "steps[3].with.manifest",
            "message": "diff/program.yaml, at reports.verdict: verdict is a name the primitive "
            "running the program gives itself. Report this under another name.",
        }
    ]


def test_a_program_yaml_over_the_size_is_refused_at_the_manifest(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    padded = b"reports: {}\n" + b"#" * MAX_MANIFEST
    found = checked(
        primitives, document(CLASSIC_V3), files(CLASSIC_V3, {"diff/program.yaml": padded})
    )

    assert found.problems == [
        {
            "path": "steps[3].with.manifest",
            "message": "diff/program.yaml: A program.yaml can be at most 256 KB. Make it smaller.",
        }
    ]


def test_a_manifest_from_a_steps_output_is_refused(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(CLASSIC_V3)
    step(given, "compare")["with"]["manifest"] = "${{ steps.solution.output }}"
    del given["outputs"]["score"]

    assert problems(primitives, given) == [
        {"path": "steps[3].with.manifest", "message": MANIFEST_FROM_STEP}
    ]


def test_a_manifest_from_an_input_leaves_its_reports_to_the_tasks_save(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(CLASSIC_V3)
    given["inputs"]["checker"] = "file"
    step(given, "compare")["with"]["manifest"] = "${{ inputs.checker }}"
    given["outputs"]["score"] = {"from": "${{ steps.compare.score }}", "unit": "points"}

    found = checked(primitives, given, files(CLASSIC_V3))

    assert found.problems == []
    assert found.deferred == [Deferred("outputs.score", "compare", None, "score", None, True)]
    assert found.reports["score"].port is None
    assert found.reports["score"].meaning("unicon/exec@v1") is None
    manifest = found.steps["compare"].program.manifest
    assert isinstance(manifest, InputManifest) and manifest.input == "checker"
    assert "time_ms" in manifest.own_names and "verdict" in manifest.own_names


def test_a_report_of_a_program_the_entrants_code_reaches_records_its_inputs(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(CLASSIC_V3)
    step(given, "compare")["with"]["program"] = "${{ steps.compile.program }}"

    found = checked(primitives, given, files(CLASSIC_V3))

    assert found.problems == []
    assert found.reports["score"].self_reported_by == {"code", "language"}
    assert found.reports["time_ms"].self_reported_by == frozenset()
    assert found.steps["compare"].program.self_reportable is True
    assert found.steps["compile"].program.controlled_by == {"code"}
    assert found.steps["compare"].verdict_self_reported_by == {"code", "language"}
    assert found.steps["solution"].verdict_self_reported_by == frozenset()
    assert found.verdict_self_reported_by == {"code", "language"}


def test_an_input_the_organisers_program_loads_makes_its_reports_self_reported(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(CLASSIC_V3)
    given["inputs"]["helper"] = {"type": "file"}
    step(given, "compare")["with"]["loads"] = {"helper.py": "${{ inputs.helper }}"}
    given["outputs"]["note"] = "${{ steps.compare.message }}"

    found = checked(primitives, given, files(CLASSIC_V3))

    assert found.problems == []
    assert found.steps["compare"].program.controlled_by == {"helper"}
    assert found.reports["score"].self_reported_by == {"helper"}
    assert found.reports["note"].self_reported_by == {"helper"}
    assert found.steps["compare"].verdict_self_reported_by == {"helper"}
    assert found.verdict_self_reported_by == {"helper"}


def test_the_verdict_and_texts_of_the_organisers_programs_are_not_self_reported(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(CLASSIC_V3)
    given["outputs"]["note"] = "${{ steps.compare.message }}"

    found = checked(primitives, given, files(CLASSIC_V3))

    assert found.problems == []
    assert found.verdict_self_reported_by == frozenset()
    assert found.reports["note"].written_by == found.steps["compare"].program
    assert found.reports["note"].self_reported_by == frozenset()


@pytest.mark.parametrize("text", ["label", "message"])
def test_a_text_the_entrants_program_writes_beside_its_reports_is_self_reported(
    primitives: dict[str, PrimitiveDeclaration], text: str
) -> None:
    given = document(CLASSIC_V3)
    step(given, "compare")["with"]["program"] = "${{ steps.compile.program }}"
    given["outputs"]["note"] = f"${{{{ steps.compare.{text} }}}}"
    given["outputs"]["solution_note"] = f"${{{{ steps.solution.{text} }}}}"

    found = checked(primitives, given, files(CLASSIC_V3))

    assert found.problems == []
    assert found.reports["note"].self_reported_by == {"code", "language"}
    assert found.reports["note"].made_by is None
    assert found.reports["solution_note"].self_reported_by == frozenset()


@pytest.mark.parametrize("output", ["staff_message", "extras"])
def test_a_report_never_names_what_only_staff_see(
    primitives: dict[str, PrimitiveDeclaration], output: str
) -> None:
    given = document(CLASSIC_V3)
    given["outputs"]["note"] = f"${{{{ steps.compare.{output} }}}}"

    found = checked(primitives, given, files(CLASSIC_V3))

    assert found.problems == [
        {
            "path": "outputs.note",
            "message": f"{output} is only for staff to see, and an output is read by a task and "
            "by a box. Give another output, or leave this one to staff.",
        }
    ]


def test_the_verdict_of_a_program_on_the_programs_form_is_recorded_per_program(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    found = checked(primitives, document(INTERACTIVE_V1), files(INTERACTIVE_V1))

    assert found.problems == []
    assert found.steps["game"].verdict_self_reported_by == {"judge", "judge_language"}
    assert found.steps["decide"].verdict_self_reported_by == frozenset()
    assert found.verdict_self_reported_by == {"judge", "judge_language"}


def test_a_programs_reports_are_its_outputs_and_may_be_missing(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(INTERACTIVE_V1)
    step(given, "game")["with"]["programs"]["judge"]["manifest"] = (
        "${{ workflow.judge/program.yaml }}"
    )
    given["outputs"]["score"] = "${{ steps.game.judge.score }}"
    judge_files = files(
        INTERACTIVE_V1,
        {"judge/program.yaml": b"reports:\n  score: {better: higher, at_least: 0, at_most: 1}\n"},
    )

    found = checked(primitives, given, judge_files)

    assert found.problems == []
    score = found.steps["game"].outputs["judge.score"]
    assert (score.type, score.optional, score.better) == (Type.NUMBER, True, Better.HIGHER)
    assert found.reports["score"].self_reported_by == {"judge", "judge_language"}
    step(given, "decide")["with"]["time_limit"] = "${{ steps.game.judge.score }}"
    given["outputs"]["oops"] = "${{ steps.game.entrant.score }}"
    assert checked(primitives, given, judge_files).problems == [
        {
            "path": "steps[4].with.time_limit",
            "message": "This value raises a limit, so it must be known when the task is saved.",
        },
        {
            "path": "outputs.oops",
            "message": "The step game has no output called entrant.score: unicon/exec@v1 "
            "gives no score of a program, and entrant is given no manifest that reports one.",
        },
    ]


def test_interactive_v1_records_the_entrant_input_reaching_only_the_entrant_program(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(INTERACTIVE_V1)
    given["outputs"]["score"] = "${{ steps.game.judge.score }}"

    found = checked(primitives, given, files(INTERACTIVE_V1))

    assert found.problems == []
    entrant, judge = found.steps["game"].programs["entrant"], found.steps["game"].programs["judge"]
    assert entrant.controlled_by == {"code", "language"}
    assert entrant.manifest is None
    assert judge.controlled_by == {"judge", "judge_language"}
    assert isinstance(judge.manifest, InputManifest)
    assert judge.manifest.input == "judge_manifest"
    assert "facts" in judge.manifest.own_names
    assert judge.self_reportable is False
    assert found.reports["score"].self_reported_by == {"judge", "judge_language"}
    assert found.deferred == [Deferred("outputs.score", "game", "judge", "score", None)]
    assert found.time_limits == [
        TimeLimitOrder(
            "steps[2].with.programs.judge.time_limit",
            "game",
            "judge",
            "judge_time_limit",
            "entrant",
            "time_limit",
        )
    ]
    assert [use.raises_limit for use in found.uses["judge_time_limit"]] == [True]
    assert [use.path for use in found.uses["input"]] == ["steps[2].with.programs.judge.files.input"]


def test_a_judges_time_limit_must_be_longer_where_the_save_knows_both(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(INTERACTIVE_V1)
    programs = step(given, "game")["with"]["programs"]
    programs["entrant"]["time_limit"] = 2
    programs["judge"]["time_limit"] = 2

    [problem] = problems(primitives, given, INTERACTIVE_V1)

    assert problem["path"] == "steps[2].with.programs.judge.time_limit"
    assert "needs a longer time limit than entrant's 2" in problem["message"]
    programs["judge"]["time_limit"] = Decimal("4.5")
    assert checked(primitives, given, files(INTERACTIVE_V1)).time_limits == []
    assert problems(primitives, given, INTERACTIVE_V1) == []
    programs["judge"]["time_limit"] = programs["entrant"]["time_limit"] = "${{ inputs.time_limit }}"
    assert [problem["path"] for problem in problems(primitives, given, INTERACTIVE_V1)] == [
        "steps[2].with.programs.judge.time_limit"
    ]


def test_two_programs_given_manifests_are_not_held_to_each_others_time_limit(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(INTERACTIVE_V1)
    programs = step(given, "game")["with"]["programs"]
    programs["referee"] = {**programs["judge"], "time_limit": 3}
    programs["judge"]["time_limit"] = 3
    programs["entrant"]["time_limit"] = 1
    step(given, "game")["with"]["links"] += ["referee -> judge", "judge -> referee"]

    found = checked(primitives, given, files(INTERACTIVE_V1))

    assert found.problems == []
    assert found.time_limits == []
    programs["entrant"]["time_limit"] = 3
    assert [problem["path"] for problem in problems(primitives, given, INTERACTIVE_V1)] == [
        "steps[2].with.programs.judge.time_limit",
        "steps[2].with.programs.referee.time_limit",
    ]


# Named files, programs and links


@pytest.mark.parametrize("name", [".x", "a/b", "..", ""])
def test_a_named_file_is_called_by_a_plain_name(
    primitives: dict[str, PrimitiveDeclaration], name: str
) -> None:
    given = document(CLASSIC_V3)
    step(given, "compare")["with"]["files"][name] = "${{ inputs.input }}"

    [problem] = problems(primitives, given)

    assert problem["path"] == f"steps[3].with.files.{name}"
    assert problem["message"].startswith(f"{name!r} cannot be a file's name.")


def test_a_named_file_is_one_file_given_once_by_a_name_the_port_allows(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(CLASSIC_V3)
    compare = step(given, "compare")["with"]
    compare["loads"] = {"answer": "${{ inputs.answer }}", "lib.py": "${{ inputs.code }}"}
    given["steps"].append(
        {
            "id": "pick",
            "use": "acme/pick@v1",
            "with": {"files": {"input": "${{ inputs.input }}", "other": "${{ inputs.answer }}"}},
        }
    )

    assert problems(primitives, given) == [
        {
            "path": "steps[3].with.loads.answer",
            "message": "answer is already given to this program. Each name is one file in its "
            "working directory.",
        },
        {
            "path": "steps[3].with.loads.lib.py",
            "message": "A named file is one file, not a folder.",
        },
        {
            "path": "steps[4].with.files.other",
            "message": "This port takes only the names input, answer.",
        },
    ]


def test_a_programs_value_names_one_to_eight_programs_by_plain_names(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(INTERACTIVE_V1)
    programs = step(given, "game")["with"]["programs"]
    entrant = programs["entrant"]
    programs["Judge"] = dict(entrant)
    programs["wall_clock"] = dict(entrant)
    programs["entrant"] = {**entrant, "colour": "blue"}
    del programs["judge"]["program"]

    paths = [problem["path"] for problem in problems(primitives, given, INTERACTIVE_V1)]

    assert paths == [
        "steps[2].with.programs.entrant.colour",
        "steps[2].with.programs.judge.program",
        "steps[2].with.programs.Judge",
        "steps[2].with.programs.wall_clock",
    ]
    for number in range(9):
        programs[f"p{number}"] = dict(entrant)
    assert "steps[2].with.programs" in [
        problem["path"] for problem in problems(primitives, given, INTERACTIVE_V1)
    ]


def test_a_link_joins_two_programs_the_step_runs(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(INTERACTIVE_V1)
    step(given, "game")["with"]["links"] += [
        "entrant -> nobody",
        "judge -> judge",
        "judge -> entrant",
        "entrant->judge",
    ]

    assert problems(primitives, given, INTERACTIVE_V1) == [
        {
            "path": "steps[2].with.links[2]",
            "message": "nobody is not one of the step's programs.",
        },
        {"path": "steps[2].with.links[3]", "message": "judge cannot be linked to itself."},
        {
            "path": "steps[2].with.links[4]",
            "message": "'judge -> entrant' is already listed above. Remove this one, or change it.",
        },
        {
            "path": "steps[2].with.links[5]",
            "message": 'Write each link as "<from> -> <to>", two program names, such as '
            '"entrant -> judge".',
        },
    ]


def test_a_named_file_cannot_take_a_pipes_name(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    given = document(INTERACTIVE_V1)
    game = step(given, "game")["with"]
    game["programs"]["helper"] = dict(game["programs"]["entrant"])
    game["links"] += ["judge -> helper", "helper -> judge"]
    game["programs"]["judge"]["files"]["to_helper"] = "${{ inputs.input }}"

    assert problems(primitives, given, INTERACTIVE_V1) == [
        {
            "path": "steps[2].with.programs.judge",
            "message": "judge has a named file to_helper, the name of one of its pipes from the "
            "links. Call the file something else.",
        }
    ]


# Sizes


def test_a_sum_or_a_max_over_the_programs_sizes_the_container(
    primitives: dict[str, PrimitiveDeclaration],
) -> None:
    formed = primitives["unicon/exec@v1"]
    several = formed.for_step(("programs",), "unicon/exec@v1")
    assert isinstance(several, PrimitiveDeclaration)
    exec_ = formed
    programs: dict[str, dict[str, int | Decimal]] = {
        "entrant": {"time_limit": 1, "memory_limit": 256},
        "judge": {"time_limit": Decimal("2.5"), "memory_limit": 64},
    }

    assert several.item_limits({}, programs) == {
        "time_ms": 16000,
        "cpu_ms": 16000,
        "memory_mb": 832,
        "pids": 1056,
        "output_mb": 64,
        "gpus": 0,
    }
    assert several.item_limits({}, {"only": {"time_limit": Decimal("0.0001")}})["memory_mb"] == 512
    assert several.limits is not None
    assert several.item_limits({}) == several.limits.as_mapping()
    one = exec_.for_step(("program", "time_limit", "memory_limit"), "unicon/exec@v1")
    assert isinstance(one, PrimitiveDeclaration)
    sized = one.item_limits({"time_limit": 10, "memory_limit": 256})
    assert (sized["time_ms"], sized["memory_mb"]) == (23000, 512)


TWO = parse_primitive(
    f"""\
schema_version: 6
image: ghcr.io/acme/two@{DIGEST}
forms:
  one:
    limits: {{time_ms: 1, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1, gpus: 0}}
    inputs:
      program: {{type: text}}
    outputs:
      outcome: outcome
  several:
    limits: {{time_ms: 2, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1, gpus: 0}}
    inputs:
      bundle: {{type: text}}
    outputs:
      outcome: outcome
"""
)


def test_a_step_giving_one_forms_inputs_checks_as_that_form() -> None:
    given = {"steps": [{"id": "run", "use": "acme/two@v1", "with": {"program": "hello"}}]}
    found = checked({"acme/two@v1": TWO}, given, None)

    assert found.problems == []
    resolved = found.steps["run"].declaration
    assert resolved.forms == {} and set(resolved.inputs) == {"program"}
    assert resolved.limits is not None and resolved.limits.time_ms == 1


def test_a_step_giving_inputs_of_both_forms_or_of_neither_is_refused() -> None:
    held = {"acme/two@v1": TWO}
    words = (
        "The step's inputs fit no form of acme/two@v1. "
        "Give every required input of one form and no input it lacks: "
        "one (program), several (bundle)."
    )
    both = {
        "steps": [
            {"id": "run", "use": "acme/two@v1", "with": {"program": "hello", "bundle": "there"}}
        ]
    }
    neither = {"steps": [{"id": "run", "use": "acme/two@v1"}]}
    assert problems(held, both) == [{"path": "steps[0].use", "message": words}]
    assert problems(held, neither) == [{"path": "steps[0].use", "message": words}]
