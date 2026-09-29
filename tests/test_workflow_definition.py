"""The workflow format: the built-in classic workflow parses, references read
as owner/name@version, and a malformed workflow is refused at its path.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.domain.errors import InvalidName
from forge.domain.workflow_definition import (
    InputType,
    WorkflowRef,
    parse_workflow,
    parse_workflow_ref,
)
from forge.domain.yaml_models import InvalidDefinition
from forge.testing import CLASSIC

SEEDED = Path(__file__).resolve().parents[2] / "deploy" / "workflows" / "classic" / "workflow.yaml"


def test_the_classic_workflow_parses() -> None:
    workflow = parse_workflow(CLASSIC)
    assert workflow.name == "unicon/classic"
    assert workflow.owner == "unicon"
    assert workflow.ref == WorkflowRef("unicon", "classic", "v1")
    assert workflow.input_types() == {
        "submission": InputType.CODE,
        "testcases": InputType.FILES,
        "time_limit": InputType.NUMBER,
        "memory_limit": InputType.NUMBER,
    }
    compile_, run, check = workflow.steps
    assert (compile_.id, str(compile_.use), compile_.foreach) == (
        "compile",
        "unicon/compile@v1",
        None,
    )
    assert run.foreach == "${{ inputs.testcases }}"
    assert run.with_["time_limit"] == "${{ inputs.time_limit }}"
    assert check.use == WorkflowRef("unicon", "diff-check", "v1")
    assert workflow.outputs["metrics"] == {"points": "${{ steps.check.points }}"}


@pytest.mark.skipif(
    not SEEDED.exists(), reason="the deploy repo is not checked out beside this one"
)
def test_the_seeded_classic_workflow_is_the_one_tested_here() -> None:
    seeded = parse_workflow(SEEDED.read_bytes())
    assert seeded == parse_workflow(CLASSIC)


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


def classic_document() -> dict[str, Any]:
    document = yaml.safe_load(CLASSIC)
    assert isinstance(document, dict)
    return document


MALFORMED: list[tuple[str, Any, str]] = [
    ("name", "classic", "name"),
    ("version", 1.0, "version"),
    ("version", "v 1", "version"),
    ("inputs", [{"id": "submission", "type": "binary"}], "inputs[0].type"),
    ("inputs", [{"id": "a", "type": "code"}, {"id": "a", "type": "text"}], "inputs[1].id"),
    ("steps", [], "steps"),
    ("copied_from", "elsewhere", "copied_from"),
    ("outputs", {"score": 1}, "outputs.score"),
    ("description", "extra", "description"),
]


@pytest.mark.parametrize(("key", "value", "path"), MALFORMED)
def test_a_malformed_workflow_is_refused_at_its_path(key: str, value: Any, path: str) -> None:
    document = classic_document()
    document[key] = value
    with pytest.raises(InvalidDefinition) as error:
        parse_workflow(yaml.safe_dump(document))
    assert path in {problem["path"] for problem in error.value.errors}


@pytest.mark.parametrize(
    ("change", "path"),
    [
        ({"id": "compile"}, "steps[1].id"),
        ({"id": "Run"}, "steps[1].id"),
        ({"use": "sandbox-run"}, "steps[1].use"),
        ({"foreach": ""}, "steps[1].foreach"),
        ({"with": ["binary"]}, "steps[1].with"),
        ({"needs": "compile"}, "steps[1].needs"),
    ],
)
def test_a_malformed_step_is_refused_at_its_path(change: dict[str, Any], path: str) -> None:
    document = classic_document()
    document["steps"][1] = {**document["steps"][1], **change}
    with pytest.raises(InvalidDefinition) as error:
        parse_workflow(yaml.safe_dump(document))
    assert path in {problem["path"] for problem in error.value.errors}


def test_a_copy_names_its_source() -> None:
    document = classic_document()
    document["name"] = "acme/classic"
    document["copied_from"] = "unicon/classic@v1"
    assert parse_workflow(yaml.safe_dump(document)).copied_from == WorkflowRef(
        "unicon", "classic", "v1"
    )
