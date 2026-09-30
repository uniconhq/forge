"""The rules under uploads, with no database: a primitive's declaration
reads or is refused at its path; and a file name, an `accept` list and the
parts of a large file.
"""

import pytest

from forge.domain.primitives import PortType, parse_primitive
from forge.domain.uploads import (
    PART_SIZE,
    SINGLE_REQUEST_MAX,
    accepts,
    filename_problem,
    in_one_request,
    part_size,
    parts_of,
)
from forge.domain.yaml_models import InvalidDefinition
from forge.testing import PRIMITIVES

DIGEST = "sha256:" + "a" * 64
BASE = f"""\
name: unicon/probe
version: v1
image: ghcr.io/uniconhq/primitive-probe@{DIGEST}
entrypoint: [/probe]
limits: {{time_ms: 1, cpu_ms: 1, memory_mb: 1, pids: 1, output_mb: 1}}
"""


def test_the_three_primitives_read_as_the_contract_declares_them() -> None:
    compile_ = parse_primitive(PRIMITIVES["compile"])
    run = parse_primitive(PRIMITIVES["sandbox-run"])
    check = parse_primitive(PRIMITIVES["diff-check"])

    assert (compile_.short_name, compile_.batch, run.batch, check.batch) == (
        "compile",
        False,
        True,
        True,
    )
    assert compile_.inputs["language"].values == ("python", "c", "cpp", "java")
    assert compile_.outputs["binary"].optional is True
    assert run.limits_from["time_ms"].input == "time_limit"
    assert check.outputs["outcome"].type is PortType.OUTCOME


@pytest.mark.parametrize(
    ("extra", "path"),
    [
        ("inputs:\n  kind: {type: enum}\n", "inputs.kind.values"),
        ("inputs:\n  kind: {type: text, values: [a]}\n", "inputs.kind.values"),
        (
            "inputs:\n  kind: {type: text}\nlimits_from:\n  time_ms: {input: kind}\n",
            "limits_from.time_ms.input",
        ),
        ("outputs:\n  kind: {type: list}\n", "outputs.kind.type"),
        ("schema_version: 2\n", "schema_version"),
        ("colour: red\n", "colour"),
    ],
    ids=["enum-without-values", "values-on-text", "limit-from-text", "unknown-type", "v2", "key"],
)
def test_a_declaration_that_breaks_the_contract_is_refused_at_its_path(
    extra: str, path: str
) -> None:
    with pytest.raises(InvalidDefinition) as refused:
        parse_primitive(BASE + extra)
    assert path in [problem["path"] for problem in refused.value.errors]


def test_a_declaration_names_its_image_by_digest_and_may_say_its_version() -> None:
    assert parse_primitive(BASE + "schema_version: 3\n").schema_version == 3
    local = BASE.replace("ghcr.io/uniconhq", "localhost:5000")
    assert parse_primitive(local).image.startswith("localhost:5000/")
    with pytest.raises(InvalidDefinition) as refused:
        parse_primitive(BASE.replace(f"@{DIGEST}", ":latest"))
    assert [problem["path"] for problem in refused.value.errors] == ["image"]


@pytest.mark.parametrize(
    ("name", "fine"),
    [
        ("main.py", True),
        ("model weights.bin", True),
        (".env", True),
        ("", False),
        ("..", False),
        ("a/b.py", False),
        ("a\\b.py", False),
        ("what?.py", False),
        (" main.py", False),
        ("x" * 256, False),
    ],
)
def test_a_file_name_is_one_plain_name(name: str, fine: bool) -> None:
    assert (filename_problem(name) is None) is fine


def test_accept_takes_endings_and_content_types() -> None:
    assert accepts(None, "anything", None)
    assert accepts((".py", ".cpp"), "Main.PY", None)
    assert not accepts((".py",), "main.pyc", None)
    assert accepts(("image/*",), "x.bin", "image/png")
    assert accepts(("application/zip",), "x", "application/zip; charset=binary")
    assert accepts(("csv",), "data.csv", None)
    assert not accepts(("text/plain",), "x.txt", "text/html")


def test_a_large_file_goes_in_parts_of_exact_lengths() -> None:
    assert in_one_request(SINGLE_REQUEST_MAX) and not in_one_request(SINGLE_REQUEST_MAX + 1)
    size = SINGLE_REQUEST_MAX + 5
    parts = parts_of(size)
    whole = SINGLE_REQUEST_MAX // PART_SIZE
    assert [part.number for part in parts] == list(range(1, whole + 2))
    assert [part.length for part in parts] == [PART_SIZE] * whole + [5]
    assert sum(part.length for part in parts) == size
    huge = 100 * 1024**3
    assert part_size(huge) > PART_SIZE and len(parts_of(huge)) <= 1000
    assert sum(part.length for part in parts_of(huge)) == huge
