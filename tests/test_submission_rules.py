"""The rules under uploads and submissions, with no database: a file name and
the parts of a large file; the form a contestant fills, from the plan's
contestant inputs and the task's form details; what a submission lays out,
keeping the contract, and what it refuses, input by input; the note its
protected version carries; and the two secrets a grading run is handed,
derived and never stored.
"""

import json
import uuid
from typing import Any

import pytest

from forge.domain.contracts import violation
from forge.domain.definitions import DEFAULT_MAX_SIZE
from forge.domain.errors import InvalidInputs
from forge.domain.grading import callback_token, envelope_key, token_hash
from forge.domain.plans import ContestantInput
from forge.domain.submissions import (
    Field,
    SubmittedInput,
    UploadedFile,
    fields_of,
    key_is_valid,
    lay_out,
    read_note,
    write_note,
)
from forge.domain.types import Type
from forge.domain.uploads import (
    POINTER_MAX,
    digest_problem,
    filename_problem,
    is_pointer,
    pointer_text,
    read_pointer,
)


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


def test_a_pointer_is_the_three_lines_git_lfs_reads() -> None:
    digest, size = "b" * 64, 4096
    written = pointer_text(digest, size)
    assert written == (
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:" + digest.encode() + b"\n"
        b"size 4096\n"
    )
    assert is_pointer(written)
    assert read_pointer(written) == (digest, size)


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
    assert read_pointer(b"version https://git-lfs.github.com/spec/v1\nnonsense\n") is None


def test_a_digest_is_lowercase_hex_of_the_right_length() -> None:
    assert digest_problem("a" * 64) is None
    assert digest_problem("A" * 64) is not None
    assert digest_problem("a" * 63) is not None
    assert digest_problem("g" * 64) is not None
    assert digest_problem("") is not None


# The form


CONTESTANT = {
    "submission": ContestantInput(type=Type.FILE),
    "language": ContestantInput(type=Type.ENUM, options=("c", "cpp", "java", "python")),
    "alpha": ContestantInput(type=Type.NUMBER),
    "fast": ContestantInput(type=Type.BOOLEAN),
    "note": ContestantInput(type=Type.TEXT),
    "weights": ContestantInput(type=Type.FOLDER),
    "answers": ContestantInput(type=Type.FILE, per_test=True),
}
"""A plan's contestant inputs, one of every kind."""

INPUTS: dict[str, Any] = {
    "time_limit": 2,
    "language": {"options": ["python", "cpp"], "default": "python", "label": "Language"},
    "submission": {"label": "Your solution", "max_size": "1KB"},
    "alpha": {"min": 0, "max": 1, "default": 0.5},
    "fast": {"default": False},
    "weights": {"max_size": "1KB"},
}
"""The task's `inputs`: a value for an input the contestant does not give,
and form details for some they do."""

TESTS = ("main/1", "main/2", "samples/1")


def test_the_form_is_the_plans_inputs_with_the_tasks_details_in_its_order() -> None:
    fields = fields_of(CONTESTANT, INPUTS)

    assert [field.id for field in fields] == [
        "language",
        "submission",
        "alpha",
        "fast",
        "weights",
        "answers",
        "note",
    ]
    by_id = {field.id: field for field in fields}
    assert by_id["language"] == Field(
        id="language",
        type=Type.ENUM,
        label="Language",
        options=("python", "cpp"),
        default="python",
    )
    assert by_id["submission"].max_size == 1024 and by_id["submission"].label == "Your solution"
    assert (by_id["alpha"].min, by_id["alpha"].max, by_id["alpha"].default) == (0, 1, 0.5)
    assert by_id["answers"].per_test and by_id["answers"].files
    assert (by_id["note"].label, by_id["note"].max_size) == ("note", DEFAULT_MAX_SIZE)
    workflows = fields_of({"language": CONTESTANT["language"]}, {})
    assert workflows[0].options == ("c", "cpp", "java", "python")


FIELDS = fields_of(CONTESTANT, INPUTS)
SOURCE, W1, W2, A1, A2, S1 = (uuid.uuid4() for _ in range(6))
UPLOADS = {
    SOURCE: UploadedFile(SOURCE, "submission", "main.py", 10),
    W1: UploadedFile(W1, "weights", "layers/b.bin", 300),
    W2: UploadedFile(W2, "weights", "a.bin", 300),
    A1: UploadedFile(A1, "answers", "main/1.txt", 5),
    A2: UploadedFile(A2, "answers", "main/2", 5),
    S1: UploadedFile(S1, "answers", "samples/1.out", 5),
}
COMPLETE = {
    "submission": SubmittedInput(uploads=(SOURCE,)),
    "language": SubmittedInput(value="cpp"),
    "note": SubmittedInput(value="hello"),
    "weights": SubmittedInput(uploads=(W1, W2)),
    "answers": SubmittedInput(uploads=(A1, A2, S1)),
}


def test_a_submission_lays_out_its_files_and_names_them_in_submission_json() -> None:
    layout = lay_out(FIELDS, TESTS, COMPLETE, UPLOADS)

    assert layout.files == {
        "files/submission/main.py": SOURCE,
        "files/weights/a.bin": W2,
        "files/weights/layers/b.bin": W1,
        "files/answers/main/1.txt": A1,
        "files/answers/main/2": A2,
        "files/answers/samples/1.out": S1,
    }
    document = json.loads(layout.document)
    assert document == {
        "schema_version": 5,
        "inputs": {
            "language": {"value": "cpp"},
            "submission": {"files": ["files/submission/main.py"]},
            "alpha": {"value": 0.5},
            "fast": {"value": False},
            "weights": {"files": ["files/weights/a.bin", "files/weights/layers/b.bin"]},
            "answers": {
                "files": [
                    "files/answers/main/1.txt",
                    "files/answers/main/2",
                    "files/answers/samples/1.out",
                ]
            },
            "note": {"value": "hello"},
        },
    }
    assert layout.document.endswith(b"}\n")
    assert violation(document, "submission") is None


def test_a_left_out_input_takes_its_default() -> None:
    layout = lay_out(FIELDS, TESTS, {**COMPLETE, "language": SubmittedInput()}, UPLOADS)

    assert json.loads(layout.document)["inputs"]["language"] == {"value": "python"}


def test_a_per_test_input_may_answer_some_tests_only() -> None:
    layout = lay_out(FIELDS, TESTS, {**COMPLETE, "answers": SubmittedInput(uploads=(A2,))}, UPLOADS)

    assert json.loads(layout.document)["inputs"]["answers"] == {"files": ["files/answers/main/2"]}


def refusal(given: dict[str, SubmittedInput], uploads: dict[uuid.UUID, UploadedFile]) -> Any:
    with pytest.raises(InvalidInputs) as refused:
        lay_out(FIELDS, TESTS, {**COMPLETE, **given}, {**UPLOADS, **uploads})
    return refused.value.extra["errors"]


EXTRA, BIG, TWIN, NOT_A_TEST, SECOND_1 = (uuid.uuid4() for _ in range(5))
MORE = {
    EXTRA: UploadedFile(EXTRA, "submission", "other.py", 10),
    BIG: UploadedFile(BIG, "submission", "big.py", 1025),
    TWIN: UploadedFile(TWIN, "weights", "a.bin", 1),
    NOT_A_TEST: UploadedFile(NOT_A_TEST, "answers", "main/3.txt", 1),
    SECOND_1: UploadedFile(SECOND_1, "answers", "main/1", 1),
}


@pytest.mark.parametrize(
    ("given", "input", "message"),
    [
        ({"language": SubmittedInput(value="rust")}, "language", "Choose one of python, cpp."),
        ({"language": SubmittedInput(value="java")}, "language", "Choose one of python, cpp."),
        ({"alpha": SubmittedInput(value=2)}, "alpha", "Must be at most 1."),
        ({"alpha": SubmittedInput(value=-0.5)}, "alpha", "Must be at least 0."),
        ({"alpha": SubmittedInput(value=True)}, "alpha", "Must be a number."),
        ({"alpha": SubmittedInput(value=float("nan"))}, "alpha", "Must be a number."),
        ({"fast": SubmittedInput(value="yes")}, "fast", "Must be true or false."),
        ({"note": SubmittedInput()}, "note", "This input is required."),
        ({"note": SubmittedInput(value=7)}, "note", "Must be text."),
        ({"note": SubmittedInput(uploads=(W1,))}, "note", "Give this input a value, not files."),
        ({"submission": SubmittedInput(value="print(1)")}, "submission", "needs a file"),
        (
            {"submission": SubmittedInput(uploads=(SOURCE, EXTRA))},
            "submission",
            "This input takes exactly one file.",
        ),
        ({"submission": SubmittedInput(uploads=(W2,))}, "submission", "another input"),
        ({"submission": SubmittedInput(uploads=(BIG,))}, "submission", "1024 bytes allowed"),
        ({"weights": SubmittedInput()}, "weights", "This input needs at least one file."),
        (
            {"weights": SubmittedInput(uploads=(W2, TWIN))},
            "weights",
            "Two files have the same path.",
        ),
        ({"weights": SubmittedInput(uploads=(W1, W1))}, "weights", "given more than once"),
        (
            {"weights": SubmittedInput(uploads=(W1, W2, TWIN))},
            "weights",
            "Two files have the same path.",
        ),
        (
            {"answers": SubmittedInput(uploads=(NOT_A_TEST,))},
            "answers",
            "main/3.txt: A file of this input is named for a test, <group>/<test>, such as "
            "main/1.txt.",
        ),
        (
            {"answers": SubmittedInput(uploads=(A1, SECOND_1))},
            "answers",
            "Two files answer the same test.",
        ),
        ({"ghost": SubmittedInput(value=1)}, "ghost", "The task has no such input."),
    ],
    ids=[
        "enum-outside-the-tasks-options",
        "enum-the-task-narrowed-away",
        "above-max",
        "below-min",
        "boolean-for-number",
        "nan-for-number",
        "text-for-boolean",
        "required-text",
        "number-for-text",
        "files-for-text",
        "value-for-file",
        "two-files-for-file",
        "upload-of-another-input",
        "file-over-its-size",
        "empty-folder",
        "same-path-twice",
        "same-upload-twice",
        "same-path-among-three",
        "not-a-plan-test",
        "one-test-twice",
        "unknown-input",
    ],
)
def test_a_submission_that_does_not_fit_the_inputs_is_refused_naming_the_input(
    given: dict[str, SubmittedInput], input: str, message: str
) -> None:
    errors = refusal(given, MORE)

    assert [error["input"] for error in errors] == [input]
    assert message in errors[0]["message"]


def test_a_folders_files_together_are_within_its_size() -> None:
    heavy = uuid.uuid4()
    uploads = {heavy: UploadedFile(heavy, "weights", "c.bin", 1024 - 600 + 1)}

    errors = refusal({"weights": SubmittedInput(uploads=(W1, W2, heavy))}, uploads)

    assert errors == [
        {
            "input": "weights",
            "message": "The files of this input total more than the 1024 bytes allowed.",
        }
    ]


def test_every_input_that_does_not_fit_is_named_together() -> None:
    errors = refusal(
        {"alpha": SubmittedInput(value=5), "note": SubmittedInput(), "ghost": SubmittedInput()},
        {},
    )

    assert [error["input"] for error in errors] == ["ghost", "alpha", "note"]


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
