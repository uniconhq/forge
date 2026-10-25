import pytest

from forge.domain.content import Edit, Uploaded
from forge.domain.errors import Conflict, Forbidden, InvalidInputs, NotFound, UploadNotReady
from forge.domain.ids import WorkflowId
from forge.domain.uploads import UploadStatus
from forge.runtime.setup import Setup
from forge.services import uploads, workflows
from tests.services.conftest import Acme, signed_in
from tests.services.test_workflow_editing import _own


async def test_files_are_uploaded_saved_together_and_frozen(setup: Setup, acme: Acme) -> None:
    import hashlib

    bob = await _own(setup, acme)
    page = await workflows.view(setup, bob, "bob", "tuned")
    assert page.draft is not None
    content = b"print(42)\n"
    slot = await workflows.file_slot(
        setup,
        bob,
        "bob",
        "tuned",
        path="scorer/main.py",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    with pytest.raises(UploadNotReady):
        await workflows.complete_file(setup, bob, "bob", "tuned", slot.id)
    door = await uploads.door(setup, bob, slot.id, length=len(content))
    assert "/bob/tuned.workflow.git/" in door.path
    acme.fake.uploads.send(door.path, door.authorization, content)
    assert (
        await workflows.complete_file(setup, bob, "bob", "tuned", slot.id)
    ).status == UploadStatus.VERIFIED
    saved = await workflows.save(
        setup,
        bob,
        "bob",
        "tuned",
        page.draft.text,
        page.draft.token,
        changes={"scorer/main.py": Edit(Uploaded(slot.id))},
    )
    entries = await workflows.files(setup, bob, "bob", "tuned", "scorer")
    assert [(e.path, e.upload.size if e.upload else None) for e in entries] == [
        ("scorer/main.py", len(content))
    ]
    await workflows.create_version(
        setup, bob, "bob", "tuned", "v1", saved.token, revision=saved.revision
    )
    file = await workflows.read_file(setup, bob, "bob", "tuned", "scorer/main.py")
    await workflows.save(
        setup,
        bob,
        "bob",
        "tuned",
        saved.text,
        saved.token,
        changes={"scorer/main.py": Edit(None, file.token)},
    )
    assert await workflows.files(setup, bob, "bob", "tuned", "scorer") == ()
    repo = acme.fake.state.repos[("bob", "tuned.workflow")]
    assert "scorer/main.py" in acme.fake.state.files_at(repo, repo.versions["v1"])
    with pytest.raises(Conflict):
        await workflows.create_version(
            setup, bob, "bob", "tuned", "v2", saved.token, revision=saved.revision
        )


async def test_file_changes_conflict_atomically_and_need_write_access(
    setup: Setup, acme: Acme
) -> None:
    bob = await _own(setup, acme)
    draft = (await workflows.view(setup, bob, "bob", "tuned")).draft
    assert draft is not None
    saved = await workflows.save(
        setup,
        bob,
        "bob",
        "tuned",
        draft.text,
        draft.token,
        changes={"scorer/main.py": Edit(b"first")},
    )
    with pytest.raises(Conflict):
        await workflows.save(
            setup,
            bob,
            "bob",
            "tuned",
            saved.text + "# change\n",
            saved.token,
            changes={"scorer/main.py": Edit(b"second")},
        )
    assert (await workflows.view(setup, bob, "bob", "tuned")).draft == saved
    with pytest.raises(InvalidInputs):
        await workflows.save(
            setup,
            bob,
            "bob",
            "tuned",
            saved.text,
            saved.token,
            changes={"scorer/main.py": Edit(None)},
        )
    ada = await signed_in(setup, acme.fake, 7)
    with pytest.raises(Forbidden):
        await workflows.file_slot(setup, ada, "bob", "tuned", path="x", size=1, sha256="0" * 64)
    with pytest.raises(NotFound):
        await workflows.files(setup, ada, "bob", "tuned", "")


async def test_uploaded_manifests_are_checked_as_bytes(setup: Setup, acme: Acme) -> None:
    from forge.domain.workflow_definition import WORKFLOW_FILE
    from forge.port.uploads import WorkflowPlace
    from forge.testing import CLASSIC

    bob = await _own(setup, acme)
    draft = (await workflows.view(setup, bob, "bob", "tuned")).draft
    assert draft is not None
    place = WorkflowPlace(WorkflowId("bob/tuned"))
    changes = {}
    for path, content in CLASSIC.items():
        if path == WORKFLOW_FILE:
            continue
        digest, size = acme.fake.uploads.put(place, content)
        slot = await workflows.file_slot(
            setup, bob, "bob", "tuned", path=path, size=size, sha256=digest
        )
        assert slot.ready
        try:
            current = await workflows.read_file(setup, bob, "bob", "tuned", path)
            token = current.token
        except NotFound:
            token = None
        changes[path] = Edit(Uploaded(slot.id), token)
    saved = await workflows.save(
        setup,
        bob,
        "bob",
        "tuned",
        CLASSIC[WORKFLOW_FILE].decode(),
        draft.token,
        changes=changes,
    )
    assert (
        await workflows.create_version(
            setup, bob, "bob", "tuned", "v1", saved.token, revision=saved.revision
        )
        == "v1"
    )


@pytest.mark.parametrize("path", ["../x", ".git/config", "scorer/.gitattributes", "workflow.yaml"])
async def test_reserved_file_paths_are_refused(setup: Setup, acme: Acme, path: str) -> None:
    from forge.domain.errors import InvalidPath

    bob = await _own(setup, acme)
    with pytest.raises((InvalidInputs, InvalidPath)):
        await workflows.file_slot(setup, bob, "bob", "tuned", path=path, size=1, sha256="0" * 64)


async def test_an_upload_is_for_one_person_workflow_and_path(setup: Setup, acme: Acme) -> None:
    from forge.port.uploads import WorkflowPlace

    bob = await _own(setup, acme)
    digest, size = acme.fake.uploads.put(WorkflowPlace(WorkflowId("bob/tuned")), b"hello")
    slot = await workflows.file_slot(setup, bob, "bob", "tuned", path="x", size=size, sha256=digest)
    await workflows.create(setup, bob, "bob", "other")
    with pytest.raises(NotFound):
        await workflows.complete_file(setup, bob, "bob", "other", slot.id)
    ada = await signed_in(setup, acme.fake, 7)
    with pytest.raises(NotFound):
        await workflows.complete_file(setup, ada, "bob", "tuned", slot.id)
    draft = (await workflows.view(setup, bob, "bob", "tuned")).draft
    assert draft is not None
    with pytest.raises(InvalidInputs):
        await workflows.save(
            setup,
            bob,
            "bob",
            "tuned",
            draft.text,
            draft.token,
            changes={"y": Edit(Uploaded(slot.id))},
        )
    await workflows.save(
        setup, bob, "bob", "tuned", draft.text, draft.token, changes={"x": Edit(Uploaded(slot.id))}
    )
