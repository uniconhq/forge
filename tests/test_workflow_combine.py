"""Combining workflows: each source inlined whole, an input or test field
declared alike shared, anything else that clashes renamed with every
reference following it, the once steps of every source before their
per-test steps, and a definition written back out that reads as it was.
"""

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
