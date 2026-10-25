"""A primitive's declaration, `primitive.yaml`: the three the built-in workflow
uses parse, keep the runner's contract and are the releases deploy pins with
the image line written in; a file or folder input says whether the primitive
runs it (P1); `secret` is an input's word; every primitive declares its
outcome; and the keys and words the format no longer has are refused at
their paths. Contract 6's declarations, `exec`'s, `interact`'s and
`compile@v3`'s from their checkouts, parse with what they add kept, and what
breaks its rules is refused.
"""

import json
from collections.abc import Mapping
from typing import Any

import pytest

from forge.domain.contracts import schema_text, violation
from forge.domain.definitions import parse_task
from forge.domain.grading import PLATFORM_MACHINE
from forge.domain.plans import compile_plan
from forge.domain.primitives import STEP_OUTCOMES, Port, PrimitiveDeclaration, parse_primitive
from forge.domain.scoring import Better
from forge.domain.types import Type
from forge.domain.yaml_models import InvalidDefinition, Problem, load_yaml
from forge.testing import PLACEHOLDER_DIGEST, PRIMITIVES
from tests.conftest import released, sibling

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


def pins() -> dict[str, dict[str, Any]]:
    """The primitives deploy's `images.json` pins, by name and version, each
    with its image and the release it is mirrored from.
    """
    found: dict[str, dict[str, Any]] = json.loads(
        sibling("deploy", "images.json").read_text(encoding="utf-8")
    )["primitives"]
    return found


@pytest.mark.parametrize("name", list(PRIMITIVES))
def test_each_seeded_primitive_is_the_release_deploy_pins_with_the_image_written_in(
    name: str,
) -> None:
    """The fake seeds each at `v2`, as bootstrap mirrors the release deploy
    pins for `v2`, read at its tag, since the checkout beside this one may
    be on a later version of the primitive, as compile's is on `v3`.
    """
    tag = pins()[name]["v2"]["source"]["tag"]
    published = released(f"primitive-{name}", tag, "primitive.yaml")
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
                name: port.model_copy(update={"description": None, "outcomes": None})
                for name, port in given.items()
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
    assert all(found.outputs["outcome"].outcomes for found in worded.values())
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


def test_each_seeded_primitive_says_what_each_outcome_it_reports_means() -> None:
    said = {
        name: parse_primitive(text).outputs["outcome"].outcomes for name, text in PRIMITIVES.items()
    }

    assert {name: list(outcomes or {}) for name, outcomes in said.items()} == {
        "compile": ["accepted", "compile_error"],
        "sandbox-run": ["accepted", "time_limit", "memory_limit", "output_limit", "runtime_error"],
        "diff-check": ["accepted", "wrong_answer"],
    }
    assert (said["compile"] or {})["accepted"] == "the source compiles"


def test_the_outcomes_a_step_may_report_are_the_contracts_own_in_their_order() -> None:
    def outcomes(node: Any) -> list[Any]:
        if isinstance(node, list):
            return [found for each in node for found in outcomes(each)]
        if not isinstance(node, dict):
            return []
        held = node.get("properties", {}).get("outcomes", {}).get("propertyNames", {})
        return ([held["enum"]] if "enum" in held else []) + outcomes(list(node.values()))

    result = json.loads(schema_text("result"))
    [named] = outcomes(json.loads(schema_text("primitive")))

    assert tuple(named) == STEP_OUTCOMES
    assert tuple(result["$defs"]["outcome"]["enum"]) == (*STEP_OUTCOMES, "skipped", "system_error")


def test_outcomes_name_what_a_step_can_report_each_with_a_line() -> None:
    declared = parse_primitive(
        BASE
        + "outputs:\n  outcome:\n    type: outcome\n    outcomes:\n"
        + "      wrong_answer: the output differs\n      accepted: it matches\n"
    )

    # In the order the file gives them.
    assert declared.outputs["outcome"].outcomes == {
        "wrong_answer": "the output differs",
        "accepted": "it matches",
    }
    assert parse_primitive(BASE + OUTCOME).outputs["outcome"].outcomes is None


@pytest.mark.parametrize(
    ("outcomes", "path", "message"),
    [
        (
            "{skipped: it did not run}",
            "outputs.outcome.outcomes.skipped",
            "skipped is not an outcome a step can report. Use accepted, wrong_answer, "
            "time_limit, memory_limit, output_limit, runtime_error or compile_error.",
        ),
        (
            "{system_error: it broke}",
            "outputs.outcome.outcomes.system_error",
            "system_error is not an outcome a step can report.",
        ),
        (
            "{}",
            "outputs.outcome.outcomes",
            "Give each outcome the step can report with a line saying when, such as "
            "accepted: the source compiles.",
        ),
        ('{accepted: "two\\nlines"}', "outputs.outcome.outcomes.accepted", "one line"),
    ],
)
def test_outcomes_that_are_not_a_steps_each_with_a_line_are_refused(
    outcomes: str, path: str, message: str
) -> None:
    problems = refused(BASE + f"outputs:\n  outcome: {{type: outcome, outcomes: {outcomes}}}\n")

    assert [problem["path"] for problem in problems] == [path]
    assert message in problems[0]["message"]


@pytest.mark.parametrize(
    ("ports", "path"),
    [
        (
            "outputs:\n  log: {type: text, outcomes: {accepted: ok}}\n  outcome: outcome\n",
            "outputs",
        ),
        ("inputs:\n  n: {type: number, outcomes: {accepted: ok}}\n" + OUTCOME, "inputs"),
    ],
)
def test_outcomes_on_any_port_but_the_outcome_are_refused(ports: str, path: str) -> None:
    problems = refused(BASE + ports)

    assert len(problems) == 1
    assert problems[0]["path"].startswith(path) and problems[0]["path"].endswith(".outcomes")
    assert problems[0]["message"] == (
        "Only the outcome port says what each outcome means. Remove this."
    )


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
        {
            "path": "inputs.k.runs",
            "message": "This applies only to a file, folder or named_files port.",
        }
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


# Contract 6


def mirrored(name: str) -> PrimitiveDeclaration:
    """`primitive-<name>`'s own `primitive.yaml` from the checkout beside this
    one, with the image line bootstrap writes. Until the primitives are
    tagged and deploy pins them (#513), the checkout is what the mirror will
    hold.
    """
    text = sibling(f"primitive-{name}", "primitive.yaml").read_text(encoding="utf-8")
    return parse_primitive(
        f"image: ghcr.io/uniconhq/primitive-{name}@{PLACEHOLDER_DIGEST}\n" + text
    )


def test_exec_reads_the_cache_names_files_and_takes_outputs_from_a_manifest() -> None:
    exec_ = mirrored("exec")

    assert (exec_.schema_version, exec_.cache, exec_.batch, exec_.outputs_from) == (
        6,
        True,
        True,
        "manifest",
    )
    files, loads = exec_.inputs["files"], exec_.inputs["loads"]
    assert (files.type, files.runs, files.names) == (Type.NAMED_FILES, False, None)
    assert (loads.type, loads.runs) == (Type.NAMED_FILES, True)
    assert exec_.inputs["manifest"].type is Type.FILE
    time_ms = exec_.outputs["time_ms"]
    assert (time_ms.better, time_ms.at_least, time_ms.at_most) == (Better.LOWER, 0, None)
    assert exec_.outputs["message"].better is None
    assert (exec_.limits_from["memory_mb"].input, exec_.limits_from["memory_mb"].add) == (
        "memory_limit",
        256,
    )


def test_interact_runs_programs_linked_and_sizes_its_container_over_them() -> None:
    interact = mirrored("interact")

    assert (interact.schema_version, interact.cache, interact.outputs_from) == (
        6,
        True,
        "programs",
    )
    programs = interact.inputs["programs"]
    assert programs.type is Type.PROGRAMS and programs.runs is None
    assert programs.inputs is not None and programs.outputs is not None
    assert list(programs.inputs) == [
        "program",
        "files",
        "args",
        "time_limit",
        "memory_limit",
        "manifest",
        "stops_others",
    ]
    assert programs.inputs["program"].runs is True
    assert programs.inputs["files"].type is Type.NAMED_FILES
    assert programs.outputs["ended"].options == (
        "finished",
        "time_limit",
        "memory_limit",
        "output_limit",
        "runtime_error",
    )
    assert programs.outputs["memory_kb"].better is Better.LOWER
    assert programs.outputs["ended_at"].at_least == 0
    assert interact.inputs["links"].type is Type.LINKS
    assert set(interact.outputs) == {"facts", "outcome"}
    time_ms, memory_mb = interact.limits_from["time_ms"], interact.limits_from["memory_mb"]
    assert (time_ms.input, time_ms.sum, time_ms.max, time_ms.scale, time_ms.add) == (
        None,
        None,
        "programs.time_limit",
        4000,
        6000,
    )
    assert (memory_mb.input, memory_mb.sum, memory_mb.max, memory_mb.add) == (
        None,
        "programs.memory_limit",
        None,
        512,
    )


def test_compile_v3_speaks_contract_6_and_reads_no_cache() -> None:
    compile_ = mirrored("compile")

    assert (compile_.schema_version, compile_.cache, compile_.outputs_from) == (6, False, None)
    assert set(compile_.outputs) == {"program", "log", "outcome"}
    assert compile_.inputs["language"].options == ("c", "cpp", "java", "python", "ipynb")


@pytest.mark.parametrize("name", ["exec", "interact", "compile"])
def test_each_contract_6_primitive_keeps_the_contract(name: str) -> None:
    text = sibling(f"primitive-{name}", "primitive.yaml").read_text(encoding="utf-8")
    image = f"image: ghcr.io/uniconhq/primitive-{name}@{PLACEHOLDER_DIGEST}\n"
    assert violation(load_yaml(image + text), "primitive") is None


def test_every_primitive_deploy_pins_reads_as_it_did() -> None:
    """Each version `images.json` pins, from its release: a `v1` is in the
    form the forge stopped reading, with `name` and `version` lines, and is
    refused as before; every later one is contract 5 and parses.
    """
    read = 0
    for name, versions in pins().items():
        for version, pin in versions.items():
            text = released(f"primitive-{name}", pin["source"]["tag"], "primitive.yaml")
            with_image = f"image: {pin['image']}\n" + text
            if version == "v1":
                assert {"name", "version"} <= {problem["path"] for problem in refused(with_image)}
                continue
            assert parse_primitive(with_image).schema_version == 5
            read += 1
    assert read == 3


@pytest.mark.parametrize(
    "example",
    [
        "primitive.yaml",
        "primitive-v6.yaml",
        "primitive-v6-programs.yaml",
        "primitive-v6-programs-limits.yaml",
    ],
)
def test_the_runners_example_declarations_parse(example: str) -> None:
    text = sibling("runner", "examples", example).read_text(encoding="utf-8")
    assert parse_primitive(text).schema_version == (5 if example == "primitive.yaml" else 6)


def test_a_declaration_speaks_version_5_when_it_names_none_or_5_or_6() -> None:
    assert parse_primitive(BASE + OUTCOME).schema_version == 5
    assert parse_primitive("schema_version: 5\n" + BASE + OUTCOME).schema_version == 5
    assert parse_primitive("schema_version: 6\n" + BASE + OUTCOME).schema_version == 6
    for other in ("4", "7", "'6'", "true", "6.0"):
        assert refused(f"schema_version: {other}\n" + BASE + OUTCOME) == [
            {"path": "schema_version", "message": "This must be 5 or 6."}
        ]


V6 = (
    "schema_version: 6\n"
    + BASE
    + """\
inputs:
  program: {type: file, runs: true}
  files: {type: named_files, runs: false, names: [input, answer]}
outputs:
  outcome: outcome
  score: {type: number, better: higher, at_least: 0, at_most: 1}
"""
)
PROGRAMS_V6 = (
    "schema_version: 6\n"
    + BASE
    + """\
outputs_from: programs
limits_from:
  memory_mb: {sum: programs.memory_limit, add: 512}
inputs:
  programs:
    type: programs
    inputs:
      program: {type: file, runs: true}
      memory_limit: number
      manifest: {type: file, runs: false, optional: true}
    outputs:
      ended: {type: enum, options: [finished, runtime_error]}
  links: {type: links, optional: true}
outputs:
  outcome: outcome
"""
)


def test_a_named_files_port_keeps_its_names_and_a_number_output_its_meaning() -> None:
    declared = parse_primitive(V6)

    assert declared.inputs["files"].names == ("input", "answer")
    score = declared.outputs["score"]
    assert (score.better, score.at_least, score.at_most) == (Better.HIGHER, 0, 1)
    assert parse_primitive(PROGRAMS_V6).inputs["links"].type is Type.LINKS


@pytest.mark.parametrize(
    ("text", "old", "new", "path", "message"),
    [
        (
            V6,
            "better: higher",
            "better: faster",
            "outputs.score.better",
            "This must be one of higher or lower.",
        ),
        (
            V6,
            "at_least: 0, at_most: 1",
            "at_least: 2, at_most: 1",
            "outputs.score.at_most",
            "at_least 2 is above at_most 1, so no value fits.",
        ),
        (
            V6,
            "names: [input, answer]",
            "names: [../x]",
            "inputs.files.names",
            "'../x' cannot be a file's name.",
        ),
        (
            V6,
            "names: [input, answer]",
            "names: [input, input]",
            "inputs.files.names",
            "'input' is already listed above.",
        ),
        (
            V6,
            "  program: {type: file, runs: true}",
            "  program: {type: file, runs: true, names: [x]}",
            "inputs.program.names",
            "Only a named_files port has names.",
        ),
        (
            V6,
            "  score: {type: number,",
            "  score: {type: text,",
            "outputs.score.better",
            "Only a number says which way is better or its bounds.",
        ),
        (
            V6,
            "  program: {type: file, runs: true}",
            "  program: {type: file, runs: true}\n  n: {type: number, at_least: 0}",
            "inputs.n.at_least",
            "This applies only to an output.",
        ),
        (
            V6,
            "  score: {type: number,",
            "  files: {type: named_files}\n  score: {type: number,",
            "outputs.files.type",
            "An output must be text, a number, true or false, an enum, a file or a folder.",
        ),
        (
            PROGRAMS_V6,
            "      program: {type: file, runs: true}\n",
            "",
            "inputs.programs.inputs.program",
            "A programs port's programs each take a program, a file input called program "
            "with runs: true.",
        ),
        (
            PROGRAMS_V6,
            "      program: {type: file, runs: true}",
            "      program: {type: file, runs: false}",
            "inputs.programs.inputs.program",
            "A programs port's programs each take a program",
        ),
        (
            PROGRAMS_V6,
            "    outputs:\n      ended: {type: enum, options: [finished, runtime_error]}\n",
            "",
            "inputs.programs.outputs",
            "A programs port declares its programs' outputs.",
        ),
        (
            PROGRAMS_V6,
            "  links: {type: links, optional: true}",
            "  links: {type: links, optional: true, inputs: {}}",
            "inputs.links.inputs",
            "Only a programs port has this.",
        ),
        (
            PROGRAMS_V6,
            "outputs_from: programs\n",
            "",
            "outputs_from",
            "A primitive with a programs port needs outputs_from: programs.",
        ),
        (
            PROGRAMS_V6,
            "      manifest: {type: file, runs: false, optional: true}\n",
            "",
            "inputs.programs.inputs.manifest",
            "outputs_from: programs needs a file input called manifest",
        ),
        (
            PROGRAMS_V6,
            "sum: programs.memory_limit",
            "sum: programs.manifest",
            "limits_from.memory_mb.sum",
            "programs has no number input called 'manifest'.",
        ),
        (
            PROGRAMS_V6,
            "sum: programs.memory_limit",
            "sum: links.memory_limit",
            "limits_from.memory_mb.sum",
            "links is not a programs port.",
        ),
        (
            PROGRAMS_V6,
            "sum: programs.memory_limit",
            "sum: memory_limit",
            "limits_from.memory_mb.sum",
            "Give a programs port and one of its number inputs",
        ),
        (
            PROGRAMS_V6,
            "{sum: programs.memory_limit, add: 512}",
            "{sum: programs.memory_limit, input: memory_limit}",
            "limits_from.memory_mb",
            "Give exactly one of input, sum and max.",
        ),
        (
            PROGRAMS_V6,
            "      memory_limit: number",
            "      memory_limit: number\n      wiring: links",
            "inputs.programs.inputs.wiring.type",
            "A program's input cannot be programs or links.",
        ),
        (
            PROGRAMS_V6,
            "      ended: {type: enum,",
            "      verdict: outcome\n      ended: {type: enum,",
            "inputs.programs.outputs.verdict",
            "Only the output called outcome can be of type outcome.",
        ),
        (
            V6,
            "  program: {type: file, runs: true}",
            "  program: {type: file, runs: true}\n  links: links",
            "inputs.links",
            "Links need a programs port whose programs they link.",
        ),
        (
            V6,
            "inputs:\n",
            "outputs_from: manifest\ninputs:\n  manifest: {type: folder, runs: false}\n",
            "inputs.manifest",
            "outputs_from: manifest needs a file input called manifest",
        ),
    ],
)
def test_a_contract_6_declaration_that_breaks_a_rule_is_refused_saying_which(
    text: str, old: str, new: str, path: str, message: str
) -> None:
    changed = text.replace(old, new)
    assert changed != text

    problems = refused(changed)

    assert path in [problem["path"] for problem in problems]
    assert message in next(problem["message"] for problem in problems if problem["path"] == path)


def test_a_programs_port_is_called_programs() -> None:
    problems = refused(PROGRAMS_V6.replace("  programs:\n    type", "  players:\n    type"))

    assert {"path": "inputs.players", "message": "A programs port must be called programs."} in (
        problems
    )


@pytest.mark.parametrize(
    ("extra", "path"),
    [
        ("cache: true\n" + OUTCOME, "cache"),
        ("cache: false\n" + OUTCOME, "cache"),
        (
            "outputs_from: manifest\ninputs:\n  manifest: {type: file, runs: false}\n" + OUTCOME,
            "outputs_from",
        ),
        ("inputs:\n  files: {type: named_files, runs: false}\n" + OUTCOME, "inputs.files.type"),
        ("outputs:\n  outcome: outcome\n  t: {type: number, better: lower}\n", "outputs.t.better"),
        ("outputs:\n  outcome: outcome\n  t: {type: number, at_most: 1}\n", "outputs.t.at_most"),
    ],
)
def test_what_contract_6_adds_is_refused_in_a_version_5_declaration(extra: str, path: str) -> None:
    assert refused(BASE + extra) == [{"path": path, "message": "This needs schema_version: 6."}]
    assert parse_primitive("schema_version: 6\n" + BASE + extra).schema_version == 6


def test_a_limit_over_programs_is_refused_in_a_version_5_declaration() -> None:
    problems = refused(PROGRAMS_V6.replace("schema_version: 6\n", ""))

    assert {"path": "limits_from.memory_mb.sum", "message": "This needs schema_version: 6."} in (
        problems
    )
    assert {"path": "inputs.programs.type", "message": "This needs schema_version: 6."} in problems
