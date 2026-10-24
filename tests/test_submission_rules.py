"""The rules under uploads and submissions, with no database: a file name and
the parts of a large file; the form an entrant fills, from the plan's
entrant inputs and the task's form details; what a submission lays out,
keeping the contract, and what it refuses, input by input and part by part;
and the note its protected version carries.
"""

import json
import uuid
from decimal import Decimal
from typing import Any

import pytest

from forge.domain import exact_json
from forge.domain.contracts import violation
from forge.domain.definitions import DEFAULT_MAX_SIZE
from forge.domain.errors import InvalidInputs
from forge.domain.run_plan.models import EntrantEntry
from forge.domain.run_task import Part, Run
from forge.domain.submissions import (
    Field,
    SubmittedInput,
    UploadedFile,
    key_is_valid,
    lay_out_runs,
    read_note,
    run_fields_of,
    skipped,
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
        (".GIT", False),
        (".git.", False),
        (".git..", False),
        ("GIT~1", False),
        (".github", True),
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


ENTRANT = {
    "submission": EntrantEntry(type=Type.FILE),
    "language": EntrantEntry(type=Type.ENUM, options=("c", "cpp", "java", "python")),
    "alpha": EntrantEntry(type=Type.NUMBER),
    "fast": EntrantEntry(type=Type.BOOLEAN),
    "note": EntrantEntry(type=Type.TEXT),
    "weights": EntrantEntry(type=Type.FOLDER),
}
"""A plan's entrant inputs, one of every kind."""

INPUTS: dict[str, Any] = {
    "time_limit": 2,
    "language": {
        "entrant": {"options": ["python", "cpp"], "default": "python", "label": "Language"}
    },
    "submission": {"entrant": {"label": "Your solution", "max_size": "1KB"}},
    "alpha": {"entrant": {"min": 0, "max": 1, "default": 0.5}},
    "fast": {"entrant": {"default": False}},
    "weights": {"entrant": {"max_size": "1KB"}},
}
"""The task's `inputs`: a value for an input the entrant does not give,
and form details for some they do."""


def test_the_form_is_the_plans_inputs_with_the_tasks_details_in_its_order() -> None:
    fields = run_fields_of(ENTRANT, INPUTS)

    assert [field.id for field in fields] == [
        "language",
        "submission",
        "alpha",
        "fast",
        "weights",
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
    assert (by_id["note"].label, by_id["note"].max_size) == ("note", DEFAULT_MAX_SIZE)
    workflows = run_fields_of({"language": ENTRANT["language"]}, {})
    assert workflows[0].options == ("c", "cpp", "java", "python")


FIELDS = run_fields_of(ENTRANT, INPUTS)
SOURCE, W1, W2 = (uuid.uuid4() for _ in range(3))
UPLOADS = {
    SOURCE: UploadedFile(SOURCE, "submission", "main.py", 10),
    W1: UploadedFile(W1, "weights", "layers/b.bin", 300),
    W2: UploadedFile(W2, "weights", "a.bin", 300),
}
COMPLETE = {
    "submission": SubmittedInput(uploads=(SOURCE,)),
    "language": SubmittedInput(value="cpp"),
    "note": SubmittedInput(value="hello"),
    "weights": SubmittedInput(uploads=(W1, W2)),
}


def test_a_submission_lays_out_its_files_and_names_them_in_submission_json() -> None:
    layout = lay_out_runs(FIELDS, {}, COMPLETE, UPLOADS)

    assert layout.files == {
        "files/submission/main.py": SOURCE,
        "files/weights/a.bin": W2,
        "files/weights/layers/b.bin": W1,
    }
    document = json.loads(layout.document)
    assert document == {
        "schema_version": 6,
        "inputs": {
            "language": {"value": "cpp"},
            "submission": {"files": ["files/submission/main.py"]},
            "alpha": {"value": 0.5},
            "fast": {"value": False},
            "weights": {"files": ["files/weights/a.bin", "files/weights/layers/b.bin"]},
            "note": {"value": "hello"},
        },
    }
    assert layout.document.endswith(b"}\n")
    assert violation(document, "submission") is None


def test_a_left_out_input_takes_its_default() -> None:
    layout = lay_out_runs(FIELDS, {}, {**COMPLETE, "language": SubmittedInput()}, UPLOADS)

    assert json.loads(layout.document)["inputs"]["language"] == {"value": "python"}


def test_a_left_out_number_takes_its_decimal_default_to_the_digit() -> None:
    ratio = Field(
        id="ratio",
        type=Type.NUMBER,
        label="ratio",
        default=Decimal("0.123456789012345678901234567891"),
    )

    layout = lay_out_runs((ratio,), {}, {}, {})

    assert exact_json.loads(layout.document)["inputs"]["ratio"] == {
        "value": Decimal("0.123456789012345678901234567891")
    }
    assert b'"value": 0.123456789012345678901234567891' in layout.document


THIRTY = "0.123456789012345678901234567891"
"""A bound and a value of 30 significant digits, past what a float holds."""


def test_a_number_given_as_its_digits_reaches_submission_json_to_the_digit() -> None:
    ratio = Field(id="ratio", type=Type.NUMBER, label="ratio", max=Decimal(THIRTY))

    layout = lay_out_runs((ratio,), {}, {"ratio": SubmittedInput(value=THIRTY)}, {})

    assert exact_json.loads(layout.document)["inputs"]["ratio"] == {"value": Decimal(THIRTY)}
    assert f'"value": {THIRTY}'.encode() in layout.document


def test_a_number_one_past_a_bound_of_thirty_digits_is_refused() -> None:
    ratio = Field(id="ratio", type=Type.NUMBER, label="ratio", max=Decimal(THIRTY))
    past = THIRTY[:-1] + "2"

    with pytest.raises(InvalidInputs) as refused:
        lay_out_runs((ratio,), {}, {"ratio": SubmittedInput(value=past)}, {})

    assert refused.value.extra["errors"] == [
        {"input": "ratio", "message": f"This must be at most {THIRTY}."}
    ]


def refusal(given: dict[str, SubmittedInput], uploads: dict[uuid.UUID, UploadedFile]) -> Any:
    with pytest.raises(InvalidInputs) as refused:
        lay_out_runs(FIELDS, {}, {**COMPLETE, **given}, {**UPLOADS, **uploads})
    return refused.value.extra["errors"]


EXTRA, BIG, TWIN, INSIDE, GIT = (uuid.uuid4() for _ in range(5))
MORE = {
    INSIDE: UploadedFile(INSIDE, "weights", "a.bin/b.bin", 1),
    GIT: UploadedFile(GIT, "weights", ".git/config", 1),
    EXTRA: UploadedFile(EXTRA, "submission", "other.py", 10),
    BIG: UploadedFile(BIG, "submission", "big.py", 1025),
    TWIN: UploadedFile(TWIN, "weights", "a.bin", 1),
}


@pytest.mark.parametrize(
    ("given", "input", "message"),
    [
        ({"language": SubmittedInput(value="rust")}, "language", "Choose one of python, cpp."),
        ({"language": SubmittedInput(value="java")}, "language", "Choose one of python, cpp."),
        ({"alpha": SubmittedInput(value="2")}, "alpha", "This must be at most 1."),
        ({"alpha": SubmittedInput(value="-0.5")}, "alpha", "This must be at least 0."),
        (
            {"alpha": SubmittedInput(value=True)},
            "alpha",
            "Give a number in plain digits, such as 2.5.",
        ),
        (
            {"alpha": SubmittedInput(value="NaN")},
            "alpha",
            "Give a number in plain digits, such as 2.5.",
        ),
        (
            {"alpha": SubmittedInput(value="1e3")},
            "alpha",
            "Give a number in plain digits, such as 2.5.",
        ),
        (
            {"alpha": SubmittedInput(value="0x10")},
            "alpha",
            "Give a number in plain digits, such as 2.5.",
        ),
        (
            {"alpha": SubmittedInput(value="")},
            "alpha",
            "Give a number in plain digits, such as 2.5.",
        ),
        (
            {"alpha": SubmittedInput(value=" 0.5")},
            "alpha",
            "Give a number in plain digits, such as 2.5.",
        ),
        ({"fast": SubmittedInput(value="yes")}, "fast", "This must be true or false."),
        ({"note": SubmittedInput()}, "note", "This input is required."),
        ({"note": SubmittedInput(value=True)}, "note", "This must be text."),
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
            "Two files have the same name. Rename one of them.",
        ),
        ({"weights": SubmittedInput(uploads=(W1, W1))}, "weights", "given more than once"),
        (
            {"weights": SubmittedInput(uploads=(W1, W2, TWIN))},
            "weights",
            "Two files have the same name. Rename one of them.",
        ),
        ({"ghost": SubmittedInput(value="1")}, "ghost", "The task has no such input."),
        (
            {"weights": SubmittedInput(uploads=(W2, INSIDE))},
            "weights",
            "a.bin is both a file and a folder. Rename one of them.",
        ),
        (
            {"weights": SubmittedInput(uploads=(GIT,))},
            "weights",
            "There is a problem with the file .git/config: A file cannot be called .git, since "
            "git keeps its own data there.",
        ),
    ],
    ids=[
        "enum-outside-the-tasks-options",
        "enum-the-task-narrowed-away",
        "above-max",
        "below-min",
        "boolean-for-number",
        "nan-for-number",
        "exponent-for-number",
        "hex-for-number",
        "empty-for-number",
        "spaced-number",
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
        "unknown-input",
        "file-and-folder-at-once",
        "git-folder",
    ],
)
def test_a_submission_that_does_not_fit_the_inputs_is_refused_naming_the_input(
    given: dict[str, SubmittedInput], input: str, message: str
) -> None:
    errors = refusal(given, MORE)

    assert [error["input"] for error in errors] == [input]
    assert message in errors[0]["message"]


def test_a_submission_that_would_break_the_contract_is_refused_at_its_input() -> None:
    oddly_named = Field(id="Note", type=Type.TEXT, label="Note")

    with pytest.raises(InvalidInputs) as refused:
        lay_out_runs((oddly_named,), {}, {"Note": SubmittedInput(value="hi")}, {})

    (error,) = refused.value.extra["errors"]
    assert error["input"] == ""
    assert error["message"].startswith("Unicon cannot grade this submission as it is (at inputs:")


def test_a_folders_files_together_are_within_its_size() -> None:
    heavy = uuid.uuid4()
    uploads = {heavy: UploadedFile(heavy, "weights", "c.bin", 1024 - 600 + 1)}

    errors = refusal({"weights": SubmittedInput(uploads=(W1, W2, heavy))}, uploads)

    assert errors == [
        {
            "input": "weights",
            "message": "The files of this input come to more than the 1024 bytes allowed. "
            "Make them smaller.",
        }
    ]


def test_every_input_that_does_not_fit_is_named_together() -> None:
    errors = refusal(
        {"alpha": SubmittedInput(value="5"), "note": SubmittedInput(), "ghost": SubmittedInput()},
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


ROUTE1, ROUTE3, CODE = (uuid.uuid4() for _ in range(3))
RUN_UPLOADS = {
    ROUTE1: UploadedFile(ROUTE1, "route", "route.txt", 10),
    ROUTE3: UploadedFile(ROUTE3, "route", "route.txt", 12),
    CODE: UploadedFile(CODE, "code", "main.py", 10),
}
RUN_FIELDS = (
    Field(id="code", type=Type.FILE, label="code"),
    Field(id="route", type=Type.FILE, label="route"),
    Field(id="guess", type=Type.NUMBER, label="guess"),
)
PARTS = {"route": 3, "guess": 2}
TOUR_RUNS = [
    Run(f"r{part}", {"input": f"parts/{part}.in", "route": Part(part)}, False) for part in (1, 2, 3)
]


def test_a_run_submission_lays_out_each_part_under_its_number() -> None:
    given = {
        "code": SubmittedInput(uploads=(CODE,)),
        "route": SubmittedInput(
            parts={1: SubmittedInput(uploads=(ROUTE1,)), 3: SubmittedInput(uploads=(ROUTE3,))}
        ),
        "guess": SubmittedInput(parts={2: SubmittedInput(value="7")}),
    }

    layout = lay_out_runs(RUN_FIELDS, PARTS, given, RUN_UPLOADS)

    assert layout.files == {
        "files/code/main.py": CODE,
        "files/route/1/route.txt": ROUTE1,
        "files/route/3/route.txt": ROUTE3,
    }
    document = json.loads(layout.document)
    assert document == {
        "schema_version": 6,
        "inputs": {
            "code": {"files": ["files/code/main.py"]},
            "route": {
                "parts": {
                    "1": {"files": ["files/route/1/route.txt"]},
                    "3": {"files": ["files/route/3/route.txt"]},
                }
            },
            "guess": {"parts": {"2": {"value": 7}}},
        },
    }
    assert violation(document, "submission") is None


def test_a_part_left_out_skips_the_runs_made_from_its_tuple() -> None:
    given = {
        "code": SubmittedInput(uploads=(CODE,)),
        "route": SubmittedInput(parts={1: SubmittedInput(uploads=(ROUTE1,))}),
        "guess": SubmittedInput(parts={2: SubmittedInput(value="7")}),
    }

    layout = lay_out_runs(RUN_FIELDS, PARTS, given, RUN_UPLOADS)

    assert skipped(TOUR_RUNS, layout.inputs) == {"r2", "r3"}


@pytest.mark.parametrize(
    ("given", "input_", "words"),
    [
        ({"route": SubmittedInput()}, "route", "Give at least one part"),
        ({"route": SubmittedInput(uploads=(ROUTE1,))}, "route", "part by part"),
        (
            {"route": SubmittedInput(parts={4: SubmittedInput(uploads=(ROUTE1,))})},
            "route",
            "no part 4",
        ),
        ({"route": SubmittedInput(parts={1: SubmittedInput(value="x")})}, "route", "Part 1:"),
        (
            {"code": SubmittedInput(parts={1: SubmittedInput(uploads=(CODE,))})},
            "code",
            "given once",
        ),
        (
            {"guess": SubmittedInput(parts={1: SubmittedInput(value="seven")})},
            "guess",
            "plain digits",
        ),
    ],
)
def test_a_run_submission_that_does_not_fit_is_refused_at_its_input(
    given: dict[str, SubmittedInput], input_: str, words: str
) -> None:
    complete = {
        "code": SubmittedInput(uploads=(CODE,)),
        "route": SubmittedInput(parts={1: SubmittedInput(uploads=(ROUTE1,))}),
        "guess": SubmittedInput(parts={2: SubmittedInput(value="7")}),
    }

    with pytest.raises(InvalidInputs) as raised:
        lay_out_runs(RUN_FIELDS, PARTS, {**complete, **given}, RUN_UPLOADS)

    errors = raised.value.extra["errors"]
    assert any(error["input"] == input_ and words in error["message"] for error in errors), errors
