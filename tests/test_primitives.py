"""A primitive's declaration, `primitive.yaml`: the three the built-in workflow
uses parse, keep the runner's contract and are their repos' own with the
image line written in; a file or folder input says whether the primitive runs
it (P1); `secret` is an input's word; every primitive declares its outcome;
and the keys and words the format no longer has are refused at their paths.
"""

from collections.abc import Mapping

import pytest

from forge.domain.contracts import violation
from forge.domain.definitions import parse_task
from forge.domain.grading import PLATFORM_MACHINE
from forge.domain.plans import compile_plan
from forge.domain.primitives import Port, PrimitiveDeclaration, parse_primitive
from forge.domain.types import Type
from forge.domain.yaml_models import InvalidDefinition, Problem, load_yaml
from forge.testing import PLACEHOLDER_DIGEST, PRIMITIVES
from tests.conftest import sibling

DIGEST = "sha256:" + "a" * 64
BASE = f"""\
image: ghcr.io/uniconhq/primitive-probe@{DIGEST}
limits: {{time_ms: 1, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1, gpus: 0}}
"""
OUTCOME = "outputs:\n  outcome: outcome\n"


def refused(text: str) -> list[Problem]:
    with pytest.raises(InvalidDefinition) as raised:
        parse_primitive(text)
    return raised.value.errors


def test_the_three_seeded_primitives_parse() -> None:
    compile_ = parse_primitive(PRIMITIVES["compile"])
    run = parse_primitive(PRIMITIVES["sandbox-run"])
    check = parse_primitive(PRIMITIVES["diff-check"])

    assert (compile_.batch, run.batch, check.batch) == (False, True, True)
    assert not (compile_.network or run.network or check.network)
    assert compile_.image == f"ghcr.io/uniconhq/primitive-compile@{PLACEHOLDER_DIGEST}"
    assert compile_.inputs["source"].type is Type.FOLDER
    assert compile_.inputs["source"].runs is True
    assert compile_.inputs["language"].options == ("c", "cpp", "java", "python")
    assert compile_.inputs["entry"].optional is True
    assert compile_.limits.as_mapping() == {
        "time_ms": 60000,
        "cpu_ms": 60000,
        "memory_mb": 1024,
        "pids": 128,
        "output_mb": 64,
        "gpus": 0,
    }
    assert (run.inputs["binary"].runs, run.inputs["input"].runs) == (True, False)
    assert run.inputs["args"].type is Type.TEXT and run.inputs["args"].runs is None
    assert run.limits_from["time_ms"].input == "time_limit"
    assert (run.limits_from["time_ms"].scale, run.limits_from["time_ms"].add) == (2000, 3000)
    assert (run.limits_from["memory_mb"].scale, run.limits_from["memory_mb"].add) == (1, 256)
    assert set(check.outputs) == {"outcome"}
    assert check.outputs["outcome"].type is Type.OUTCOME
    assert not any(port.secret for port in compile_.inputs.values())


@pytest.mark.parametrize("name", list(PRIMITIVES))
def test_each_seeded_primitive_keeps_the_contract(name: str) -> None:
    assert violation(load_yaml(PRIMITIVES[name]), "primitive") is None


@pytest.mark.parametrize("name", list(PRIMITIVES))
def test_each_seeded_primitive_is_its_repos_declaration_with_the_image_written_in(
    name: str,
) -> None:
    published = sibling(f"primitive-{name}", "primitive.yaml").read_text(encoding="utf-8")
    lines = PRIMITIVES[name].decode().splitlines(keepends=True)
    assert "".join(line for line in lines if not line.startswith("image: ")) == published


def test_a_primitive_and_its_ports_say_what_they_are_for_and_no_plan_reads_it() -> None:
    run = parse_primitive(PRIMITIVES["sandbox-run"])
    plain = parse_primitive(BASE + OUTCOME)

    assert run.title == "Run program"
    assert run.description is not None and run.description.startswith("Runs a program")
    assert run.inputs["binary"].description == "The program to run."
    assert run.outputs["outcome"].description is not None
    assert (plain.title, plain.description, plain.outputs["outcome"].description) == (
        None,
        None,
        None,
    )
    # A plan compiled from the declarations without their words is the
    # same to the byte.
    from tests.test_plans import CLASSIC_TASK, CLASSIC_WORKFLOW, HARNESS, cases_of, classic_tests

    def wordless(found: PrimitiveDeclaration) -> PrimitiveDeclaration:
        def ports(given: Mapping[str, Port]) -> dict[str, Port]:
            return {
                name: port.model_copy(update={"description": None}) for name, port in given.items()
            }

        return found.model_copy(
            update={
                "title": None,
                "description": None,
                "inputs": ports(found.inputs),
                "outputs": ports(found.outputs),
            }
        )

    worded = {f"unicon/{name}@v2": parse_primitive(text) for name, text in PRIMITIVES.items()}
    assert any(found.title is not None for found in worded.values())
    paths = classic_tests("main/1")
    tests = cases_of(CLASSIC_WORKFLOW, paths, {})

    def plan(primitives: Mapping[str, PrimitiveDeclaration]) -> bytes:
        made = compile_plan(
            parse_task(CLASSIC_TASK),
            CLASSIC_WORKFLOW,
            primitives,
            paths,
            tests,
            secrets=frozenset(),
            machine=PLATFORM_MACHINE,
            harness_image=HARNESS,
        )
        return made.plan.to_bytes()

    assert plan(worded) == plan({ref: wordless(found) for ref, found in worded.items()})


@pytest.mark.parametrize(
    ("extra", "path"),
    [
        ('title: ""\n', "title"),
        ('description: "Two\\nlines."\n', "description"),
        ("inputs:\n  x: {type: number, description: 3}\n", "inputs.x.description"),
    ],
)
def test_a_title_or_description_is_one_line_of_text(extra: str, path: str) -> None:
    assert path in [problem["path"] for problem in refused(BASE + OUTCOME + extra)]


def test_the_smallest_declaration_is_an_image_limits_and_an_outcome() -> None:
    declared = parse_primitive(BASE + OUTCOME)

    assert (declared.batch, declared.network, declared.limits_from, declared.inputs) == (
        False,
        False,
        {},
        {},
    )


@pytest.mark.parametrize("kind", ["file", "folder"])
def test_a_file_or_folder_port_without_runs_is_refused_naming_it(kind: str) -> None:
    problems = refused(BASE + f"inputs:\n  data: {{type: {kind}}}\n" + OUTCOME)

    assert problems == [
        {
            "path": "inputs.data",
            "message": f"data is a {kind} port. Say whether the primitive runs it as a "
            "program, with runs: true or runs: false.",
        }
    ]


def test_a_port_given_by_its_types_name_alone_still_says_runs() -> None:
    assert [problem["path"] for problem in refused(BASE + "inputs:\n  data: file\n" + OUTCOME)] == [
        "inputs.data"
    ]


@pytest.mark.parametrize("kind", ["text", "number", "boolean", "enum, options: [a]"])
def test_runs_on_a_scalar_port_is_refused(kind: str) -> None:
    problems = refused(BASE + f"inputs:\n  k: {{type: {kind}, runs: false}}\n" + OUTCOME)

    assert problems == [
        {"path": "inputs.k.runs", "message": "This applies only to a file or folder port."}
    ]


def test_runs_on_an_output_is_refused() -> None:
    problems = refused(BASE + "outputs:\n  outcome: outcome\n  out: {type: file, runs: true}\n")

    assert problems == [{"path": "outputs.out.runs", "message": "This applies only to an input."}]


def test_secret_on_an_output_is_refused_and_on_an_input_is_kept() -> None:
    problems = refused(BASE + "outputs:\n  outcome: outcome\n  log: {type: text, secret: true}\n")
    assert problems == [{"path": "outputs.log.secret", "message": "This applies only to an input."}]

    declared = parse_primitive(
        BASE
        + "inputs:\n  api_key: {type: text, secret: true}\n"
        + "  interactor: {type: file, runs: true, secret: true}\n"
        + OUTCOME
    )
    assert declared.inputs["api_key"].secret and declared.inputs["interactor"].secret


@pytest.mark.parametrize(
    ("outputs", "path", "message"),
    [
        ("", "outputs", "Every primitive must have an output called outcome, of type outcome."),
        (
            "outputs:\n  result: text\n",
            "outputs",
            "Every primitive must have an output called outcome",
        ),
        (
            "outputs:\n  outcome: text\n",
            "outputs",
            "Every primitive must have an output called outcome",
        ),
        (
            "outputs:\n  outcome: {type: outcome, optional: true}\n",
            "outputs.outcome.optional",
            "The outcome cannot be optional: every step gives one.",
        ),
        (
            "outputs:\n  outcome: outcome\n  verdict: outcome\n",
            "outputs.verdict",
            "Only the output called outcome can be of type outcome.",
        ),
    ],
)
def test_every_primitive_declares_its_outcome(outputs: str, path: str, message: str) -> None:
    problems = refused(BASE + outputs)

    assert [problem["path"] for problem in problems] == [path]
    assert message in problems[0]["message"]


def test_an_input_port_is_one_of_the_six_value_types() -> None:
    assert refused(BASE + "inputs:\n  k: outcome\n" + OUTCOME) == [
        {
            "path": "inputs.k.type",
            "message": "An input must be text, a number, true or false, an enum, a file or a "
            "folder.",
        }
    ]


@pytest.mark.parametrize(
    ("text", "path", "sentence"),
    [
        ("name: unicon/probe\n" + BASE + OUTCOME, "name", "the name it was made under"),
        ("version: v1\n" + BASE + OUTCOME, "version", "set when the version is made"),
        ("schema_version: 4\n" + BASE + OUTCOME, "schema_version", "no longer used"),
        (
            BASE + "inputs:\n  kind: {type: enum, values: [a, b]}\n" + OUTCOME,
            "inputs.kind",
            "values is now called options.",
        ),
        (
            BASE + "inputs:\n  data: {type: 'file[]', runs: false}\n" + OUTCOME,
            "inputs.data",
            "The type file[] is now called folder.",
        ),
        (
            BASE + "outputs:\n  outcome: outcome\n  files: 'file[]'\n",
            "outputs.files",
            "The type file[] is now called folder.",
        ),
    ],
)
def test_a_retired_key_or_word_is_refused_at_its_path(text: str, path: str, sentence: str) -> None:
    problems = refused(text)

    assert [problem["path"] for problem in problems] == [path]
    assert sentence in problems[0]["message"]


@pytest.mark.parametrize(
    ("extra", "path"),
    [
        ("inputs:\n  kind: {type: enum}\n", "inputs.kind.options"),
        ("inputs:\n  kind: {type: text, options: [a]}\n", "inputs.kind.options"),
        ("inputs:\n  Kind: text\n", "inputs.Kind"),
        (
            "inputs:\n  kind: text\nlimits_from:\n  time_ms: {input: kind}\n",
            "limits_from.time_ms.input",
        ),
        ("limits_from:\n  time_ms: {input: ghost}\n", "limits_from.time_ms.input"),
        ("inputs:\n  n: number\nlimits_from:\n  disk_mb: {input: n}\n", "limits_from.disk_mb"),
        (
            "inputs:\n  n: number\nlimits_from:\n  time_ms: {input: n, scale: 0}\n",
            "limits_from.time_ms.scale",
        ),
        (
            "inputs:\n  n: number\nlimits_from:\n  time_ms: {input: n, add: -1}\n",
            "limits_from.time_ms.add",
        ),
        ("colour: red\n", "colour"),
        ("batch: 'yes'\n", "batch"),
    ],
)
def test_a_declaration_that_breaks_the_format_is_refused_at_its_path(extra: str, path: str) -> None:
    assert path in [problem["path"] for problem in refused(BASE + extra + OUTCOME)]


@pytest.mark.parametrize(
    ("limits", "path"),
    [
        ("{time_ms: 1, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1}", "limits.gpus"),
        ("{time_ms: 0, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1, gpus: 0}", "limits.time_ms"),
        ("{time_ms: 1, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1, gpus: -1}", "limits.gpus"),
    ],
)
def test_the_six_limits_are_all_given(limits: str, path: str) -> None:
    text = BASE.replace(
        "{time_ms: 1, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1, gpus: 0}", limits
    )
    assert [problem["path"] for problem in refused(text + OUTCOME)] == [path]


def test_an_image_is_named_by_its_digest() -> None:
    local = BASE.replace("ghcr.io/uniconhq", "localhost:5000")
    assert parse_primitive(local + OUTCOME).image.startswith("localhost:5000/")
    problems = refused(BASE.replace(f"@{DIGEST}", ":latest") + OUTCOME)
    assert [problem["path"] for problem in problems] == ["image"]
