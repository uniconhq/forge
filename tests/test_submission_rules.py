"""The rules under uploads and submissions, with no database: a primitive's
declaration reads or is refused at its path; a file name, an `accept` list
and the parts of a large file; what a submission lays out and what it
refuses, input by input; the note its protected version carries; and the
two secrets a grading run is handed, derived and never stored.
"""

import json
import uuid

import pytest

from forge.domain.definitions import parse_task, starter_task
from forge.domain.errors import InvalidInputs
from forge.domain.grading import callback_token, envelope_key, token_hash
from forge.domain.primitives import PortType, parse_primitive
from forge.domain.submissions import (
    SubmittedInput,
    UploadedFile,
    key_is_valid,
    lay_out,
    read_note,
    write_note,
)
from forge.domain.uploads import (
    POINTER_MAX,
    accepts,
    digest_problem,
    filename_problem,
    is_pointer,
    pointer_text,
)
from forge.domain.yaml_models import InvalidDefinition
from forge.testing import PRIMITIVES

DIGEST = "sha256:" + "a" * 64
BASE = f"""\
name: unicon/probe
version: v1
image: ghcr.io/uniconhq/primitive-probe@{DIGEST}
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
        ("schema_version: 3\n", "schema_version"),
        ("colour: red\n", "colour"),
    ],
    ids=["enum-without-values", "values-on-text", "limit-from-text", "unknown-type", "v3", "key"],
)
def test_a_declaration_that_breaks_the_contract_is_refused_at_its_path(
    extra: str, path: str
) -> None:
    with pytest.raises(InvalidDefinition) as refused:
        parse_primitive(BASE + extra)
    assert path in [problem["path"] for problem in refused.value.errors]


def test_a_declaration_names_its_image_by_digest_and_may_say_its_version() -> None:
    assert parse_primitive(BASE + "schema_version: 4\n").schema_version == 4
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
        (".gitattributes", False),
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


def test_a_pointer_is_the_three_lines_git_lfs_reads() -> None:
    digest, size = "b" * 64, 4096
    written = pointer_text(digest, size)
    assert written == (
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:" + digest.encode() + b"\n"
        b"size 4096\n"
    )
    assert is_pointer(written)


def test_what_counts_as_a_pointer_is_what_a_checkout_would_act_on() -> None:
    # Anything a checkout would resolve against the org's shared store,
    # however malformed after its first line, has to be refused as typed
    # content; what git-lfs would treat as an ordinary file does not.
    assert is_pointer(b"version https://git-lfs.github.com/spec/v1\nnonsense\n")
    assert is_pointer(b"version https://hawser.github.com/spec/v1\n")
    assert is_pointer(b"  version https://git-lfs.github.com/spec/v1  \noid sha256:x\n")
    assert is_pointer(b"\n\nversion https://git-lfs.github.com/spec/v1\noid sha256:x\n")
    assert is_pointer(b"version http://git-media.io/v/2\noid sha256:x\n")
    assert is_pointer(
        b"version https://git-lfs.github.com/spec/v1\noid sha256:x\nsize 1\n" + b" " * POINTER_MAX
    )
    assert not is_pointer(b"print('hello')\n")
    assert not is_pointer(b"version 1\n")
    assert not is_pointer(b"# version https://git-lfs.github.com/spec/v1\n")
    assert not is_pointer(b" " * POINTER_MAX + b"version https://git-lfs.github.com/spec/v1\n")


def test_a_digest_is_lowercase_hex_of_the_right_length() -> None:
    assert digest_problem("a" * 64) is None
    assert digest_problem("A" * 64) is not None
    assert digest_problem("a" * 63) is not None
    assert digest_problem("g" * 64) is not None
    assert digest_problem("") is not None


TASK = parse_task(
    starter_task("Sum")["task.yaml"].replace(
        b"      language: [python]\n",
        b"      language: [python, cpp]\n"
        b"    - {id: weights, type: 'file[]', max_size: 1MB}\n"
        b"    - {id: alpha, type: number, min: 0, max: 1, default: 0.5}\n"
        b"    - {id: note, type: text}\n",
    )
)
SOURCE, W1, W2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
UPLOADS = {
    SOURCE: UploadedFile(SOURCE, "submission", "main.py", 10),
    W1: UploadedFile(W1, "weights", "b.bin", 3),
    W2: UploadedFile(W2, "weights", "a.bin", 3),
}


def test_a_submission_lays_out_its_files_and_names_them_in_submission_json() -> None:
    layout = lay_out(
        TASK.inputs.contestant,
        {
            "submission": SubmittedInput(uploads=(SOURCE,), language="cpp"),
            "weights": SubmittedInput(uploads=(W1, W2)),
            "note": SubmittedInput(value="hello"),
        },
        UPLOADS,
    )

    assert layout.files == {
        "files/submission/main.py": SOURCE,
        "files/weights/a.bin": W2,
        "files/weights/b.bin": W1,
    }
    assert json.loads(layout.document) == {
        "schema_version": 4,
        "inputs": {
            "submission": {"files": ["files/submission/main.py"], "language": "cpp"},
            "weights": {"files": ["files/weights/a.bin", "files/weights/b.bin"]},
            "alpha": {"value": 0.5},
            "note": {"value": "hello"},
        },
    }
    assert layout.document.endswith(b"}\n")


@pytest.mark.parametrize(
    ("given", "input", "message"),
    [
        ({"submission": SubmittedInput(uploads=(SOURCE,))}, "submission", "Choose one of"),
        (
            {"submission": SubmittedInput(uploads=(SOURCE,), language="rust")},
            "submission",
            "Choose one of the languages python, cpp.",
        ),
        ({"submission": SubmittedInput(value="print(1)")}, "submission", "needs a file"),
        (
            {"submission": SubmittedInput(uploads=(SOURCE, W1), language="cpp")},
            "submission",
            "exactly one file",
        ),
        (
            {"submission": SubmittedInput(uploads=(W1,), language="cpp")},
            "submission",
            "another input",
        ),
        ({"alpha": SubmittedInput(value=2)}, "alpha", "Must be at most 1."),
        ({"alpha": SubmittedInput(value=True)}, "alpha", "Must be a number."),
        ({"alpha": SubmittedInput(value=float("nan"))}, "alpha", "Must be a number."),
        ({"note": SubmittedInput(value=None)}, "note", "This input is required."),
        ({"note": SubmittedInput(uploads=(W1,))}, "note", "not files"),
        ({"ghost": SubmittedInput(value=1)}, "ghost", "The task has no such input."),
    ],
    ids=[
        "no-language",
        "unlisted-language",
        "value-for-code",
        "two-files",
        "upload-of-another-input",
        "above-max",
        "boolean-for-number",
        "nan-for-number",
        "required-text",
        "files-for-text",
        "unknown-input",
    ],
)
def test_a_submission_that_does_not_fit_the_inputs_is_refused_naming_the_input(
    given: dict[str, SubmittedInput], input: str, message: str
) -> None:
    complete = {
        "submission": SubmittedInput(uploads=(SOURCE,), language="cpp"),
        "weights": SubmittedInput(uploads=(W1,)),
        "note": SubmittedInput(value="x"),
    }
    with pytest.raises(InvalidInputs) as refused:
        lay_out(TASK.inputs.contestant, {**complete, **given}, UPLOADS)
    errors = refused.value.extra["errors"]
    assert [error["input"] for error in errors] == [input]
    assert message in errors[0]["message"]


def test_two_files_of_one_name_are_refused() -> None:
    twin = uuid.uuid4()
    uploads = {**UPLOADS, twin: UploadedFile(twin, "weights", "a.bin", 1)}
    with pytest.raises(InvalidInputs) as refused:
        lay_out(
            TASK.inputs.contestant,
            {
                "submission": SubmittedInput(uploads=(SOURCE,), language="cpp"),
                "weights": SubmittedInput(uploads=(W2, twin)),
                "note": SubmittedInput(value="x"),
            },
            uploads,
        )
    assert refused.value.extra["errors"] == [
        {"input": "weights", "message": "Two files have the same name."}
    ]


def test_a_key_is_short_random_text_and_its_note_reads_back() -> None:
    assert key_is_valid(str(uuid.uuid4()))
    assert not key_is_valid("short")
    assert not key_is_valid("has space 12345")
    assert not key_is_valid("x" * 129)
    assert read_note(write_note("key-12345678")) == "key-12345678"
    assert read_note(None) is None
    assert read_note("[1") is None
    assert read_note("idempotency_key: 7") is None


def test_a_gradings_secrets_are_its_own_and_only_the_hash_is_kept() -> None:
    key = b"\x01" * 32
    one, two = uuid.uuid4(), uuid.uuid4()

    token = callback_token(key, one)
    assert token == callback_token(key, one)
    assert token != callback_token(key, two)
    assert token != callback_token(b"\x02" * 32, one)
    assert token != envelope_key(key, one)
    assert envelope_key(key, one) != envelope_key(key, two)
    assert "=" not in token and len(token) == 43
    assert len(token_hash(token)) == 32
    assert token_hash(token) != token.encode()
