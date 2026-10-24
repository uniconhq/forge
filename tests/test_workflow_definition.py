"""The workflow file: the package's copy of `unicon/classic@v3` is deploy's,
a new workflow starts as it, every workflow deploy seeds is a run workflow,
a reference reads as owner/name@version and a value's references are found
in it, the keys and types the format no longer has are refused at their
paths, and only a number takes a unit.
"""

from pathlib import Path
from typing import Any

import pytest

from forge.domain.errors import InvalidName
from forge.domain.port_values import stray_units
from forge.domain.workflow_definition import (
    README_FILE,
    WORKFLOW_FILE,
    BadReference,
    Reference,
    WorkflowRef,
    classic_files,
    parse_run_workflow,
    parse_workflow_ref,
    references,
    starter_workflow,
    whole_reference,
)
from forge.domain.yaml_models import InvalidDefinition, Problem, load_yaml, one_line
from forge.testing import CLASSIC, RUN_WORKFLOWS
from tests.conftest import sibling


def classic() -> dict[str, Any]:
    document = load_yaml(CLASSIC[WORKFLOW_FILE])
    assert isinstance(document, dict)
    return document


def refused(document: dict[str, Any]) -> list[Problem]:
    with pytest.raises(InvalidDefinition) as raised:
        parse_run_workflow(one_line(document))
    return raised.value.errors


def paths(problems: list[Problem]) -> list[str]:
    return [problem["path"] for problem in problems]


def held(folder: Path) -> dict[str, bytes]:
    """Every file under `folder`, by its path there, Python's caches left
    out, as bootstrap seeds a version's folder.
    """
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.name != ".DS_Store"
    }


# The built-in workflow


def test_the_packages_classic_is_the_one_deploy_seeds() -> None:
    seeded = sibling("deploy", RUN_WORKFLOWS["unicon/classic@v3"])

    assert classic_files() == held(seeded) == CLASSIC


def test_a_new_workflow_starts_as_classic_without_its_readme() -> None:
    files = starter_workflow("acme", "sorting")

    assert set(files) == set(CLASSIC) - {README_FILE}
    assert files[WORKFLOW_FILE].startswith(b"# acme/sorting, a workflow.")
    assert parse_run_workflow(files[WORKFLOW_FILE]) == parse_run_workflow(CLASSIC[WORKFLOW_FILE])
    assert all(files[path] == CLASSIC[path] for path in files if path != WORKFLOW_FILE)


def test_every_workflow_deploy_seeds_is_a_run_workflow_named_here() -> None:
    found = sorted(sibling("deploy", "workflows").glob("*/*/workflow.yaml"))

    assert {path.parent for path in found} == {
        sibling("deploy", folder) for folder in RUN_WORKFLOWS.values()
    }
    for path in found:
        workflow = parse_run_workflow(path.read_bytes())
        assert stray_units(workflow) == [], path
        assert all(output.title for output in workflow.outputs.values()), path


# The file


@pytest.mark.parametrize(
    ("key", "value", "sentence"),
    [
        ("name", "unicon/classic", "the name it was made under"),
        ("version", "v3", "set when the version is made"),
        ("copied_from", "unicon/classic@v1", "copied from"),
    ],
)
def test_a_retired_workflow_key_is_refused_at_its_path_with_what_replaced_it(
    key: str, value: Any, sentence: str
) -> None:
    document = classic()
    document[key] = value

    problems = refused(document)

    assert paths(problems) == [key]
    assert sentence in problems[0]["message"]


@pytest.mark.parametrize(
    ("declaration", "sentence"),
    [
        ("code", "give a program as a file input"),
        ({"type": "file[]"}, "This type is now called folder."),
        ("dataset", "This type is no longer used: use file or folder instead."),
    ],
)
def test_a_retired_type_is_refused_at_the_declaration(declaration: Any, sentence: str) -> None:
    document = classic()
    document["inputs"]["old"] = declaration

    problems = refused(document)

    assert paths(problems) == ["inputs.old"]
    assert sentence in problems[0]["message"]


def test_a_missing_key_is_refused_at_its_path() -> None:
    document = classic()
    del document["steps"]

    assert refused(document) == [{"path": "steps", "message": "This is required."}]


def test_a_key_given_twice_is_refused() -> None:
    with pytest.raises(InvalidDefinition) as raised:
        parse_run_workflow(CLASSIC[WORKFLOW_FILE] + b"steps: []\n")

    assert "the setting 'steps' is given twice" in raised.value.errors[0]["message"]


def test_a_workflow_uses_at_most_32_primitive_versions() -> None:
    steps = "".join(f"  - {{id: s{n}, use: unicon/p{n}@v1}}\n" for n in range(33))

    with pytest.raises(InvalidDefinition) as raised:
        parse_run_workflow(f"steps:\n{steps}")

    assert paths(raised.value.errors) == ["steps"]


@pytest.mark.parametrize("kind", ["named_files", "programs", "links"])
def test_a_workflow_input_takes_none_of_a_ports_own_types(kind: str) -> None:
    """`named_files`, `programs` and `links` are a primitive's input ports'
    alone, though they share `Type`.
    """
    document = classic()
    document["inputs"]["extra"] = {"type": kind}

    problems = refused(document)

    assert paths(problems) == ["inputs.extra.type"]


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

    assert stray_units(parse_run_workflow(one_line(document))) == [
        {
            "path": "inputs.checker.unit",
            "message": f"Only a number takes a unit, and checker is {words}. "
            "Remove the unit, or make checker a number.",
        }
    ]


def test_an_input_names_itself_to_the_organisers_form() -> None:
    workflow = parse_run_workflow(CLASSIC[WORKFLOW_FILE])
    time_limit = workflow.inputs["time_limit"]

    assert (time_limit.title, time_limit.unit) == ("Time limit", "seconds")
    assert workflow.inputs["code"].unit is None


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
    assert whole_reference(" ${{inputs.input}} ") == Reference("inputs", "input")
    assert whole_reference("${{ steps.run.output }}") == Reference("steps", "run", "output")
    assert whole_reference("--n ${{ inputs.n }}") is None
    assert [found for _, found in references("--n ${{ inputs.n }} -e ${{ workflow.e }}")] == [
        Reference("inputs", "n"),
        Reference("workflow", "e"),
    ]
    assert str(Reference("steps", "run", "output")) == "${{ steps.run.output }}"


@pytest.mark.parametrize(
    "text",
    [
        "${{ secrets.key }}",
        "${{ inputs }}",
        "${{ steps.run }}",
        "${{ test.input }}",
        "a ${{ inputs.x",
    ],
)
def test_a_reference_a_workflow_does_not_make_is_refused(text: str) -> None:
    with pytest.raises(BadReference):
        references(text)
