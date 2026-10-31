"""A primitive's declaration, `primitive.yaml`: the one the built-in workflow
uses parses, keeps the runner's contract and is its repo's own with the
image line written in; a declaration says `schema_version: 6`; a file or
folder input says whether the primitive runs it (P1); `secret` is an
input's word; every primitive declares its outcome; and the keys and words
the format no longer has are refused at their paths. `exec`'s declaration
from its checkout parses with what it adds kept, what a batch step gives
once among it, and what breaks the format's rules is refused.
"""

import json
from typing import Any

import pytest

from forge.domain.blocks.numbers import Better
from forge.domain.contracts import schema_text, violation
from forge.domain.primitives import (
    STEP_OUTCOMES,
    FromInput,
    FromMax,
    FromSum,
    PrimitiveDeclaration,
    PrimitiveForm,
    parse_primitive,
)
from forge.domain.roles import PRIMITIVE_OWNER
from forge.domain.run_plan import retired
from forge.domain.types import Type
from forge.domain.yaml_models import InvalidDefinition, Problem, load_yaml
from forge.testing import PLACEHOLDER_DIGEST, PRIMITIVES
from tests.conftest import released, sibling

DIGEST = "sha256:" + "a" * 64
BASE = f"""\
schema_version: 6
image: ghcr.io/uniconhq/primitive-probe@{DIGEST}
limits: {{time_ms: 1, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1, gpus: 0}}
"""
OUTCOME = "outputs:\n  outcome: outcome\n"


def refused(text: str) -> list[Problem]:
    with pytest.raises(InvalidDefinition) as raised:
        parse_primitive(text)
    return raised.value.errors


def test_the_seeded_primitive_parses() -> None:
    exec_ = parse_primitive(PRIMITIVES["unicon/exec@v2"])

    assert list(PRIMITIVES) == ["unicon/exec@v2"]
    assert (exec_.batch, exec_.network, exec_.cache) == (True, False, True)
    assert exec_.image == f"ghcr.io/uniconhq/primitive-exec@{PLACEHOLDER_DIGEST}"
    assert set(exec_.forms) == {"one", "programs", "build"}
    one = exec_.forms["one"]
    assert (one.inputs["source"].type, one.inputs["source"].runs) == (Type.FOLDER, True)
    assert one.inputs["stdin"].runs is False
    assert one.inputs["language"].options == ("c", "cpp", "java", "python", "ipynb")
    assert one.inputs["entry"].optional is True
    assert one.limits is not None and one.limits.as_mapping() == {
        "time_ms": 2000,
        "cpu_ms": 2000,
        "memory_mb": 1136,
        "pids": 160,
        "output_mb": 64,
        "gpus": 0,
    }
    assert one.limits.once_time_ms == 40000
    assert one.limits_from["time_ms"] == FromInput(input="time_limit", scale=2000, add=3000)
    assert not any(port.secret for port in one.inputs.values())


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


@pytest.mark.parametrize("reference", list(PRIMITIVES))
def test_each_seeded_primitive_is_its_repos_own_with_the_image_written_in(
    reference: str,
) -> None:
    """The fake seeds each as bootstrap mirrors it, the primitive repo's own
    `primitive.yaml` with the image line written in at its top.
    """
    name = reference.removeprefix("unicon/").partition("@")[0]
    own = sibling(f"primitive-{name}", "primitive.yaml").read_text(encoding="utf-8")
    lines = PRIMITIVES[reference].decode().splitlines(keepends=True)
    assert "".join(line for line in lines if not line.startswith("image: ")) == own


def test_a_primitive_and_its_ports_say_what_they_are_for() -> None:
    exec_ = parse_primitive(PRIMITIVES["unicon/exec@v2"])
    plain = parse_primitive(BASE + OUTCOME)

    assert exec_.title == "Run programs"
    assert exec_.description is not None and exec_.description.startswith("Compiles")
    assert exec_.forms["build"].inputs["language"].description == (
        "The language the source is written in; ipynb is a Jupyter notebook."
    )
    assert exec_.forms["build"].outputs["outcome"].outcomes
    assert (plain.title, plain.description, plain.outputs["outcome"].description) == (
        None,
        None,
        None,
    )


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
    exec_ = parse_primitive(PRIMITIVES["unicon/exec@v2"])
    said = {name: form.outputs["outcome"].outcomes for name, form in exec_.forms.items()}

    assert {name: list(outcomes or {}) for name, outcomes in said.items()} == {
        "one": [
            "accepted",
            "wrong_answer",
            "time_limit",
            "memory_limit",
            "output_limit",
            "runtime_error",
            "compile_error",
        ],
        "programs": ["accepted", "compile_error"],
        "build": ["accepted", "compile_error"],
    }
    assert (said["build"] or {})["accepted"] == "the source compiles"


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


def test_an_input_port_is_one_of_the_input_types() -> None:
    assert refused(BASE + "inputs:\n  k: outcome\n" + OUTCOME) == [
        {
            "path": "inputs.k.type",
            "message": "An input must be text, a number, true or false, an enum, a file, a "
            "folder, named files, programs or links.",
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
    formed = mirrored("exec")
    stepped = formed.for_step(
        ("source", "language", "time_limit", "memory_limit"), "unicon/exec@v2"
    )
    assert isinstance(stepped, PrimitiveDeclaration)
    exec_ = stepped

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
    assert exec_.limits_from["memory_mb"] == FromInput(input="memory_limit", add=256)


def test_execs_programs_form_runs_programs_linked_and_sizes_its_container() -> None:
    formed = mirrored("exec")
    several = formed.for_step(("programs",), "unicon/exec@v2")
    assert isinstance(several, PrimitiveDeclaration)

    assert (several.schema_version, several.cache, several.outputs_from) == (
        6,
        True,
        "programs",
    )
    programs = several.inputs["programs"]
    assert programs.type is Type.PROGRAMS and programs.runs is None
    assert programs.inputs is not None and programs.outputs is not None
    assert list(programs.inputs) == [
        "source",
        "language",
        "entry",
        "files",
        "args",
        "time_limit",
        "memory_limit",
        "manifest",
        "stops_others",
    ]
    assert programs.inputs["source"].runs is True
    assert programs.inputs["source"].type is Type.FOLDER
    assert programs.outputs["log"].once is True
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
    assert several.inputs["links"].type is Type.LINKS
    assert set(several.outputs) == {"facts", "outcome"}
    assert several.outputs["outcome"].once == ("compile_error",)
    assert several.limits_from["time_ms"] == FromMax(
        max="programs.time_limit", scale=4000, add=6000
    )
    assert several.limits_from["memory_mb"] == FromSum(sum="programs.memory_limit", add=512)


def test_exec_compiles_once_and_gives_its_log_and_a_compile_error_once() -> None:
    formed = mirrored("exec")
    one = formed.forms["one"]
    built = formed.for_step(("source", "language"), "unicon/exec@v2")
    assert isinstance(built, PrimitiveDeclaration)

    assert one.outputs["log"].once is True
    assert one.outputs["outcome"].once == ("compile_error",)
    assert one.outputs["outcome"].once_outcomes == ("compile_error",)
    assert one.outputs["output"].once is None
    assert one.limits is not None and one.limits.once_time_ms == 40000
    assert (built.outputs_from, set(built.inputs)) == (None, {"source", "language", "entry"})
    assert set(built.outputs) == {"log", "outcome"}
    assert built.limits is not None and built.limits.once_time_ms == 40000


@pytest.mark.parametrize("name", ["exec"])
def test_each_contract_6_primitive_keeps_the_contract(name: str) -> None:
    text = sibling(f"primitive-{name}", "primitive.yaml").read_text(encoding="utf-8")
    image = f"image: ghcr.io/uniconhq/primitive-{name}@{PLACEHOLDER_DIGEST}\n"
    assert violation(load_yaml(image + text), "primitive") is None


def test_every_primitive_deploy_pins_reads_as_it_did() -> None:
    """Each version `images.json` pins, from its release. One that says
    `schema_version: 6` parses. An older declaration, with `name` and
    `version` lines and no schema version, is refused as before. A retired
    version is refused by name before its declaration is read, so it is
    not read here.
    """
    for name, versions in pins().items():
        for version, pin in versions.items():
            if retired(PRIMITIVE_OWNER, name, version):
                continue
            text = released(f"primitive-{name}", pin["source"]["tag"], "primitive.yaml")
            with_image = f"image: {pin['image']}\n" + text
            if "schema_version:" in text:
                assert parse_primitive(with_image).schema_version == 6
                continue
            assert version == "v1"
            assert {"name", "version"} <= {problem["path"] for problem in refused(with_image)}


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
    assert parse_primitive(text).schema_version == 6


def test_a_declaration_says_schema_version_6() -> None:
    assert parse_primitive(BASE + OUTCOME).schema_version == 6
    unnamed = BASE.replace("schema_version: 6\n", "")
    assert [problem["path"] for problem in refused(unnamed + OUTCOME)] == ["schema_version"]
    for other in ("4", "5", "7", "'6'", "true", "6.0"):
        assert refused(f"schema_version: {other}\n" + unnamed + OUTCOME) == [
            {"path": "schema_version", "message": "This must be 6."}
        ]


V6 = (
    BASE
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
    BASE
    + """\
outputs_from: programs
limits_from:
  memory_mb: {sum: programs.memory_limit, add: 512}
inputs:
  programs:
    type: programs
    inputs:
      source: {type: folder, runs: true}
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
            "      source: {type: folder, runs: true}\n",
            "",
            "inputs.programs.inputs.source",
            "A programs port's programs each take a source, a folder input called source "
            "with runs: true.",
        ),
        (
            PROGRAMS_V6,
            "      source: {type: folder, runs: true}",
            "      source: {type: folder, runs: false}",
            "inputs.programs.inputs.source",
            "A programs port's programs each take a source",
        ),
        (
            PROGRAMS_V6,
            "      source: {type: folder, runs: true}",
            "      source: {type: file, runs: true}",
            "inputs.programs.inputs.source",
            "A programs port's programs each take a source",
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


ONCE = (
    BASE
    + """\
batch: true
inputs:
  source: {type: folder, runs: true}
outputs:
  log: {type: text, once: true}
  outcome:
    type: outcome
    once: [compile_error]
    outcomes: {accepted: it runs, compile_error: it does not compile}
"""
)


def test_an_output_a_batch_step_gives_once_says_so_and_the_outcome_lists_which() -> None:
    declared = parse_primitive(ONCE)

    assert declared.outputs["log"].once is True
    assert declared.outputs["outcome"].once == ("compile_error",)
    assert declared.outputs["outcome"].once_outcomes == ("compile_error",)
    assert declared.outputs["log"].once_outcomes == ()
    plain = parse_primitive(ONCE.replace("    once: [compile_error]\n", ""))
    assert plain.outputs["outcome"].once is None


@pytest.mark.parametrize(
    ("old", "new", "path", "message"),
    [
        (
            "  log: {type: text, once: true}",
            "  log: {type: text, once: [compile_error]}",
            "outputs.log.once",
            "Only the outcome port lists outcomes here. Say true.",
        ),
        (
            "    once: [compile_error]",
            "    once: []",
            "outputs.outcome.once",
            "List at least one outcome, such as compile_error.",
        ),
        (
            "    once: [compile_error]",
            "    once: [compile_error, compile_error]",
            "outputs.outcome.once[1]",
            "already listed",
        ),
        (
            "    once: [compile_error]",
            "    once: [time_limit]",
            "outputs.outcome.once[0]",
            "time_limit is not among the port's outcomes.",
        ),
        (
            "    once: [compile_error]",
            "    once: true",
            "outputs.outcome.once",
            "List the outcomes the step gives once, such as [compile_error].",
        ),
        (
            "  source: {type: folder, runs: true}",
            "  source: {type: folder, runs: true, once: true}",
            "inputs.source.once",
            "This applies only to an output.",
        ),
    ],
)
def test_once_that_breaks_a_rule_is_refused_saying_which(
    old: str, new: str, path: str, message: str
) -> None:
    changed = ONCE.replace(old, new)
    assert changed != ONCE

    problems = refused(changed)

    assert path in [problem["path"] for problem in problems]
    assert message in next(problem["message"] for problem in problems if problem["path"] == path)


def test_the_time_a_batch_step_takes_once_is_a_limit_of_its_own() -> None:
    limits = "{time_ms: 1, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1, gpus: 0}"
    timed = ONCE.replace(limits, limits[:-1] + ", once_time_ms: 40000}")
    assert timed != ONCE

    declared = parse_primitive(timed)

    assert declared.limits is not None and declared.limits.once_time_ms == 40000
    assert parse_primitive(ONCE).limits.once_time_ms is None  # type: ignore[union-attr]
    assert "limits.once_time_ms" in [
        problem["path"] for problem in refused(timed.replace("40000", "0"))
    ]


def test_a_programs_port_is_called_programs() -> None:
    problems = refused(PROGRAMS_V6.replace("  programs:\n    type", "  players:\n    type"))

    assert {"path": "inputs.players", "message": "A programs port must be called programs."} in (
        problems
    )


_LIMITS = "{time_ms: 1, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1, gpus: 0}"
FORMS = f"""\
schema_version: 6
image: ghcr.io/uniconhq/primitive-probe@{DIGEST}
forms:
  one:
    description: Runs one program.
    limits: {_LIMITS}
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


def test_a_two_form_declaration_parses() -> None:
    declared = parse_primitive(FORMS)

    assert declared.limits is None and set(declared.forms) == {"one", "several"}
    assert declared.forms["one"].description == "Runs one program."
    assert set(declared.forms["one"].inputs) == {"program"}


def test_a_form_key_at_the_top_beside_forms_is_refused() -> None:
    assert refused(FORMS + f"limits: {_LIMITS}\n") == [
        {
            "path": "limits",
            "message": "A primitive with forms gives this on each form, not here.",
        }
    ]


def test_a_fifth_form_is_refused() -> None:
    extra = ""
    for name in ("a", "b", "c"):
        extra += (
            f"  {name}:\n    limits: {_LIMITS}\n    inputs:\n      {name}: {{type: text}}\n"
            "    outputs:\n      outcome: outcome\n"
        )
    assert refused(FORMS + extra) == [
        {"path": "forms", "message": "A primitive has at most 4 forms."}
    ]


def test_a_form_name_must_be_a_port_name() -> None:
    text = FORMS.replace("  one:\n", "  One:\n")
    [problem] = refused(text)
    assert problem["path"] == "forms.One"
    assert problem["message"].startswith("A form's name may use only")


def test_two_forms_that_each_declare_every_input_either_requires_are_refused() -> None:
    text = FORMS.replace(
        "      program: {type: text}\n",
        "      program: {type: text}\n      bundle: {type: text, optional: true}\n",
        1,
    ).replace(
        "      bundle: {type: text}\n",
        "      bundle: {type: text}\n      program: {type: text, optional: true}\n",
    )
    assert refused(text) == [
        {
            "path": "forms.several",
            "message": "A step giving bundle, program fits both one and several. Give one of "
            "them a required input the other does not declare.",
        }
    ]
    alike = FORMS.replace("      bundle: {type: text}\n", "      program: {type: text}\n")
    assert refused(alike)[0]["message"].startswith("A step giving program fits both one and")


def test_a_form_whose_inputs_are_part_of_anothers_is_told_apart_by_what_it_lacks() -> None:
    """As exec's `build` is of `one`: a step giving `program` alone fits
    `several`, and one giving `only` too fits `one`."""
    text = FORMS.replace(
        "      program: {type: text}\n",
        "      program: {type: text}\n      only: {type: text}\n",
        1,
    ).replace("      bundle: {type: text}\n", "      program: {type: text}\n")
    declared = parse_primitive(text)

    one = declared.for_step(("program", "only"), "acme/two@v1")
    several = declared.for_step(("program",), "acme/two@v1")
    assert isinstance(one, PrimitiveDeclaration) and isinstance(several, PrimitiveDeclaration)
    assert (set(one.inputs), set(several.inputs)) == ({"program", "only"}, {"program"})


def test_for_step_picks_the_one_form_the_inputs_fit() -> None:
    declared = parse_primitive(FORMS)
    one = declared.for_step(("program",), "acme/two@v1")
    several = declared.for_step(("bundle",), "acme/two@v1")
    assert isinstance(one, PrimitiveDeclaration) and isinstance(several, PrimitiveDeclaration)
    assert one.forms == {} and set(one.inputs) == {"program"} and one.limits is not None
    assert one.limits.time_ms == 1
    assert several.forms == {} and set(several.inputs) == {"bundle"}
    assert several.limits is not None and several.limits.time_ms == 2
    formless = parse_primitive(BASE + OUTCOME)
    assert formless.for_step(("program",), "acme/two@v1") is formless


def test_for_step_refuses_inputs_that_fit_no_form_or_more_than_one() -> None:
    declared = parse_primitive(FORMS)
    none = declared.for_step(("other",), "acme/two@v1")
    assert none == (
        "The step's inputs fit no form of acme/two@v1. "
        "Give every required input of one form and no input it lacks: "
        "one (program), several (bundle)."
    )
    both = PrimitiveDeclaration.model_construct(
        schema_version=6,
        image=declared.image,
        forms={
            "one": PrimitiveForm.model_construct(
                limits=declared.forms["one"].limits,
                inputs=declared.forms["one"].inputs,
                outputs=declared.forms["one"].outputs,
            ),
            "several": PrimitiveForm.model_construct(
                limits=declared.forms["several"].limits,
                inputs=declared.forms["one"].inputs,
                outputs=declared.forms["several"].outputs,
            ),
        },
    )
    assert both.for_step(("program",), "acme/two@v1") == (
        "The step's inputs fit more than one form of acme/two@v1. "
        "Give every required input of one form and no input it lacks: "
        "one (program), several (program)."
    )
