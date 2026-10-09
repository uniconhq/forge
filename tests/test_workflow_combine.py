"""Combining workflows: each source inlined whole, an input or test field
declared alike shared, anything else that clashes renamed with every
reference following it, the once steps of every source before their
per-test steps, and a definition written back out that reads as it was.
"""

import pytest
import yaml

from forge.domain.plans import check_workflow
from forge.domain.primitives import parse_primitive
from forge.domain.workflow_combine import combine_workflows
from forge.domain.workflow_definition import parse_workflow
from forge.testing import CLASSIC, PRIMITIVES

DECLARATIONS = {
    f"unicon/{name}@v2": parse_primitive(declaration) for name, declaration in PRIMITIVES.items()
}

TUNABLE = b"""\
# A second way to grade: the contestant's number on the command line.
inputs:
  submission: {type: file, contestant: true}
  language: {type: enum, options: [c, cpp], contestant: true}
  episodes: {type: number, contestant: true}
  time_limit: number
  memory_limit: number
test:
  input: file
  answer: {type: file, public: true}
steps:
  - id: compile
    use: unicon/compile@v2
    with: {source: "${{ inputs.submission }}", language: "${{ inputs.language }}"}
  - id: run
    use: unicon/sandbox-run@v2
    per_test: true
    with:
      binary: ${{ steps.compile.binary }}
      input: ${{ test.input }}
      args: "--episodes ${{ inputs.episodes }}"
      time_limit: ${{ inputs.time_limit }}
      memory_limit: ${{ inputs.memory_limit }}
report:
  time_ms: {from: "${{ steps.run.time_ms }}", fold: sum, better: lower, at_least: 0}
"""


def test_one_workflow_is_written_back_as_it_reads() -> None:
    classic = parse_workflow(CLASSIC)

    assert parse_workflow(combine_workflows([classic])) == classic


@pytest.mark.parametrize(
    "value",
    [
        b"line one\\nline two",
        b"multi\\n  indented\\n",
        b"a\\x85b",
        b"x\\u2028y",
        b"yes",
        b"2026-01-01",
        b"1e-9",
        b"it's ${{ inputs.episodes }}",
    ],
)
def test_a_value_yaml_reads_otherwise_is_written_back_as_it_reads(value: bytes) -> None:
    tricky = TUNABLE.replace(b'"--episodes ${{ inputs.episodes }}"', b'"' + value + b'"')
    workflow = parse_workflow(tricky)

    combined = combine_workflows([workflow])

    assert parse_workflow(combined) == workflow
    # On one line, as a reader stricter than the forge's, the editor's, takes it.
    [line] = [line for line in combined.splitlines() if "args:" in line]
    assert yaml.safe_load(line.strip()) == {"args": workflow.steps[1].with_["args"]}


def test_two_workflows_share_what_they_declare_alike_and_rename_the_rest() -> None:
    combined = parse_workflow(combine_workflows([parse_workflow(CLASSIC), parse_workflow(TUNABLE)]))

    assert list(combined.inputs) == [
        "submission",
        "language",
        "time_limit",
        "memory_limit",
        "language-2",
        "episodes",
    ]
    assert combined.inputs["language-2"].options == ("c", "cpp")
    assert list(combined.test) == ["input", "answer", "answer-2"]
    assert combined.test["answer-2"].public is True
    assert [step.id for step in combined.steps] == [
        "compile",
        "compile-2",
        "run",
        "check",
        "run-2",
    ]
    second = combined.steps[1]
    assert second.with_ == {
        "source": "${{ inputs.submission }}",
        "language": "${{ inputs.language-2 }}",
    }
    run = combined.steps[4]
    assert run.with_["binary"] == "${{ steps.compile-2.binary }}"
    assert run.with_["args"] == "--episodes ${{ inputs.episodes }}"
    assert list(combined.report) == ["time_ms", "memory_kb", "log", "time_ms_2"]
    meaning = combined.report["time_ms_2"]
    assert not isinstance(meaning, str)
    assert meaning.from_ == "${{ steps.run-2.time_ms }}"


def test_the_combination_checks_as_a_version_on_its_own() -> None:
    combined = parse_workflow(combine_workflows([parse_workflow(CLASSIC), parse_workflow(TUNABLE)]))

    assert check_workflow(combined, DECLARATIONS) == []


def test_a_workflow_combined_with_itself_runs_twice() -> None:
    classic = parse_workflow(CLASSIC)

    combined = parse_workflow(combine_workflows([classic, classic]))

    assert [step.id for step in combined.steps] == [
        "compile",
        "compile-2",
        "run",
        "check",
        "run-2",
        "check-2",
    ]
    assert combined.inputs == classic.inputs
    assert combined.test == classic.test
    assert combined.steps[5].with_["actual"] == "${{ steps.run-2.output }}"
    assert check_workflow(combined, DECLARATIONS) == []


def test_a_renamed_input_never_lands_on_another_of_the_same_source() -> None:
    first = parse_workflow(
        b"""inputs: {t: number}
test: {input: file}
steps:
  - {id: run, use: unicon/sandbox-run@v2, per_test: true, with: {time_limit: "${{ inputs.t }}"}}
"""
    )
    second = parse_workflow(
        b"""inputs: {t: text, t-2: text}
test: {input: file}
steps:
  - id: run
    use: unicon/sandbox-run@v2
    per_test: true
    with: {args: "${{ inputs.t }} ${{ inputs.t-2 }}"}
report: {run: "${{ steps.run.time_ms }}", run_2: "${{ steps.run.memory_kb }}"}
"""
    )

    combined = parse_workflow(
        combine_workflows([first, parse_workflow(combine_workflows([second]))])
    )

    assert list(combined.inputs) == ["t", "t-3", "t-2"]
    assert combined.steps[1].with_ == {"args": "${{ inputs.t-3 }} ${{ inputs.t-2 }}"}
    assert list(combined.report) == ["run", "run_2"]
