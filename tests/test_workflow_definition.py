"""The workflow format: the built-in classic workflow parses, a reference reads
as owner/name@version and a value's references are found in it, a malformed
workflow and every key the format no longer has are refused at their paths,
the report's own rules hold (W1 to W5), and a workflow is checked against
the primitives its steps use as a version is (`plans.check_workflow`): every
port given a value of its type, under the two widenings, from a reference
that may feed it.
"""

from pathlib import Path
from typing import Any

import pytest

from forge.domain.errors import InvalidName
from forge.domain.meanings import ValueMeaning
from forge.domain.plans import (
    check_workflow,
    reported,
    reserved_inputs,
    stray_report_units,
    stray_units,
    value_meanings,
)
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
from forge.domain.yaml_models import InvalidDefinition, Problem, load_yaml, one_line
from forge.testing import CLASSIC, PLACEHOLDER_DIGEST, PRIMITIVES, RUN_WORKFLOWS
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
    document = load_yaml(CLASSIC)
    assert isinstance(document, dict)
    return document


def refused(document: dict[str, Any]) -> list[Problem]:
    with pytest.raises(InvalidDefinition) as raised:
        parse_workflow(one_line(document))
    return raised.value.errors


def paths(problems: list[Problem]) -> list[str]:
    return [problem["path"] for problem in problems]


def checked(
    document: dict[str, Any], primitives: dict[str, PrimitiveDeclaration] | None = None
) -> list[Problem]:
    workflow = parse_workflow(one_line(document))
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


def test_a_long_reported_value_name_of_todays_format_still_parses() -> None:
    """As a version made before the run format capped a column's name at
    64 letters, which today's format never did.
    """
    name = "log_" + "x" * 66
    workflow = parse_workflow(CLASSIC.replace(b"\n  log:\n", f"\n  {name}:\n".encode()))

    assert name in workflow.report


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
    log = workflow.report["log"]
    assert isinstance(log, Meaning)
    assert (log.from_, log.title, log.unit, log.fold) == (
        "${{ steps.compile.compile_log }}",
        "Compile log",
        None,
        None,
    )
    time_ms = workflow.report["time_ms"]
    assert isinstance(time_ms, Meaning)
    assert (time_ms.from_, time_ms.fold, time_ms.better, time_ms.at_least, time_ms.at_most) == (
        "${{ steps.run.time_ms }}",
        Fold.MAX,
        "lower",
        0,
        None,
    )
    assert (time_ms.title, time_ms.unit) == ("Run time", "ms")
    assert check_workflow(workflow, SEEDED) == []


def test_an_input_names_itself_to_the_organisers_form_and_no_plan_reads_it() -> None:
    workflow = parse_workflow(CLASSIC)
    time_limit = workflow.inputs["time_limit"]
    document = classic()
    for declared in document["inputs"].values():
        for key in ("title", "help", "unit"):
            declared.pop(key, None)
    plain = parse_workflow(one_line(document))

    assert (time_limit.title, time_limit.unit) == ("Time limit", "seconds")
    assert time_limit.help == "The CPU time the program may use on each test."
    assert workflow.inputs["submission"].unit is None
    assert check_workflow(plain, SEEDED) == check_workflow(workflow, SEEDED) == []
    assert plain.steps == workflow.steps and plain.report == workflow.report


@pytest.mark.parametrize(
    ("kind", "words"),
    [
        ("text", "text"),
        ("boolean", "true or false"),
        ("file", "a file"),
        ("folder", "a folder"),
        ("enum", "one of a list"),
    ],
)
def test_only_a_number_takes_a_unit_and_the_refusal_names_the_input(kind: str, words: str) -> None:
    document = classic()
    document["inputs"]["checker"] = {"type": kind, "title": "Checker", "unit": "seconds"}
    if kind == "enum":
        document["inputs"]["checker"]["options"] = ["exact", "tokens"]

    assert stray_units(parse_workflow(one_line(document))) == [
        {
            "path": "inputs.checker.unit",
            "message": f"Only a number takes a unit, and checker is {words}. "
            "Remove the unit, or make checker a number.",
        }
    ]


def test_an_enum_with_a_unit_is_refused_and_a_number_with_one_is_taken() -> None:
    document = classic()
    document["inputs"]["level"] = {"type": "enum", "options": ["low", "high"], "unit": "dB"}
    document["inputs"]["threshold"] = {"type": "number", "unit": "dB"}

    assert paths(stray_units(parse_workflow(one_line(document)))) == ["inputs.level.unit"]
    del document["inputs"]["level"]
    assert parse_workflow(one_line(document)).inputs["threshold"].unit == "dB"


@pytest.mark.parametrize(
    ("key", "holds"),
    [
        ("tests", "its tests"),
        ("public", "the files every contestant is served"),
        ("plans", "the plan Unicon compiles"),
    ],
)
def test_an_input_named_as_a_folder_the_format_gives_meaning_to_is_refused(
    key: str, holds: str
) -> None:
    document = classic()
    document["inputs"][key] = {"type": "folder"}

    assert reserved_inputs(parse_workflow(one_line(document))) == [
        {
            "path": f"inputs.{key}",
            "message": f"An input cannot be called {key}: a task's {key}/ folder already "
            f"holds {holds}. Choose another name.",
        }
    ]
    assert reserved_inputs(parse_workflow(CLASSIC)) == []


def test_a_version_made_with_a_stray_unit_still_reads_with_the_unit_dropped() -> None:
    document = classic()
    document["inputs"]["checker"] = {"type": "file", "title": "Checker", "unit": "seconds"}

    workflow = parse_workflow(one_line(document))

    assert workflow.inputs["checker"].shown_unit is None
    assert workflow.inputs["time_limit"].shown_unit == "seconds"


def test_a_reported_value_names_itself_to_pages_and_no_plan_reads_it() -> None:
    workflow = parse_workflow(CLASSIC)
    document = classic()
    for entry in document["report"].values():
        entry.pop("title", None)
        entry.pop("unit", None)
    plain = parse_workflow(one_line(document))

    assert value_meanings(workflow, reported(workflow, SEEDED)) == (
        ValueMeaning("time_ms", "Run time", "ms"),
        ValueMeaning("memory_kb", "Memory used", "KB"),
        ValueMeaning("log", "Compile log"),
    )
    assert value_meanings(plain, reported(plain, SEEDED)) == (
        ValueMeaning("time_ms"),
        ValueMeaning("memory_kb"),
        ValueMeaning("log"),
    )
    assert check_workflow(plain, SEEDED) == check_workflow(workflow, SEEDED) == []
    assert plain.steps == workflow.steps
    assert plain.report == {
        name: entry.model_copy(update={"title": None, "unit": None})
        for name, entry in workflow.report.items()
        if isinstance(entry, Meaning)
    }


def test_a_text_value_takes_a_title_with_from_alone_and_the_short_form_has_none() -> None:
    document = classic()
    document["report"]["log"] = {"from": "${{ steps.compile.compile_log }}", "title": "Log"}
    document["report"]["raw"] = "${{ steps.compile.compile_log }}"
    workflow = parse_workflow(one_line(document))

    assert checked(document) == []
    assert stray_report_units(workflow, SEEDED) == []
    assert value_meanings(workflow, reported(workflow, SEEDED))[-2:] == (
        ValueMeaning("log", "Log"),
        ValueMeaning("raw"),
    )


@pytest.mark.parametrize(
    ("entry", "path"),
    [
        ({"from": "${{ steps.compile.compile_log }}", "unit": "lines"}, "report.log.unit"),
        (
            {"from": "${{ steps.compile.compile_log }}", "title": "Log", "unit": "B"},
            "report.log.unit",
        ),
    ],
)
def test_only_a_reported_number_takes_a_unit_and_the_refusal_names_the_value(
    entry: dict[str, str], path: str
) -> None:
    document = classic()
    document["report"]["log"] = entry
    workflow = parse_workflow(one_line(document))

    assert stray_report_units(workflow, SEEDED) == [
        {"path": path, "message": "Only a number takes a unit, and log is text. Remove the unit."}
    ]
    assert stray_report_units(parse_workflow(CLASSIC), SEEDED) == []


def test_a_version_made_with_a_unit_on_a_reported_text_reads_with_it_dropped() -> None:
    document = classic()
    document["report"]["log"] = {
        "from": "${{ steps.compile.compile_log }}",
        "title": "Compile log",
        "unit": "lines",
    }
    workflow = parse_workflow(one_line(document))

    assert check_workflow(workflow, SEEDED) == []
    assert value_meanings(workflow, reported(workflow, SEEDED))[-1] == ValueMeaning(
        "log", "Compile log"
    )


@pytest.mark.parametrize("key", ["title", "unit"])
def test_a_reported_values_title_and_unit_are_each_one_line(key: str) -> None:
    document = classic()
    document["report"]["time_ms"][key] = "Run\ntime"

    assert paths(refused(document)) == [f"report.time_ms.{key}"]


def test_the_seeded_classic_workflow_is_the_one_tested_here() -> None:
    seeded = sibling("deploy", "workflows", "classic", "v2", "workflow.yaml")
    assert parse_workflow(seeded.read_bytes()) == parse_workflow(CLASSIC)


def todays_seeded() -> list[Path]:
    """Every workflow deploy seeds in today's format: the run workflows beside
    them are another format, read by `forge.domain.run_workflow`."""
    run = {sibling("deploy", folder) for folder in RUN_WORKFLOWS.values()}
    found = sibling("deploy", "workflows").glob("*/*/workflow.yaml")
    return sorted(path for path in found if path.parent not in run)


def test_every_seeded_workflow_puts_a_unit_only_on_a_number() -> None:
    seeded = todays_seeded()

    assert len(seeded) >= 2
    for path in seeded:
        workflow = parse_workflow(path.read_bytes())
        with_unit = {key for key, declared in workflow.inputs.items() if declared.unit}
        assert {workflow.inputs[key].type for key in with_unit} <= {Type.NUMBER}, path
        assert stray_report_units(workflow, SEEDED) == [], path


def test_every_seeded_workflow_names_each_value_it_reports() -> None:
    seeded = todays_seeded()

    for path in seeded:
        workflow = parse_workflow(path.read_bytes())
        meanings = value_meanings(workflow, reported(workflow, SEEDED))
        assert meanings and all(meaning.title for meaning in meanings), path


def test_a_new_workflow_starts_as_classic() -> None:
    files = starter_workflow("acme", "sorting")

    assert parse_workflow(files["workflow.yaml"]) == parse_workflow(CLASSIC)


def test_a_declaration_may_be_just_its_types_name() -> None:
    document = classic()
    document["test"]["input"] = {"type": "file"}

    assert parse_workflow(one_line(document)) == parse_workflow(CLASSIC)


@pytest.mark.parametrize(
    ("key", "value", "path", "sentence"),
    [
        ("name", "unicon/classic", "name", "the name it was made under"),
        ("version", "v2", "version", "set when the version is made"),
        ("copied_from", "unicon/classic@v1", "copied_from", "copied from"),
        ("outputs", {"metrics": {}}, "outputs", "This is now called report"),
        (
            "inputs",
            [{"id": "submission", "type": "file"}],
            "inputs",
            "listed by name, each with its type",
        ),
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
        ("inputs", "code", "give a program as a file input"),
        ("inputs", {"type": "file[]", "contestant": True}, "This type is now called folder."),
        ("inputs", "dataset", "This type is no longer used: use file or folder instead."),
        ("test", "jupyter", "This type is no longer used: use file instead."),
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
            "message": "This is now set by running the step for each test. Turn that on, and "
            "remove this.",
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
        ({"inputs": {"x": {"type": "number", "title": ""}}}, "inputs.x.title"),
        ({"inputs": {"x": {"type": "number", "help": "Two\nlines."}}}, "inputs.x.help"),
        ({"inputs": {"x": {"type": "number", "unit": 3}}}, "inputs.x.unit"),
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

    assert refused(document) == [{"path": "steps", "message": "This is required."}]


def test_a_key_given_twice_is_refused() -> None:
    with pytest.raises(InvalidDefinition) as raised:
        parse_workflow(CLASSIC + b"test:\n  input: file\n")

    assert "the setting 'test' is given twice" in raised.value.errors[0]["message"]


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
        {"path": "test.test", "message": "A test field cannot be called test. Choose another name."}
    ]


def test_a_once_step_after_a_per_test_step_is_refused_at_the_version() -> None:
    document = classic()
    document["steps"].append(
        {"id": "late", "use": "unicon/compile@v2", "with": {"source": "${{ inputs.submission }}"}}
    )

    problems = refused(document)

    assert paths(problems) == ["steps[3].per_test"]
    assert "must come before every step that runs for each test" in problems[0]["message"]


# W1 to W5


@pytest.mark.parametrize(
    ("entry", "path", "sentence"),
    [
        (
            {"from": "${{ steps.run.time_ms }}", "colour": "red"},
            "report.x.colour",
            "does not know this setting",
        ),
        ({"fold": "max"}, "report.x.from", "This is required."),
        ({"from": "${{ steps.run.time_ms }}", "fold": "min"}, "report.x.fold", "sum, mean or max"),
        (
            {"from": "${{ steps.run.time_ms }}", "better": "up"},
            "report.x.better",
            "Choose higher or lower",
        ),
        (
            {"from": "${{ steps.run.time_ms }}", "better": "${{ inputs.language }}"},
            "report.x.better",
            "language must be an enum input that the task gives, not the entrant.",
        ),
        (
            {"from": "${{ steps.run.time_ms }}", "better": "${{ inputs.time_limit }}"},
            "report.x.better",
            "time_limit must be an enum input that the task gives",
        ),
        (
            {"from": "${{ steps.run.time_ms }}", "better": "${{ inputs.ghost }}"},
            "report.x.better",
            "The workflow has no input called ghost.",
        ),
        (
            {"from": "${{ steps.run.time_ms }}", "at_least": 2, "at_most": 1},
            "report.x.at_most",
            "The upper bound must be at least the lower bound.",
        ),
        (
            {"from": "${{ steps.run.time_ms }}", "at_least": "none"},
            "report.x.at_least",
            "This must be a number.",
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
    assert problems[0]["message"].startswith("Unicon cannot read ${{ foo }}.")
    assert "points is a name Unicon uses itself" in problems[1]["message"]


def test_better_may_be_a_task_enum_of_higher_and_lower() -> None:
    document = classic()
    document["inputs"]["better"] = {"type": "enum", "options": ["higher", "lower"]}
    document["report"]["x"] = {"from": "${{ steps.run.time_ms }}", "better": "${{ inputs.better }}"}
    parse_workflow(one_line(document))

    document["inputs"]["better"] = {"type": "enum", "options": ["higher", "sideways"]}
    assert refused(document) == [
        {
            "path": "report.x.better",
            "message": "The options of better must be higher and lower, and nothing else.",
        }
    ]


@pytest.mark.parametrize("name", ["outcome", "points", "penalty"])
def test_the_platforms_own_names_are_refused_as_reported_names(name: str) -> None:
    document = classic()
    document["report"][name] = "${{ steps.run.time_ms }}"

    problems = refused(document)

    assert paths(problems) == [f"report.{name}"]
    assert "a name Unicon uses itself" in problems[0]["message"]


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
        "message": "unicon/classic@v2 is not a primitive Unicon knows. Check its owner, name "
        "and version.",
    }


def test_a_port_the_primitive_does_not_have_is_refused() -> None:
    document = classic()
    document["steps"][2]["with"]["colour"] = "red"

    assert checked(document) == [
        {
            "path": "steps[2].with.colour",
            "message": "unicon/diff-check@v2 has no input called colour. Remove it.",
        }
    ]


def test_a_required_port_left_out_is_refused_and_an_optional_one_is_not() -> None:
    document = classic()
    del document["steps"][2]["with"]["expected"]

    assert checked(document) == [
        {
            "path": "steps[2].with.expected",
            "message": "unicon/diff-check@v2 needs a value for its input expected.",
        }
    ]
    assert "args" not in classic()["steps"][1]["with"]


@pytest.mark.parametrize(
    ("port", "value", "message"),
    [
        ("times", "3", "This takes a number, not text."),
        ("times", "${{ inputs.language }}", "This takes a number, not an enum."),
        ("times", True, "This takes a number, not true or false."),
        ("message", "${{ inputs.submission }}", "This takes text, not a file."),
        ("mode", "${{ inputs.time_limit }}", "This takes one of fast, slow."),
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
        {"path": "steps[1].with.input", "message": "This takes a file, not a folder."}
    ]


def test_an_enum_input_is_checked_against_the_ports_options() -> None:
    document = classic()
    document["inputs"]["language"]["options"] = ["c", "rust"]

    assert checked(document) == [
        {
            "path": "steps[0].with.language",
            "message": "This takes one of c, cpp, java, python, not rust.",
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
        {"path": "steps[0].with.language", "message": "This takes one of c, cpp, java, python."}
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
            "message": "That output may be missing, so it can be given only to an optional port.",
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
            "message": "That input is optional, so it can be given only to an optional port.",
        }
    ]

    written = with_echo(**{**ECHO_PORTS, "note": "key=${{ inputs.key }}"})
    written["inputs"]["key"] = {"type": "text", "optional": True}
    assert checked(written) == [
        {
            "path": "steps[1].with.note",
            "message": "${{ inputs.key }} is optional, so it can be given only whole, to an "
            "optional port, not written into text.",
        }
    ]


def test_a_contestant_input_never_raises_a_limit() -> None:
    document = classic()
    document["inputs"]["seconds"] = {"type": "number", "contestant": True}
    document["steps"][1]["with"]["time_limit"] = "${{ inputs.seconds }}"

    assert checked(document) == [
        {
            "path": "steps[1].with.time_limit",
            "message": "This value raises a limit, so the task must give it, not the entrant.",
        }
    ]


def test_a_step_output_never_raises_a_limit() -> None:
    document = with_echo(**ECHO_PORTS)
    document["steps"][2]["with"]["time_limit"] = "${{ steps.echo.size }}"

    assert checked(document) == [
        {
            "path": "steps[2].with.time_limit",
            "message": "This value raises a limit, so it must be known when the task is saved.",
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
            "message": "Only a step that runs for each test can read a test's fields.",
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
            "an enum can be written into text.",
        }
    ]


def test_a_per_test_input_is_read_only_in_a_per_test_step() -> None:
    document = classic()
    document["inputs"]["answers"] = {"type": "file", "contestant": True, "per_test": True}
    document["steps"][0]["with"]["source"] = "${{ inputs.answers }}"

    assert checked(document) == [
        {
            "path": "steps[0].with.source",
            "message": "answers is given once for each test, so only a step that runs for "
            "each test can read it.",
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
            "message": "The step run runs for each test, so a step that runs once cannot read it.",
        }
    ]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("${{ steps.later.output }}", "There is no step called later before this one."),
        ("${{ steps.compile.nothing }}", "The step compile has no output called nothing."),
        ("${{ steps.compile.outcome }}", "A step's outcome is read by Unicon itself"),
        ("${{ inputs.ghost }}", "The workflow has no input called ghost."),
        ("${{ test.ghost }}", "ghost is not one of the workflow's test fields."),
        ("${{ secrets.key }}", "Unicon cannot read ${{ secrets.key }}"),
        ("log: ${{ steps.compile.compile_log }}", "A step's output can be given only whole"),
        (["a"], "not a list, a set of settings or nothing"),
        (None, "not a list, a set of settings or nothing"),
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
        (
            "${{ steps.run.output }}",
            "steps.run.output is a file, and only text or a number can be reported.",
        ),
        (
            "${{ steps.run.outcome }}",
            "steps.run.outcome is an outcome, and only text or a number can be reported.",
        ),
        ("${{ steps.ghost.size }}", "steps.ghost.size is not the output of any step."),
        (
            "${{ inputs.time_limit }}",
            "Give the step output this reports, as ${{ steps.<id>.<output> }}.",
        ),
        (
            {"from": "${{ steps.compile.compile_log }}", "fold": "max"},
            "fold, better, at_least and at_most apply only to a number, and this is text.",
        ),
        (
            {"from": "${{ steps.compile.compile_log }}", "at_most": 1},
            "fold, better, at_least and at_most apply only to a number, and this is text.",
        ),
        (
            {"from": "${{ steps.echo.size }}", "fold": "sum"},
            "fold and better apply only to a number reported for each test, and this step runs "
            "once.",
        ),
        (
            {"from": "${{ steps.echo.size }}", "better": "higher"},
            "fold and better apply only to a number reported for each test, and this step runs "
            "once.",
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


def test_an_optional_input_never_says_which_way_is_better() -> None:
    text = (
        CLASSIC.decode()
        .replace(
            "\ntest:\n",
            "\n  dir: {type: enum, options: [higher, lower], optional: true}\ntest:\n",
        )
        .replace(
            "    better: lower\n    at_least: 0\n  log:",
            '    better: "${{ inputs.dir }}"\n    at_least: 0\n  log:',
        )
    )

    with pytest.raises(InvalidDefinition) as refused:
        parse_workflow(text)

    assert refused.value.errors == [
        {
            "path": "report.memory_kb.better",
            "message": "dir is optional, so it cannot say which way is better. Choose an input "
            "that is not optional.",
        }
    ]


def test_a_definition_names_at_most_32_primitive_versions() -> None:
    steps = "".join(f"  - {{id: s{n}, use: unicon/p{n}@v1}}\n" for n in range(33))

    with pytest.raises(InvalidDefinition) as refused:
        parse_workflow(f"test: {{input: file}}\nsteps:\n{steps}")

    assert [error["path"] for error in refused.value.errors] == ["steps"]


@pytest.mark.parametrize("kind", ["named_files", "programs", "links"])
def test_a_workflow_input_or_test_field_takes_none_of_a_ports_own_types(kind: str) -> None:
    """`named_files`, `programs` and `links` are a contract 6 primitive's
    input ports' alone, though they share `Type`.
    """
    document = classic()
    document["inputs"]["extra"] = {"type": kind}
    document["test"]["extra"] = kind

    problems = refused(document)

    assert paths(problems) == ["inputs.extra.type", "test.extra.type"]
    assert {problem["message"] for problem in problems} == {
        "This must be one of text, number, boolean, enum, file, folder."
    }


def test_a_contract_6_primitive_is_refused_in_this_format() -> None:
    """The forge reads contract 6 primitives before any workflow can use
    them, so this format, which hands every step contract 5's files, refuses
    one by its `use`.
    """
    six = ECHO.model_copy(update={"schema_version": 6})

    assert checked(with_echo(**ECHO_PORTS), {**SEEDED, "acme/echo@v1": six}) == [
        {
            "path": "steps[1].use",
            "message": "acme/echo@v1 is written for the run workflow's format, which this "
            "workflow is not in. Use a version written for this format.",
        }
    ]
    assert checked(with_echo(**ECHO_PORTS)) == []
