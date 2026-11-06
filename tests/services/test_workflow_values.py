"""A workflow's draft read and saved as values: the comments of the file
kept, a stale token refused, an unreadable draft answered with its text,
values checked as a version would be, and a version read as values.
"""

import copy

import pytest

from forge.domain import settings_values
from forge.domain.errors import Conflict, Rejected
from forge.domain.sessions import Session
from forge.domain.settings_values import MAX_FILE
from forge.domain.workflow_definition import WORKFLOW_FILE
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import workflows
from forge.testing import CLASSIC
from tests.services.conftest import Acme, signed_in

WORKFLOW = CLASSIC[WORKFLOW_FILE].decode()


async def _own(setup: Setup, acme: Acme, text: str = WORKFLOW) -> Session:
    bob = await signed_in(setup, acme.fake, 8)
    await workflows.create(setup, bob, "bob", "tuned")
    repo = acme.fake.state.repos[("bob", "tuned.workflow")]
    acme.fake.state.commit(repo, {**CLASSIC, WORKFLOW_FILE: text.encode()}, "Write steps", 8)
    return bob


def _comments(text: str) -> list[str]:
    return sorted(line[line.index("#") :] for line in text.splitlines() if "#" in line)


COMMENTED = WORKFLOW.replace(
    "  input: {type: file, title: Input}\n",
    "  input: {type: file, title: Input}  # one test's input\n  # the answer to compare with\n",
).replace("  - id: compare\n", "  # Then the checker.\n  - id: compare  # by word\n")


async def test_a_commented_workflow_saved_as_values_keeps_every_comment(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme, COMMENTED)
    draft = await workflows.read_draft(setup, bob, "bob", "tuned")
    assert draft.values is not None and draft.unreadable is None
    values = copy.deepcopy(draft.values)
    values["inputs"] = {
        ("source" if key == "code" else key): value for key, value in values["inputs"].items()
    }
    values["steps"][0]["with"]["source"] = "${{ inputs.source }}"
    values["steps"][1]["with"]["time_limit"] = 5
    values["steps"].append({"id": "tidy", "use": "unicon/exec@v4"})
    values["outputs"]["label"] = "${{ steps.compare.label }}"

    saved = await workflows.save_values(setup, bob, "bob", "tuned", values, draft.token)

    written = acme.fake.state.repos[("bob", "tuned.workflow")].files[WORKFLOW_FILE].decode()
    assert saved.text == written
    assert saved.values == values
    assert saved.token != draft.token
    assert _comments(written) == _comments(COMMENTED)
    assert written.index("source:") < written.index("language:")
    assert "  - id: compare  # by word\n" in written


async def test_values_saved_over_a_stale_token_are_a_conflict(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)
    draft = await workflows.read_draft(setup, bob, "bob", "tuned")
    assert draft.values is not None
    await workflows.save(setup, bob, "bob", "tuned", draft.text + "\n", draft.token)

    with pytest.raises(Conflict, match="has changed since you opened it"):
        await workflows.save_values(setup, bob, "bob", "tuned", draft.values, draft.token)
    with pytest.raises(Conflict):
        await workflows.save_values(setup, bob, "bob", "tuned", draft.values, None)


async def test_an_unreadable_draft_answers_why_and_its_text(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme, "steps: [unclosed\n")

    draft = await workflows.read_draft(setup, bob, "bob", "tuned")

    assert draft.values is None
    assert draft.unreadable
    assert draft.text == "steps: [unclosed\n"
    with pytest.raises(InvalidDefinition) as refused:
        await workflows.save_values(setup, bob, "bob", "tuned", {"steps": []}, draft.token)
    assert [problem["path"] for problem in refused.value.errors] == [()]
    mended = await workflows.save(setup, bob, "bob", "tuned", "steps: []\n", draft.token)
    assert (mended.values, mended.unreadable) == ({"steps": []}, None)


@pytest.mark.parametrize("text", ["- a\n", "limit: .inf\nsteps: []\n"])
async def test_a_draft_that_is_not_values_is_unreadable(
    setup: Setup, acme: Acme, text: str
) -> None:
    bob = await _own(setup, acme, text)

    draft = await workflows.read_draft(setup, bob, "bob", "tuned")

    assert (draft.values, draft.text) == (None, text)
    assert draft.unreadable


async def test_a_new_workflows_draft_is_values(setup: Setup, acme: Acme) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    await workflows.create(setup, bob, "bob", "fresh")

    draft = await workflows.read_draft(setup, bob, "bob", "fresh")

    assert draft.values == {"steps": []}
    assert draft.token is not None


async def test_values_are_checked_as_a_version_would_be_at_their_paths(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    draft = await workflows.read_draft(setup, bob, "bob", "tuned")
    assert draft.values is not None

    fine = await workflows.check_values(setup, bob, draft.values)
    named = copy.deepcopy(draft.values)
    named["inputs"]["Bad"] = {"type": "number"}
    wired = copy.deepcopy(draft.values)
    wired["steps"][1]["with"]["files"]["output"] = "${{ steps.solver.output }}"

    assert fine == ()
    assert [p["path"] for p in await workflows.check_values(setup, bob, named)] == [
        ("inputs", "Bad")
    ]
    assert [p["path"] for p in await workflows.check_values(setup, bob, wired)] == [
        ("steps", 1, "with", "files", "output")
    ]


async def test_a_version_reads_as_its_text_and_values(setup: Setup, acme: Acme) -> None:
    bob = await _own(setup, acme)
    await workflows.create_version(setup, bob, "bob", "tuned", "v1")

    read = await workflows.read_version(setup, bob, "bob/tuned@v1")

    assert read.text == WORKFLOW
    assert read.unreadable is None
    assert read.values is not None
    assert read.values["inputs"]["code"] == {"type": "folder", "title": "Solution"}
    assert read.values["outputs"]["score"] == "${{ steps.compare.score }}"


async def test_values_over_the_most_a_workflow_holds_are_refused_before_any_write(
    setup: Setup, acme: Acme, monkeypatch: pytest.MonkeyPatch
) -> None:
    bob = await _own(setup, acme)
    draft = await workflows.read_draft(setup, bob, "bob", "tuned")
    big = {"description": "x" * MAX_FILE, "steps": []}

    def unwritten(*_: object) -> str:
        raise AssertionError("values over the limit were written")

    monkeypatch.setattr(settings_values, "write_values", unwritten)

    with pytest.raises(Rejected, match="at most 256 KB"):
        await workflows.check_values(setup, bob, big)
    with pytest.raises(Rejected, match="at most 256 KB"):
        await workflows.save_values(setup, bob, "bob", "tuned", big, draft.token)
