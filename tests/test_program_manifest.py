"""A program's manifest, its `program.yaml`: a report of each kind parses,
what breaks the format or a primitive's name rules is refused naming the
file and the report, and the JSON the forge hands a primitive is what the
primitive's own `read_manifest` accepts, checked against `exec` and
`interact` in the checkouts beside this one, whose name rules the forge
copies.
"""

import importlib.util
import sys
from collections.abc import Collection
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from forge.domain import exact_json
from forge.domain.blocks.numbers import Better
from forge.domain.program_manifest import (
    EXEC_OWN_NAMES,
    INTERACT_OWN_NAMES,
    MAX_MANIFEST,
    Report,
    parse_program,
)
from forge.domain.workflow_definition import REPORT_NAME
from forge.domain.yaml_models import InvalidDefinition, Problem
from tests.conftest import sibling

PATH = "checker/program.yaml"
EVERY_KIND = """\
reports:
  score: {better: higher, at_least: 0, at_most: 1}
  error: {type: number, better: lower, at_least: 0, optional: true}
  steps: {}
  note: {type: text, optional: true}
  verdict_detail: {type: text}
"""


def primitive(name: str) -> ModuleType:
    """`primitive-<name>`'s program, from the checkout beside this one."""
    path = sibling(f"primitive-{name}", "src", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"unicon_primitive_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module", params=["exec", "interact"])
def checker(request: pytest.FixtureRequest) -> tuple[ModuleType, frozenset[str]]:
    """Each primitive that runs a program given a manifest, with the names
    the forge copies for it.
    """
    own = {"exec": EXEC_OWN_NAMES, "interact": INTERACT_OWN_NAMES}[request.param]
    return primitive(request.param), own


def refused(text: str, own_names: Collection[str] = EXEC_OWN_NAMES) -> InvalidDefinition:
    with pytest.raises(InvalidDefinition) as raised:
        parse_program(text, PATH, own_names=own_names)
    return raised.value


def read_by(module: ModuleType, manifest: bytes, tmp_path: Path) -> dict[str, Any]:
    """The reports `module`'s `read_manifest` reads from `manifest`."""
    path = tmp_path / "manifest.json"
    path.write_bytes(manifest)
    read: dict[str, Any] = module.read_manifest(path, "the manifest")
    return read


def test_a_report_of_each_kind_parses_in_the_files_order() -> None:
    manifest = parse_program(EVERY_KIND, PATH, own_names=EXEC_OWN_NAMES)

    assert list(manifest.reports) == ["score", "error", "steps", "note", "verdict_detail"]
    score, error, steps, note = (
        manifest.reports[name] for name in ("score", "error", "steps", "note")
    )
    assert (score.type, score.optional, score.better, score.at_least, score.at_most) == (
        "number",
        False,
        Better.HIGHER,
        0,
        1,
    )
    assert (error.better, error.optional, error.at_most) == (Better.LOWER, True, None)
    assert (steps.type, steps.better, steps.at_least) == ("number", None, None)
    assert (note.type, note.optional) == ("text", True)


def test_the_json_writes_every_key_and_each_bound_exactly() -> None:
    text = "reports:\n  share: {better: higher, at_least: 0.1, at_most: 1.0000000000000000001}\n"
    manifest = parse_program(text, PATH, own_names=EXEC_OWN_NAMES)

    assert exact_json.loads(manifest.as_json()) == {
        "reports": {
            "share": {
                "type": "number",
                "optional": False,
                "better": "higher",
                "at_least": Decimal("0.1"),
                "at_most": Decimal("1.0000000000000000001"),
            }
        }
    }
    assert parse_program("reports: {}\n", PATH, own_names=EXEC_OWN_NAMES).as_json() == (
        b'{"reports": {}}'
    )


@pytest.mark.parametrize(
    ("text", "path", "message"),
    [
        ("reports: {}\nversion: 1\n", "version", "Unicon does not know this setting."),
        ("{}\n", "reports", "This is required."),
        (
            "reports:\n  score: {title: Score}\n",
            "reports.score.title",
            "Unicon does not know this setting.",
        ),
        (
            "reports:\n  note: {type: text, better: higher}\n",
            "reports.note.better",
            "note is text, and only a number says which way is better or its bounds.",
        ),
        (
            "reports:\n  note: {type: text, at_most: 1}\n",
            "reports.note.at_most",
            "note is text, and only a number says which way",
        ),
        (
            "reports:\n  score: {at_least: 2, at_most: 1}\n",
            "reports.score.at_most",
            "score has at_least 2 above at_most 1, so no value fits.",
        ),
        ("reports:\n  score: {better: faster}\n", "reports.score.better", "higher or lower"),
        ("reports:\n  score: {type: file}\n", "reports.score.type", "number or text"),
        ("reports:\n  score: {optional: 'yes'}\n", "reports.score.optional", "true or false"),
        ("reports:\n  score: {at_most: .inf}\n", "reports.score.at_most", "a number"),
        (
            "reports:\n  score: {at_least: -1e400}\n",
            "reports.score.at_least",
            "too large for the primitive to read",
        ),
        (
            "reports:\n  score: {at_most: 1" + "0" * 400 + "}\n",
            "reports.score.at_most",
            "too large for the primitive to read",
        ),
        (
            "reports:\n  verdict: {}\n",
            "reports.verdict",
            "verdict is a name the primitive running the program gives itself.",
        ),
        ("reports:\n  time_ms: {}\n", "reports.time_ms", "time_ms is a name the primitive"),
        ("reports:\n  Bad: {}\n", "reports.Bad", "'Bad' cannot be a report's name."),
        ("reports:\n  " + "a" * 65 + ": {}\n", "reports." + "a" * 65, "at most 64"),
    ],
)
def test_a_manifest_that_breaks_a_rule_is_refused_naming_the_file_and_the_report(
    text: str, path: str, message: str
) -> None:
    problem = refused(text)

    assert [found["path"] for found in problem.errors] == [path]
    assert message in problem.errors[0]["message"]
    assert PATH in problem.detail


def test_a_name_interact_gives_is_refused_only_under_interact() -> None:
    text = "reports:\n  ended: {type: text}\n  facts: {}\n"

    assert list(parse_program(text, PATH, own_names=EXEC_OWN_NAMES).reports) == ["ended", "facts"]
    assert [found["path"] for found in refused(text, INTERACT_OWN_NAMES).errors] == [
        "reports.ended",
        "reports.facts",
    ]


def test_a_manifest_over_the_size_is_refused_unread() -> None:
    padded = b"reports: {}\n" + b"#" * MAX_MANIFEST

    with pytest.raises(InvalidDefinition) as raised:
        parse_program(padded, "diff/program.yaml", own_names=EXEC_OWN_NAMES)

    assert raised.value.errors == [
        Problem(path="", message="A program.yaml can be at most 256 KB. Make it smaller.")
    ]
    fits = b"reports: {}\n" + b"#" * (MAX_MANIFEST - 12)
    assert parse_program(fits, "diff/program.yaml", own_names=EXEC_OWN_NAMES).reports == {}


def test_every_problem_is_listed_together() -> None:
    problem = refused("reports:\n  verdict: {}\n  note: {type: text, better: higher}\n")

    assert problem.errors == [
        Problem(
            path="reports.verdict",
            message="verdict is a name the primitive running the program gives itself. "
            "Report this under another name.",
        ),
        Problem(
            path="reports.note.better",
            message="note is text, and only a number says which way is better or its bounds. "
            "Remove this.",
        ),
    ]


@pytest.mark.parametrize(
    ("key", "shown"), [("1", "1"), ("true", "true"), ("2.5", "2.5"), ("null", "null")]
)
def test_a_name_that_is_not_text_is_one_problem_at_its_written_path(key: str, shown: str) -> None:
    problem = refused(f"reports:\n  {key}: {{title: x}}\n  score: {{}}\n")

    assert problem.errors == [
        Problem(
            path=f"reports.{shown}",
            message=f"'{shown}' cannot be a report's name. Use at most 64 lower case letters, "
            "digits and underscores (_), starting with a letter.",
        )
    ]


def test_the_name_rules_are_the_primitives_own(checker: tuple[ModuleType, frozenset[str]]) -> None:
    module, own = checker

    assert module.REPORT_NAME.pattern == REPORT_NAME.pattern
    assert own == module.OWN_NAMES
    assert frozenset(Report.model_fields) == module.REPORT_KEYS


@pytest.mark.parametrize(
    "text",
    [
        EVERY_KIND,
        "reports: {}\n",
        "reports:\n  share: {better: higher, at_least: 0.1, at_most: 1.0000000000000000001}\n",
        "reports:\n  big: {at_least: -12345678901234567890123, at_most: 1e300}\n",
    ],
)
def test_the_primitive_reads_the_json_as_the_forge_parsed_it(
    checker: tuple[ModuleType, frozenset[str]], text: str, tmp_path: Path
) -> None:
    module, own = checker
    manifest = parse_program(text, PATH, own_names=own)

    read = read_by(module, manifest.as_json(), tmp_path)

    assert list(read) == list(manifest.reports)
    for name, report in manifest.reports.items():
        assert (read[name].kind, read[name].optional) == (report.type, report.optional)
        assert (read[name].at_least, read[name].at_most) == (report.at_least, report.at_most)


@pytest.mark.parametrize(
    "reports",
    [
        {"verdict": {"type": "number"}},
        {"time_ms": {"type": "number"}},
        {"Bad": {"type": "number"}},
        {"score": {"type": "number", "title": "Score"}},
        {"note": {"type": "text", "better": "higher"}},
        {"score": {"type": "number", "at_least": 2, "at_most": 1}},
        {"score": {"type": "number", "at_most": Decimal("1e400")}},
        {"score": {"type": "number", "at_least": -(10**400)}},
    ],
)
def test_what_the_forge_refuses_the_primitive_refuses_too(
    checker: tuple[ModuleType, frozenset[str]], reports: dict[str, Any], tmp_path: Path
) -> None:
    """The forge refuses at the save what the primitive would refuse at the
    run, so no manifest it hands over is one the primitive refuses.
    """
    module, own = checker
    manifest = exact_json.dumps({"reports": reports}).encode()

    with pytest.raises(module.PrimitiveError):
        read_by(module, manifest, tmp_path)
    with pytest.raises(InvalidDefinition):
        parse_program(exact_json.dumps({"reports": reports}), PATH, own_names=own)
