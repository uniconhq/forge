"""The workflow format: the built-in classic workflow parses, a reference reads
as owner/name@version and a value's references are found in it, a malformed
workflow and every key the format no longer has are refused at their paths,
the report's own rules hold (W1 to W5), and a workflow is checked against
the primitives its steps use as a version is (`plans.check_workflow`): every
port given a value of its type, under the two widenings, from a reference
that may feed it.
"""

from typing import Any

import pytest
import yaml

from forge.domain.errors import InvalidName
from forge.domain.plans import check_workflow
from forge.domain.primitives import PrimitiveDeclaration, parse_primitive
from forge.domain.types import Type
from forge.domain.workflow_definition import (
    BadReference,
    Fold,
    Meaning,
    Reference,
    WorkflowRef,
    WorkflowStep,
    parse_workflow,
    parse_workflow_ref,
    references,
    starter_workflow,
    whole_reference,
)
from forge.domain.yaml_models import InvalidDefinition, Problem
from forge.testing import CLASSIC, PLACEHOLDER_DIGEST, PRIMITIVES
from tests.conftest import sibling

SEEDED = {f"unicon/{name}@v2": parse_primitive(text) for name, text in PRIMITIVES.items()}


def declared(name: str, rest: str) -> PrimitiveDeclaration:
    return parse_primitive(f"image: ghcr.io/acme/{name}@{PLACEHOLDER_DIGEST}\n" + rest)


LIMITS = "limits: {time_ms: 1000, cpu_ms: 1000, memory_mb: 64, pids: 8, output_mb: 1, gpus: 0}\n"

ECHO = declared(
    "echo",
    LIMITS
    + """\
inputs:
  message: text
  note: {type: text, optional: true}
  times: number
  mode: {type: enum, options: [fast, slow]}
outputs:
  hint: {type: text, optional: true}
  said: text
  size: number
  outcome: outcome
""",
)
"""A primitive that runs nothing: a required and an optional text port, a
number port, an enum port, an optional output and a number output."""

PRIMITIVES_WITH_ECHO = {**SEEDED, "acme/echo@v1": ECHO}


def classic() -> dict[str, Any]:
    document = yaml.safe_load(CLASSIC)
    assert isinstance(document, dict)
    return document


def refused(document: dict[str, Any]) -> list[Problem]:
    with pytest.raises(InvalidDefinition) as raised:
        parse_workflow(yaml.safe_dump(document, sort_keys=False))
    return raised.value.errors


def paths(problems: list[Problem]) -> list[str]:
    return [problem["path"] for problem in problems]


def checked(
    document: dict[str, Any], primitives: dict[str, PrimitiveDeclaration] | None = None
) -> list[Problem]:
    workflow = parse_workflow(yaml.safe_dump(document, sort_keys=False))
    return check_workflow(workflow, primitives if primitives is not None else PRIMITIVES_WITH_ECHO)


def with_echo(*, per_test: bool = False, **with_: Any) -> dict[str, Any]:
    """Classic with an echo step, once before every per-test step or per
    test at the end, given `with_` as its ports.
    """
    document = classic()
    step = {"id": "echo", "use": "acme/echo@v1", "per_test": per_test, "with": with_}
    if per_test:
        document["steps"].append(step)
    else:
        document["steps"].insert(1, step)
    return document


ECHO_PORTS: dict[str, Any] = {"message": "hi", "times": 1, "mode": "fast"}


# The file


def test_the_classic_workflow_parses() -> None:
    workflow = parse_workflow(CLASSIC)

    assert set(workflow.inputs) == {"submission", "language", "time_limit", "memory_limit"}
    submission, language = workflow.inputs["submission"], workflow.inputs["language"]
    assert (submission.type, submission.contestant) == (Type.FILE, True)
    assert language.options == ("c", "cpp", "java", "python")
    assert workflow.inputs["time_limit"].type is Type.NUMBER
    assert not workflow.inputs["time_limit"].contestant
    assert {name: field.type for name, field in workflow.test.items()} == {
        "input": Type.FILE,
        "answer": Type.FILE,
    }
    compile_, run, check = workflow.steps
    assert (compile_.id, compile_.use, compile_.per_test) == (
        "compile",
        WorkflowRef("unicon", "compile", "v2"),
        False,
    )
    assert (run.per_test, check.per_test) == (True, True)
    assert run.with_["time_limit"] == "${{ inputs.time_limit }}"
    assert workflow.report["log"] == "${{ steps.compile.compile_log }}"
    time_ms = workflow.report["time_ms"]
    assert isinstance(time_ms, Meaning)
    assert (time_ms.from_, time_ms.fold, time_ms.better, time_ms.at_least, time_ms.at_most) == (
        "${{ steps.run.time_ms }}",
        Fold.MAX,
        "lower",
        0,
        None,
    )
    assert check_workflow(workflow, SEEDED) == []


def test_the_seeded_classic_workflow_is_the_one_tested_here() -> None:
    seeded = sibling("deploy", "workflows", "classic", "v2", "workflow.yaml")
    assert parse_workflow(seeded.read_bytes()) == parse_workflow(CLASSIC)


def test_a_new_workflow_starts_as_classic() -> None:
    files = starter_workflow("acme", "sorting")

    assert parse_workflow(files["workflow.yaml"]) == parse_workflow(CLASSIC)


def test_a_declaration_may_be_just_its_types_name() -> None:
    document = classic()
    document["inputs"]["time_limit"] = {"type": "number"}

    assert parse_workflow(yaml.safe_dump(document)) == parse_workflow(CLASSIC)


@pytest.mark.parametrize(
    ("key", "value", "path", "sentence"),
    [
        ("name", "unicon/classic", "name", "its repo is its name"),
        ("version", "v2", "version", "its tag is its version"),
        ("copied_from", "unicon/classic@v1", "copied_from", "copied from"),
        ("outputs", {"metrics": {}}, "outputs", "`outputs` is now `report`"),
        ("inputs", [{"id": "submission", "type": "file"}], "inputs", "mapping of id to"),
    ],
)
def test_a_retired_workflow_key_is_refused_at_its_path_with_what_replaced_it(
    key: str, value: Any, path: str, sentence: str
) -> None:
    document = classic()
    document[key] = value

    problems = refused(document)

    assert paths(problems) == [path]
    assert sentence in problems[0]["message"]


@pytest.mark.parametrize(
    ("place", "declaration", "sentence"),
    [
        ("inputs", "code", "`code` is gone"),
        ("inputs", {"type": "file[]", "contestant": True}, "`file[]` is now `folder`."),
        ("inputs", "dataset", "`dataset` is now `file` or `folder`."),
        ("test", "jupyter", "`jupyter` is now `file`."),
    ],
)
def test_a_retired_type_is_refused_at_the_declaration(
    place: str, declaration: Any, sentence: str
) -> None:
    document = classic()
    document[place]["old"] = declaration

    problems = refused(document)

    assert paths(problems) == [f"{place}.old"]
    assert sentence in problems[0]["message"]


def test_foreach_is_refused_naming_per_test() -> None:
    document = classic()
    document["steps"][1]["foreach"] = "${{ inputs.testcases }}"

    problems = refused(document)

    assert problems == [
        {
            "path": "steps[1].foreach",
            "message": "`foreach` is now `per_test: true`, over the task's tests.",
        }
    ]


@pytest.mark.parametrize(
    ("change", "path"),
    [
        ({"description": "extra"}, "description"),
        ({"steps": []}, "steps"),
        ({"test": {}}, "test"),
        ({"test": None}, "test"),
        ({"inputs": {"Bad": "number"}}, "inputs.Bad"),
        ({"inputs": {"x": "binary"}}, "inputs.x.type"),
        ({"inputs": {"x": {"type": "enum"}}}, "inputs.x.options"),
        ({"inputs": {"x": {"type": "text", "options": ["a"]}}}, "inputs.x.options"),
        ({"inputs": {"x": {"type": "enum", "options": []}}}, "inputs.x.options"),
        ({"inputs": {"x": {"type": "enum", "options": ["a", "a"]}}}, "inputs.x.options"),
        ({"inputs": {"x": {"type": "text", "per_test": True}}}, "inputs.x.per_test"),
        (
            {"inputs": {"x": {"type": "folder", "contestant": True, "per_test": True}}},
            "inputs.x.per_test",
        ),
        (
            {"inputs": {"x": {"type": "file", "contestant": True, "optional": True}}},
            "inputs.x.optional",
        ),
        ({"inputs": {"x": {"type": "text", "label": "X"}}}, "inputs.x.label"),
        ({"test": {"input": {"type": "number", "public": True}}}, "test.input.public"),
        ({"report": {"Time": "${{ steps.run.time_ms }}"}}, "report.Time"),
        ({"report": {"x": 3}}, "report.x"),
    ],
)
def test_a_malformed_workflow_is_refused_at_its_path(change: dict[str, Any], path: str) -> None:
    document = classic()
    document.update(change)

    assert path in paths(refused(document))


def test_a_missing_key_is_refused_at_its_path() -> None:
    document = classic()
    del document["steps"]

    assert refused(document) == [{"path": "steps", "message": "This key is required."}]


def test_a_key_given_twice_is_refused() -> None:
    with pytest.raises(InvalidDefinition) as raised:
        parse_workflow(CLASSIC + b"test:\n  input: file\n")

    assert "the key 'test' is given twice" in raised.value.errors[0]["message"]


@pytest.mark.parametrize(
    ("change", "path"),
    [
        ({"id": "compile"}, "steps[1].id"),
        ({"id": "Run"}, "steps[1].id"),
        ({"use": "sandbox-run"}, "steps[1].use"),
        ({"per_test": "yes"}, "steps[1].per_test"),
        ({"with": ["binary"]}, "steps[1].with"),
        ({"needs": "compile"}, "steps[1].needs"),
    ],
)
def test_a_malformed_step_is_refused_at_its_path(change: dict[str, Any], path: str) -> None:
    document = classic()
    document["steps"][1] = {**document["steps"][1], **change}

    assert path in paths(refused(document))


def test_a_field_named_test_is_refused() -> None:
    document = classic()
    document["test"]["test"] = "number"

    assert refused(document) == [
        {"path": "test.test", "message": "No field is named test, since test.yaml would match it."}
    ]


def test_a_once_step_after_a_per_test_step_is_refused_at_the_version() -> None:
    document = classic()
    document["steps"].append(
        {"id": "late", "use": "unicon/compile@v2", "with": {"source": "${{ inputs.submission }}"}}
    )

    problems = refused(document)

    assert paths(problems) == ["steps[3].per_test"]
    assert "comes before every step that runs per test" in problems[0]["message"]


# W1 to W5


@pytest.mark.parametrize(
    ("entry", "path", "sentence"),
    [
        (
            {"from": "${{ steps.run.time_ms }}", "colour": "red"},
            "report.x.colour",
            "not part of the format",
        ),
        ({"fold": "max"}, "report.x.from", "This key is required."),
        ({"from": "${{ steps.run.time_ms }}", "fold": "min"}, "report.x.fold", "sum, mean or max"),
        ({"from": "${{ steps.run.time_ms }}", "better": "up"}, "report.x.better", "higher, lower"),
        (
            {"from": "${{ steps.run.time_ms }}", "better": "${{ inputs.language }}"},
            "report.x.better",
            "language must be an enum input the task gives, not the contestant.",
        ),
        (
            {"from": "${{ steps.run.time_ms }}", "better": "${{ inputs.time_limit }}"},
            "report.x.better",
            "time_limit must be an enum input the task gives",
        ),
        (
            {"from": "${{ steps.run.time_ms }}", "better": "${{ inputs.ghost }}"},
            "report.x.better",
            "ghost is not an input of the workflow.",
        ),
        (
            {"from": "${{ steps.run.time_ms }}", "at_least": 2, "at_most": 1},
            "report.x.at_most",
            "Must be at least at_least.",
        ),
        (
            {"from": "${{ steps.run.time_ms }}", "at_least": "none"},
            "report.x.at_least",
            "Must be a number.",
        ),
    ],
    ids=[
        "w1-key",
        "w1-from",
        "w3-fold",
        "w3-better",
        "w3-contestant",
        "w3-number",
        "w3-ghost",
        "w4",
        "number",
    ],
)
def test_a_report_entry_breaking_its_rules_is_refused_at_its_key(
    entry: dict[str, Any], path: str, sentence: str
) -> None:
    document = classic()
    document["report"]["x"] = entry

    problems = refused(document)

    assert path in paths(problems)
    assert sentence in next(problem["message"] for problem in problems if problem["path"] == path)


def test_a_better_that_is_no_reference_is_refused_at_its_key_beside_every_other_problem() -> None:
    document = classic()
    document["report"]["x"] = {"from": "${{ steps.run.time_ms }}", "better": "${{ foo }}"}
    document["report"]["points"] = "${{ steps.run.time_ms }}"

    problems = refused(document)

    assert paths(problems) == ["report.x.better", "report.points"]
    assert problems[0]["message"].startswith("${{ foo }} is not a reference a workflow makes")
    assert "points is the platform's own name" in problems[1]["message"]


def test_better_may_be_a_task_enum_of_higher_and_lower() -> None:
    document = classic()
    document["inputs"]["better"] = {"type": "enum", "options": ["higher", "lower"]}
    document["report"]["x"] = {"from": "${{ steps.run.time_ms }}", "better": "${{ inputs.better }}"}
    parse_workflow(yaml.safe_dump(document))

    document["inputs"]["better"] = {"type": "enum", "options": ["higher", "sideways"]}
    assert refused(document) == [
        {"path": "report.x.better", "message": "The options of better must be higher and lower."}
    ]


@pytest.mark.parametrize("name", ["outcome", "points", "penalty"])
def test_the_platforms_own_names_are_refused_as_reported_names(name: str) -> None:
    document = classic()
    document["report"][name] = "${{ steps.run.time_ms }}"

    problems = refused(document)

    assert paths(problems) == [f"report.{name}"]
    assert "the platform's own name" in problems[0]["message"]


# References


def test_a_reference_is_owner_name_and_version() -> None:
    ref = parse_workflow_ref("my-org/tunable-eval@1.0.2")
    assert ref == WorkflowRef("my-org", "tunable-eval", "1.0.2")
    assert str(ref) == "my-org/tunable-eval@1.0.2"


@pytest.mark.parametrize(
    "text",
    ["classic", "unicon/classic", "unicon/classic@", "Unicon/classic@v1", "a/b/c@v1", "a/b@-v1"],
)
def test_a_malformed_reference_is_refused(text: str) -> None:
    with pytest.raises(InvalidName):
        parse_workflow_ref(text)


def test_the_references_in_a_value_are_found() -> None:
    assert whole_reference("${{ inputs.time_limit }}") == Reference("inputs", "time_limit")
    assert whole_reference(" ${{test.input}} ") == Reference("test", "input")
    assert whole_reference("${{ steps.run.output }}") == Reference("steps", "run", "output")
    assert whole_reference("--n ${{ inputs.n }}") is None
    assert [found for _, found in references("--n ${{ inputs.n }} -e ${{ test.e }}")] == [
        Reference("inputs", "n"),
        Reference("test", "e"),
    ]
    assert str(Reference("steps", "run", "output")) == "${{ steps.run.output }}"


@pytest.mark.parametrize(
    "text", ["${{ secrets.key }}", "${{ inputs }}", "${{ steps.run }}", "a ${{ inputs.x"]
)
def test_a_reference_a_workflow_does_not_make_is_refused(text: str) -> None:
    with pytest.raises(BadReference):
        references(text)


# Checking a version against its primitives


def test_a_use_that_is_not_a_primitive_is_refused() -> None:
    document = classic()
    document["steps"][2]["use"] = "unicon/classic@v2"

    assert checked(document)[0] == {
        "path": "steps[2].use",
        "message": "unicon/classic@v2 is not a primitive.",
    }


def test_a_port_the_primitive_does_not_have_is_refused() -> None:
    document = classic()
    document["steps"][2]["with"]["colour"] = "red"

    assert checked(document) == [
        {"path": "steps[2].with.colour", "message": "unicon/diff-check@v2 has no input colour."}
    ]


def test_a_required_port_left_out_is_refused_and_an_optional_one_is_not() -> None:
    document = classic()
    del document["steps"][2]["with"]["expected"]

    assert checked(document) == [
        {
            "path": "steps[2].with.expected",
            "message": "unicon/diff-check@v2 needs the input expected.",
        }
    ]
    assert "args" not in classic()["steps"][1]["with"]


@pytest.mark.parametrize(
    ("port", "value", "message"),
    [
        ("times", "3", "Takes number, not text."),
        ("times", "${{ inputs.language }}", "Takes number, not enum."),
        ("times", True, "Takes number, not boolean."),
        ("message", "${{ inputs.submission }}", "Takes text, not file."),
        ("mode", "${{ inputs.time_limit }}", "Takes one of fast, slow."),
    ],
)
def test_a_value_of_another_type_than_its_port_is_refused(
    port: str, value: Any, message: str
) -> None:
    assert checked(with_echo(**{**ECHO_PORTS, port: value})) == [
        {"path": f"steps[1].with.{port}", "message": message}
    ]


@pytest.mark.parametrize(
    "value",
    [
        "${{ inputs.time_limit }}",
        "${{ inputs.language }}",
        True,
        2.5,
        "--limit ${{ inputs.time_limit }} in ${{ inputs.language }}",
    ],
)
def test_a_scalar_widens_to_text(value: Any) -> None:
    assert checked(with_echo(**{**ECHO_PORTS, "message": value})) == []


def test_a_file_widens_to_a_folder_and_a_folder_never_narrows_to_a_file() -> None:
    assert checked(classic()) == []  # compile's source is a folder, given the submission file

    document = classic()
    document["inputs"]["sources"] = {"type": "folder", "contestant": True}
    document["steps"][1]["with"]["input"] = "${{ inputs.sources }}"

    assert checked(document) == [
        {"path": "steps[1].with.input", "message": "Takes file, not folder."}
    ]


def test_an_enum_input_is_checked_against_the_ports_options() -> None:
    document = classic()
    document["inputs"]["language"]["options"] = ["c", "rust"]

    assert checked(document) == [
        {
            "path": "steps[0].with.language",
            "message": "Takes one of c, cpp, java, python, not rust.",
        }
    ]
    narrower = classic()
    narrower["inputs"]["language"]["options"] = ["python"]
    assert checked(narrower) == []


def test_a_literal_string_into_an_enum_port_is_one_of_its_options() -> None:
    fits = classic()
    fits["steps"][0]["with"]["language"] = "python"
    assert checked(fits) == []

    document = classic()
    document["steps"][0]["with"]["language"] = "rust"
    assert checked(document) == [
        {"path": "steps[0].with.language", "message": "Takes one of c, cpp, java, python."}
    ]


def test_an_optional_output_feeds_only_an_optional_port() -> None:
    document = with_echo(**ECHO_PORTS)
    document["steps"][2]["with"]["args"] = "${{ steps.echo.hint }}"
    assert checked(document) == []

    document["steps"].insert(
        2,
        {
            "id": "again",
            "use": "acme/echo@v1",
            "with": {**ECHO_PORTS, "message": "${{ steps.echo.hint }}"},
        },
    )
    assert checked(document) == [
        {
            "path": "steps[2].with.message",
            "message": "The output may be absent, so it feeds only an optional port.",
        }
    ]


def test_an_optional_input_feeds_only_an_optional_port_and_is_never_written_into_text() -> None:
    document = with_echo(**{**ECHO_PORTS, "note": "${{ inputs.key }}"})
    document["inputs"]["key"] = {"type": "text", "optional": True}
    assert checked(document) == []

    into_required = with_echo(**{**ECHO_PORTS, "message": "${{ inputs.key }}"})
    into_required["inputs"]["key"] = {"type": "text", "optional": True}
    assert checked(into_required) == [
        {
            "path": "steps[1].with.message",
            "message": "The input is optional, so it feeds only an optional port.",
        }
    ]

    written = with_echo(**{**ECHO_PORTS, "note": "key=${{ inputs.key }}"})
    written["inputs"]["key"] = {"type": "text", "optional": True}
    assert checked(written) == [
        {
            "path": "steps[1].with.note",
            "message": "${{ inputs.key }} is optional, so it is given whole to optional ports, "
            "never written into text.",
        }
    ]


def test_a_contestant_input_never_raises_a_limit() -> None:
    document = classic()
    document["inputs"]["seconds"] = {"type": "number", "contestant": True}
    document["steps"][1]["with"]["time_limit"] = "${{ inputs.seconds }}"

    assert checked(document) == [
        {
            "path": "steps[1].with.time_limit",
            "message": "This port raises a limit, so the task must give it, not the contestant.",
        }
    ]


def test_a_step_output_never_raises_a_limit() -> None:
    document = with_echo(**ECHO_PORTS)
    document["steps"][2]["with"]["time_limit"] = "${{ steps.echo.size }}"

    assert checked(document) == [
        {
            "path": "steps[2].with.time_limit",
            "message": "This port raises a limit, so it must be known at the save.",
        }
    ]


def test_a_literal_or_a_test_field_may_raise_a_limit() -> None:
    document = classic()
    document["test"]["seconds"] = "number"
    document["steps"][1]["with"]["time_limit"] = "${{ test.seconds }}"
    document["steps"][1]["with"]["memory_limit"] = 512

    assert checked(document) == []


def test_a_test_field_is_read_only_in_a_per_test_step() -> None:
    document = with_echo(**{**ECHO_PORTS, "note": "${{ test.input }}"})

    assert checked(document) == [
        {
            "path": "steps[1].with.note",
            "message": "test.<field> is there only in a step that runs per test.",
        }
    ]


def test_a_test_field_written_into_text_is_a_scalar() -> None:
    document = classic()
    document["test"]["episodes"] = "number"
    document["steps"][1]["with"]["args"] = "--episodes ${{ test.episodes }}"
    assert checked(document) == []

    document["steps"][1]["with"]["args"] = "--input ${{ test.input }}"
    assert checked(document) == [
        {
            "path": "steps[1].with.args",
            "message": "${{ test.input }} is a file, and only text, a number, true or false or "
            "an enum is written into text.",
        }
    ]


def test_a_per_test_input_is_read_only_in_a_per_test_step() -> None:
    document = classic()
    document["inputs"]["answers"] = {"type": "file", "contestant": True, "per_test": True}
    document["steps"][0]["with"]["source"] = "${{ inputs.answers }}"

    assert checked(document) == [
        {
            "path": "steps[0].with.source",
            "message": "answers is given once per test, so only a per-test step reads it.",
        }
    ]
    per_test = classic()
    per_test["inputs"]["answers"] = {"type": "file", "contestant": True, "per_test": True}
    per_test["steps"][2]["with"]["actual"] = "${{ inputs.answers }}"
    assert checked(per_test) == []


def test_a_once_step_reading_a_per_test_step_is_refused() -> None:
    workflow = parse_workflow(CLASSIC)
    reader = WorkflowStep.model_validate(
        {
            "id": "late",
            "use": "acme/echo@v1",
            "with": {**ECHO_PORTS, "note": "${{ steps.run.time_ms }}"},
        }
    )
    # A once step listed after a per-test step is refused as the file is
    # read; this one is put there past that check, to show the reference is
    # refused on its own.
    unordered = workflow.model_copy(update={"steps": (*workflow.steps, reader)})

    assert check_workflow(unordered, PRIMITIVES_WITH_ECHO) == [
        {
            "path": "steps[3].with.note",
            "message": "The step run runs per test, and a step that runs once cannot read it.",
        }
    ]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("${{ steps.later.output }}", "later is not a step before this one."),
        ("${{ steps.compile.nothing }}", "The step compile has no output nothing."),
        ("${{ steps.compile.outcome }}", "A step's outcome is read by the harness"),
        ("${{ inputs.ghost }}", "ghost is not an input of the workflow."),
        ("${{ test.ghost }}", "ghost is not a field of the workflow's tests."),
        ("${{ secrets.key }}", "is not a reference a workflow makes"),
        ("log: ${{ steps.compile.compile_log }}", "A step's output is given whole"),
        (["a"], "not a list, a mapping or nothing"),
        (None, "not a list, a mapping or nothing"),
    ],
)
def test_a_reference_that_points_at_nothing_it_may_read_is_refused(
    value: Any, message: str
) -> None:
    document = classic()
    document["steps"][1]["with"]["args"] = value

    problems = checked(document)

    assert paths(problems) == ["steps[1].with.args"]
    assert message in problems[0]["message"]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("${{ steps.run.output }}", "steps.run.output is file; a report holds text or numbers."),
        (
            "${{ steps.run.outcome }}",
            "steps.run.outcome is outcome; a report holds text or numbers.",
        ),
        ("${{ steps.ghost.size }}", "steps.ghost.size is not an output of a step."),
        ("${{ inputs.time_limit }}", "Must be ${{ steps.<id>.<output> }}."),
        (
            {"from": "${{ steps.compile.compile_log }}", "fold": "max"},
            "fold, better, at_least and at_most say what a number means; this is text.",
        ),
        (
            {"from": "${{ steps.compile.compile_log }}", "at_most": 1},
            "fold, better, at_least and at_most say what a number means; this is text.",
        ),
        (
            {"from": "${{ steps.echo.size }}", "fold": "sum"},
            "fold and better are for a number reported per test; this step runs once.",
        ),
        (
            {"from": "${{ steps.echo.size }}", "better": "higher"},
            "fold and better are for a number reported per test; this step runs once.",
        ),
    ],
)
def test_a_report_entry_reads_a_declared_port_of_a_type_it_holds(entry: Any, message: str) -> None:
    document = with_echo(**ECHO_PORTS)
    document["report"]["x"] = entry

    assert checked(document) == [{"path": "report.x", "message": message}]


def test_a_once_number_may_carry_bounds_and_a_per_test_one_a_fold_and_a_direction() -> None:
    document = with_echo(**ECHO_PORTS)
    document["report"]["size"] = {"from": "${{ steps.echo.size }}", "at_least": 0, "at_most": 10}
    document["report"]["said"] = "${{ steps.echo.said }}"
    document["report"]["total_ms"] = {
        "from": "${{ steps.run.time_ms }}",
        "fold": "sum",
        "better": "lower",
        "at_least": 0,
    }

    assert checked(document) == []
