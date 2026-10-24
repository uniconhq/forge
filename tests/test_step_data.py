"""What a run task's step is handed and hands back, without its door: the
upload list a step may send, what its `outputs.json` must be, an item's
key, the pipeline left once stored steps are taken out, result version 6
built from a fake set of outputs, a step's token, the name Forgejo keeps a
large file under, and the fake store's links.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from forge.adapters.objects.fake import FakeObjects
from forge.adapters.objects.s3 import lfs_object
from forge.domain import step_data
from forge.domain.clock import FakeClock
from forge.domain.contracts import violation
from forge.domain.errors import Forbidden, InvalidCallback, NotFound, Rejected
from forge.domain.grading import step_token
from forge.domain.plan_basics import RESULT_MAX
from forge.domain.run_plan import RunEntry, RunPlan
from forge.domain.step_data import (
    OUTPUTS_MAX,
    UPLOADS_MAX,
    Fault,
    Faulted,
    Found,
    Item,
    Upload,
    all_accepted,
    chain_of,
    is_step_name,
    item_key,
    program_alone,
    read_outputs,
    read_uploads,
    result_of,
    results_key,
    runs_file,
    step_order,
    unreported,
    without,
)
from forge.port.objects import Stored
from tests.test_run_plan import planned
from tests.test_run_task import (
    ISLANDS,
    ISLANDS_PATHS,
    MEAN,
    MEAN_MANIFEST,
    MEAN_PATHS,
    deployed,
    mirrored,
)

SHA = "0" * 64
GRADING = uuid.UUID("0199a2c1-6b7e-7c3a-9f10-5d2e4b8a6c31")


def _planned(task: str, paths: frozenset[str], **kw: Any) -> RunPlan:
    primitives = {
        "unicon/exec@v1": mirrored("exec"),
        "unicon/compile@v3": mirrored("compile"),
    }
    _, files = deployed("unicon/classic@v3")
    found = planned(primitives, task, files["workflow.yaml"].decode(), files, paths, **kw)
    assert found.plan is not None, found.problems
    return found.plan


@pytest.fixture(scope="module")
def islands() -> RunPlan:
    return _planned(ISLANDS, ISLANDS_PATHS)


@pytest.fixture(scope="module")
def mean() -> RunPlan:
    return _planned(MEAN, MEAN_PATHS, manifests={"programs/mean/program.yaml": MEAN_MANIFEST})


def _listed(*entries: tuple[str, int]) -> bytes:
    files = [{"path": path, "sha256": SHA, "size": size} for path, size in entries]
    return json.dumps({"files": files}).encode()


# Uploads


def test_a_step_lists_outputs_json_and_files_under_out() -> None:
    found = read_uploads(_listed(("outputs.json", 10), ("out/r1/output", 3)), item_mb=1, items=1)

    assert found == {"outputs.json": Upload(SHA, 10), "out/r1/output": Upload(SHA, 3)}
    assert results_key(GRADING, "solution", "out/r1/output") == (
        f"results/{GRADING}/steps/solution/out/r1/output"
    )


@pytest.mark.parametrize(
    ("body", "item_mb", "items"),
    [
        (b"not json", 1, 1),
        (b'{"files": {}}', 1, 1),
        (b'{"files": [], "more": 1}', 1, 1),
        (_listed(("out/x", 1)), 1, 1),
        (_listed(("outputs.json", 1), ("outputs.json", 1)), 1, 1),
        (_listed(("outputs.json", OUTPUTS_MAX + 1)), 1, 1),
        (_listed(("outputs.json", 1), ("out/x", 1024 * 1024 + 1)), 1, 1),
        (_listed(("outputs.json", 1), ("out/x", 1024 * 1024), ("out/y", 1024 * 1024)), 1, 1),
        (_listed(("outputs.json", 1), ("out/x", -1)), 1, 1),
        (_listed(("outputs.json", 1), *[(f"out/{n}", 0) for n in range(UPLOADS_MAX)]), 1, 1),
        (_listed(("outputs.json", 1), ("out/" + "x" * 1024, 0)), 1, 1),
        (_listed(("outputs.json", 1), ("out/" + "\u00e9" * 511, 0)), 1, 1),
        (_listed(("outputs.json", 1), ("out/\ud800", 0)), 1, 1),
        (
            json.dumps(
                {"files": [{"path": "outputs.json", "sha256": "A" * 64, "size": 1}]}
            ).encode(),
            1,
            1,
        ),
        (
            json.dumps({"files": [{"path": "outputs.json", "sha256": SHA, "size": True}]}).encode(),
            1,
            1,
        ),
    ],
)
def test_an_upload_list_out_of_bounds_is_refused(body: bytes, item_mb: int, items: int) -> None:
    with pytest.raises(InvalidCallback):
        read_uploads(body, item_mb=item_mb, items=items)


def test_a_step_name_is_a_step_id_alone_or_with_a_run() -> None:
    assert all(is_step_name(name) for name in ("compile", "solution.r1", "report", "a-b_c.R_2-x"))
    assert not any(
        is_step_name(name)
        for name in (
            "",
            "Compile",
            "-x",
            "a\x00b",
            "../x",
            "a.b.c",
            "a/b",
            "x" * 41,
            "a.r\n",
        )
    )


def test_a_batch_may_write_what_each_of_its_items_may() -> None:
    body = _listed(("outputs.json", 1), ("out/x", 1024 * 1024), ("out/y", 1024 * 1024))

    assert len(read_uploads(body, item_mb=1, items=2)) == 3


# outputs.json


def _batch(*runs: str, **outputs: Any) -> bytes:
    entries = [
        {"run": run, "outputs": {"outcome": "accepted", "memory_kb": 1, "time_ms": 1} | outputs}
        for run in runs
    ]
    return json.dumps({"schema_version": 6, "batch": entries}).encode()


def test_a_batch_answers_its_runs_in_order(islands: RunPlan) -> None:
    uploads = {"out/r1": Upload(SHA, 1), "out/r2": Upload(SHA, 2)}
    content = json.dumps(
        {
            "schema_version": 6,
            "batch": [
                {
                    "run": run,
                    "outputs": {
                        "outcome": "accepted",
                        "output": {"file": f"out/{run}"},
                        "memory_kb": 1,
                        "time_ms": 1,
                    },
                }
                for run in ("r1", "r2")
            ],
        }
    ).encode()

    found = read_outputs(content, islands.steps["solution"], ["r1", "r2"], True, uploads)

    assert [(item.run, item.files) for item in found] == [
        ("r1", {"out/r1": Upload(SHA, 1)}),
        ("r2", {"out/r2": Upload(SHA, 2)}),
    ]


@pytest.mark.parametrize(
    ("content", "fault"),
    [
        (b"{", "not JSON"),
        (b'{"schema_version": 5, "outputs": {"outcome": "accepted"}}', "6 was expected"),
        (b'{"schema_version": 6, "error": "No isolate."}', "The step failed: No isolate."),
        (b'{"schema_version": 6, "verdict": "accepted"}', "contract"),
        (_batch("r2", "r1", outcome="wrong_answer"), "in their order"),
        (_batch("r1", outcome="wrong_answer"), "in their order"),
        (_batch("r1", "r2", outcome="great"), "no outcome"),
        (_batch("r1", "r2", outcome="accepted"), "leave out output"),
        (_batch("r1", "r2", output={"file": "out/missing"}), "did not upload"),
        (_batch("r1", "r2", output={"file": "in/x"}), "not a file"),
        (_batch("r1", "r2", outcome="wrong_answer", score=1), "declares no output score"),
        (_batch("r1", "r2", outcome="wrong_answer", memory_kb="a lot"), "not a number"),
        (b'{"schema_version": 6, "outputs": {"outcome": "accepted"}}', "in their order"),
        (_batch("r1", "r2", outcome="wrong_answer", message="a \x00 byte"), "NUL"),
        (b'{"schema_version": 6, "outputs": {"outcome\\u0000": "accepted"}}', "NUL"),
        (b'{"schema_version": 6, "error": "a \\u0000 byte"}', "NUL"),
    ],
)
def test_an_outputs_json_that_is_not_the_steps_outputs_is_a_fault(
    islands: RunPlan, content: bytes, fault: str
) -> None:
    with pytest.raises(Fault, match=fault):
        read_outputs(content, islands.steps["solution"], ["r1", "r2"], True, {})


def test_a_file_an_output_names_must_be_one_a_job_can_place(islands: RunPlan) -> None:
    uploads = {
        path: Upload(SHA, 1)
        for path in ("out/r1", "out/my file", "out/extras/-x", "out/extras/ok", "out/a b")
    }

    def read(**outputs: Any) -> list[Any]:
        content = _batch("r1", outcome="wrong_answer", **outputs)
        return read_outputs(content, islands.steps["solution"], ["r1"], True, uploads)

    with pytest.raises(Fault, match="'out/my file', which a job cannot place"):
        read(output={"file": "out/my file"})
    with pytest.raises(Fault, match="'out/extras/-x', which a job cannot place"):
        read(extras={"folder": "out/extras"})
    [item] = read(output={"file": "out/r1"})
    assert item.files == {"out/r1": Upload(SHA, 1)}


# Keys and the pipeline left


def test_an_items_key_is_its_image_and_inputs_and_nothing_else() -> None:
    inputs = {"stdin": {"file": SHA}, "time_limit": {"value": 2}}

    assert item_key("image@1", inputs) == item_key("image@1", dict(reversed(inputs.items())))
    assert item_key("image@1", inputs) != item_key("image@2", inputs)
    assert item_key("image@1", inputs) != item_key("image@1", inputs | {"time_limit": 3})


def test_a_stored_step_is_left_out_and_what_waited_for_it_rewired(islands: RunPlan) -> None:
    early = islands.pipeline("early")

    left = without(early, ["solution"])
    assert [(held.name, held.depends_on) for held in left] == [
        ("compile", ()),
        ("compare", ("compile",)),
    ]
    assert [(held.name, held.depends_on) for held in without(early, ["compile"])] == [
        ("solution", ()),
        ("compare", ("solution",)),
    ]
    assert without(early, ["compile", "solution", "compare"]) == ()


# Result version 6


def _found(outcome: str = "accepted", **outputs: Any) -> Found:
    return Found(str(uuid.uuid4()), {"outcome": outcome, **outputs})


def _early(
    skip: tuple[str, ...] = (), **changes: Found | Faulted | None
) -> dict[Item, Found | Faulted]:
    found: dict[Item, Found | Faulted | None] = {
        ("diff", None): _found(program={"file": "out/program"}),
        ("compile", None): _found(log="built", program={"file": "out/program"}),
    }
    for run in ("r1", "r2"):
        if run in skip:
            continue
        found[("solution", run)] = _found(memory_kb=1500, time_ms=12, output={"file": "out/o"})
        found[("compare", run)] = _found(
            memory_kb=1, time_ms=1, score=1, output={"file": "out/o"}, message="Right."
        )
    for name, value in changes.items():
        step, _, run = name.partition("_")
        found[(step, run or None)] = value
    return {item: value for item, value in found.items() if value is not None}


def test_a_result_gives_each_run_its_verdict_and_reports(islands: RunPlan) -> None:
    found = _early(compare_r2=_found("wrong_answer"))

    result = result_of(islands, "early", frozenset(), found)

    assert violation(result, "result", "result_v6") is None
    assert [(run["id"], run["verdict"], run["passed"]) for run in result["runs"]] == [
        ("r1", "accepted", 1),
        ("r2", "wrong_answer", 0),
    ]
    assert result["runs"][0]["reports"] == {"memory_kb": 1500, "score": 1, "time_ms": 12}
    assert result["runs"][1]["reports"] == {"memory_kb": 1500, "time_ms": 12}
    log = found[("compile", None)]
    assert isinstance(log, Found)
    assert result["once"]["texts"] == {"log": {"output": log.id, "name": "log"}}
    assert result["error"] is None and result["run_log"] is None


def test_a_skipped_run_and_a_fault_are_verdicts(islands: RunPlan) -> None:
    found = _early(skip=("r1",), solution_r2=Faulted("The step solution faulted: no JSON."))

    result = result_of(islands, "early", frozenset({"r1"}), found)

    assert [(run["id"], run["verdict"]) for run in result["runs"]] == [
        ("r1", "skipped"),
        ("r2", "system_error"),
    ]
    assert result["error"] == "The step solution faulted: no JSON."
    assert violation(result, "result", "result_v6") is None


def test_a_step_that_gave_nothing_is_a_system_error_but_a_stop_before_it_is_not(
    islands: RunPlan,
) -> None:
    missing = result_of(islands, "early", frozenset(), _early(compare_r1=None))
    stopped = _early(compile=_found("compile_error", log="error"), solution_r1=None)

    assert missing["error"] == "The step compare gave nothing for the run r1."
    result = result_of(islands, "early", frozenset(), stopped)
    assert [run["verdict"] for run in result["runs"]] == ["compile_error", "compile_error"]
    # Which the compiled fact reads.
    assert result["once"]["outcomes"] == {"compile": "compile_error"}


def test_staff_texts_are_kept_once_per_item_this_pipeline_did(islands: RunPlan) -> None:
    found = _early(compare_r1=_found(score=0, memory_kb=1, time_ms=1, staff_message="Look."))

    result = result_of(islands, "early", frozenset(), found)

    [staff] = result["staff"]
    assert (staff["step"], staff["run"], staff["staff_message"]["name"]) == (
        "compare",
        "r1",
        "staff_message",
    )
    late = result_of(islands, "late", frozenset(), {})
    assert late["staff"] == []
    assert late["once"] == {"reports": {}, "texts": {}, "outcomes": {}}


def test_a_64_kb_text_on_a_thousand_runs_stays_under_the_cap(islands: RunPlan) -> None:
    runs = tuple(
        RunEntry(id=f"r{n}", key={"input": f"tests/{n}.in"}, early=True) for n in range(1000)
    )
    steps = {
        name: step.model_copy(
            update={"items": {run.id: next(iter((step.items or {}).values())) for run in runs}}
        )
        if not step.once
        else step
        for name, step in islands.steps.items()
    }
    plan = islands.model_copy(update={"runs": runs, "steps": steps})
    text = "x" * 64 * 1024
    found: dict[Item, Found | Faulted] = {
        ("diff", None): _found(),
        ("compile", None): _found(log=text, program={"file": "out/program"}),
    }
    for run in runs:
        found[("solution", run.id)] = _found(memory_kb=1, time_ms=1, label=text)
        found[("compare", run.id)] = _found(score=1, memory_kb=1, time_ms=1, message=text)

    result = result_of(plan, "early", frozenset(), found)

    assert len(json.dumps(result).encode()) < RESULT_MAX
    assert result["runs"][999]["message"]["name"] == "message"


# The token, the large-file store and the fake store


def test_a_steps_token_names_its_grading_attempt_and_step() -> None:
    key = b"\x01" * 32
    token = step_token(key, GRADING, 1, "compile")

    assert token == step_token(key, GRADING, 1, "compile")
    assert "=" not in token and len(token) == 43
    others = {
        step_token(key, uuid.uuid4(), 1, "compile"),
        step_token(key, GRADING, 2, "compile"),
        step_token(key, GRADING, 1, "solution"),
        step_token(b"\x02" * 32, GRADING, 1, "compile"),
    }
    assert token not in others and len(others) == 4


def test_a_large_file_is_named_as_forgejo_names_it() -> None:
    digest = hashlib.sha256(b"3\n").hexdigest()

    assert lfs_object(digest) == f"lfs/{digest[:2]}/{digest[2:4]}/{digest[4:]}"
    for wrong in ("../" + digest[3:], digest.upper(), digest[:-1], digest + "/x"):
        with pytest.raises(ValueError):
            lfs_object(wrong)


def test_the_fake_stores_links_allow_one_method_on_one_object_for_a_while() -> None:
    clock = FakeClock()
    store = FakeObjects(clock)
    put = store.put_url("results/a", expires_in=timedelta(minutes=15))
    store.put(put, b"a")
    store.objects["results/b"] = b"b"
    get = store.get_url("results/a", expires_in=timedelta(minutes=15))
    digest = hashlib.sha256(b"large").hexdigest()
    store.lfs[digest] = b"large"
    large = store.lfs_get_url(digest, expires_in=timedelta(minutes=15))

    assert store.get(get) == b"a" and store.get(large) == b"large"
    assert "/forgejo-lfs/lfs/" in large
    with pytest.raises(Forbidden):
        store.get(put)
    with pytest.raises(Forbidden):
        store.put(get, b"replaced")
    with pytest.raises(Forbidden):
        store.get(get.replace("results/a", "results/b").split("?")[0] + "?signature=wrong")
    with pytest.raises(NotFound):
        store.get(store.get_url("results/none", expires_in=timedelta(minutes=15)))
    clock.advance(timedelta(minutes=15, seconds=1))
    with pytest.raises(Forbidden):
        store.get(get)


async def test_the_fake_stores_put_signed_for_a_checksum_writes_only_those_bytes() -> None:
    store = FakeObjects(FakeClock())
    digest = hashlib.sha256(b"a").hexdigest()
    put = store.put_url("results/a", expires_in=timedelta(minutes=15), sha256=digest)

    with pytest.raises(Rejected):
        store.put(put, b"b")
    with pytest.raises(NotFound):
        await store.head("results/a")
    store.put(put, b"a")
    store.put(put, b"a")
    assert await store.head("results/a") == Stored(1, digest)
    store.put(store.put_url("results/a", expires_in=timedelta(minutes=15)), b"bb")
    assert await store.head("results/a") == Stored(2, None)


def _late(changes: Mapping[Item, Found | Faulted | None] = {}) -> dict[Item, Found | Faulted]:
    """Every run of the plan with a value's program graded, each accepted,
    the program reporting a mean of 0.5, but what `changes` gives."""
    found = _early()
    for run in ("r3", "r4"):
        found[("solution", run)] = _found(memory_kb=1500, time_ms=20, output={"file": "out/o"})
        found[("compare", run)] = _found(memory_kb=1, time_ms=1, score=0, output={"file": "out/o"})
    found[("exec-mean", None)] = _found(
        mean=Decimal("0.5"), memory_kb=1, time_ms=3, output={"file": "out/o"}
    )
    for item, value in changes.items():
        if value is None:
            found.pop(item, None)
        else:
            found[item] = value
    return found


def test_a_values_program_is_no_step_of_a_runs_and_its_number_goes_in_values(
    mean: RunPlan,
) -> None:
    order = step_order(mean, "late")

    assert "exec-mean" in order and "build-mean" in order
    assert chain_of(mean, order, "r3") == [
        ("diff", None),
        ("compile", None),
        ("solution", "r3"),
        ("compare", "r3"),
    ]
    result = result_of(mean, "late", frozenset(), _late())
    assert violation(result, "result", "result_v6") is None
    assert result["values"] == {"mean": Decimal("0.5")}
    assert [run["id"] for run in result["runs"]] == ["r3", "r4"]
    assert result["error"] is None
    assert "values" not in result_of(mean, "early", frozenset(), _early())


@pytest.mark.parametrize(
    ("program", "error"),
    [
        (Faulted("The step exec-mean faulted: no JSON."), "The step exec-mean faulted: no JSON."),
        (None, "The step exec-mean gave nothing."),
        (_found("runtime_error", memory_kb=1, time_ms=3), "The step exec-mean gave nothing."),
        (_found(memory_kb=1, time_ms=3), "The step exec-mean gave nothing."),
    ],
)
def test_a_program_that_faulted_or_gave_no_number_is_the_gradings_error_not_a_verdict(
    mean: RunPlan, program: Found | Faulted | None, error: str
) -> None:
    result = result_of(mean, "late", frozenset(), _late({("exec-mean", None): program}))

    assert "values" not in result
    assert result["error"] == error
    assert {run["verdict"] for run in result["runs"]} == {"accepted"}


def test_the_runs_file_holds_each_runs_id_key_verdict_passed_and_numbers_alone(
    mean: RunPlan,
) -> None:
    found = _late({("compare", "r2"): _found("wrong_answer", message="No.")})
    present = {item: got for item, got in found.items() if isinstance(got, Found)}

    written = runs_file(mean, "late", ["r1", "r2", "r3", "r4"], frozenset({"r4"}), present)

    assert written is not None
    assert json.loads(written) == {
        "runs": [
            {
                "id": "r1",
                "key": {"answer": "samples/1.ans", "input": "samples/1.in"},
                "passed": 1,
                "reports": {"memory_kb": 1500, "score": 1, "time_ms": 12},
                "verdict": "accepted",
            },
            {
                "id": "r2",
                "key": {"answer": "samples/2.ans", "input": "samples/2.in"},
                "passed": 0,
                "reports": {"memory_kb": 1500, "time_ms": 12},
                "verdict": "wrong_answer",
            },
            {
                "id": "r3",
                "key": {"answer": "tests/3.ans", "input": "tests/3.in"},
                "passed": 1,
                "reports": {"memory_kb": 1500, "score": 0, "time_ms": 20},
                "verdict": "accepted",
            },
            {
                "id": "r4",
                "key": {"answer": "tests/4.ans", "input": "tests/4.in"},
                "passed": 0,
                "reports": {},
                "verdict": "skipped",
            },
        ]
    }
    without_r3 = {item: got for item, got in present.items() if item != ("compare", "r3")}
    assert runs_file(mean, "late", ["r3"], frozenset(), without_r3) is None


def test_a_save_check_needs_every_step_done_once_accepted() -> None:
    runs = [{"verdict": "accepted"}, {"verdict": "skipped"}]

    assert all_accepted({"runs": runs, "once": {"outcomes": {"build-mean": "accepted"}}})
    assert not all_accepted({"runs": runs, "once": {"outcomes": {"build-mean": "compile_error"}}})
    assert not all_accepted({"runs": [{"verdict": "wrong_answer"}]})
    assert not all_accepted(None)


def test_a_number_too_long_for_the_runs_file_is_missing_to_the_program(
    mean: RunPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    huge = Decimal("1" * 200)
    found = _late({("compare", "r1"): _found(memory_kb=1, time_ms=1, score=huge)})
    present = {item: got for item, got in found.items() if isinstance(got, Found)}

    written = runs_file(mean, "late", ["r1", "r2"], frozenset(), present)

    assert written is not None
    first, second = json.loads(written)["runs"]
    # The entrant's huge number is left out; the run's other reports stay.
    assert first["reports"] == {"memory_kb": 1500, "time_ms": 12}
    assert second["reports"] == {"memory_kb": 1500, "score": 1, "time_ms": 12}
    monkeypatch.setattr(step_data, "MOST_RUN_ENTRY", 120)
    cut = runs_file(mean, "late", ["r1", "r2"], frozenset(), present)
    assert cut is not None
    assert [run["reports"] for run in json.loads(cut)["runs"]] == [{}, {}]
    assert [run["verdict"] for run in json.loads(cut)["runs"]] == ["accepted", "accepted"]


def test_a_fault_of_a_values_program_alone_is_told_apart_from_a_runs() -> None:
    accepted = {"runs": [{"verdict": "accepted"}, {"verdict": "wrong_answer"}], "error": "x"}

    assert program_alone(accepted)
    assert not program_alone({"runs": [{"verdict": "system_error"}], "error": "x"})
    assert unreported({**accepted, "values": {"a": 1}}, ["a", "b"]) == {"b"}
    assert unreported({"runs": [], "error": None}, ["a"]) == frozenset()
    assert unreported(None, ["a"]) == frozenset()
