import pytest

from forge.domain.content import Edit
from forge.domain.errors import Forbidden
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import TaskId
from forge.domain.workflow_definition import parse_workflow_ref
from forge.domain.workflows import Visibility
from forge.runtime.setup import Setup
from forge.services import task_save, workflows
from forge.services.task_save import Draft, Published
from forge.testing import CLASSIC
from tests.services.conftest import Acme, publish, signed_in


async def test_search_finds_other_peoples_public_readme_and_hides_private_ones(
    setup: Setup, acme: Acme
) -> None:
    ada = await signed_in(setup, acme.fake, 7)
    bob = await signed_in(setup, acme.fake, 8)
    as_ = AsUser(7, acme.fake.mint(7))
    for name, visibility in [("public", Visibility.PUBLIC), ("private", Visibility.PRIVATE)]:
        await acme.fake.workflows.create_workflow(
            as_,
            "ada",
            name,
            {**CLASSIC, "README.md": b"# Title\n\nFind **needle** here.\n"},
            visibility,
        )
        await workflows.create_version(setup, ada, "ada", name, "v1")
    page = await workflows.search(setup, bob, "needle")
    assert [(item.owner, item.name) for item in page.items] == [("ada", "public")]
    assert page.items[0].description == "Find needle here."
    assert not page.items[0].editable
    assert not (await workflows.search(setup, bob, "needle", scope="mine")).items
    assert [
        item.name for item in (await workflows.search(setup, ada, "needle", scope="mine")).items
    ] == ["private", "public"]
    assert [
        item.name for item in (await workflows.search(setup, bob, "needle", scope="public")).items
    ] == ["public"]


async def test_search_pages_empty_queries_and_short_filtered_word_pages(
    setup: Setup, acme: Acme
) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    for number in range(21):
        await workflows.create(setup, bob, "bob", f"find-{number}")
    first = await workflows.search(setup, bob, scope="mine")
    assert len(first.items) == 20 and first.next == 2
    second = await workflows.search(setup, bob, scope="mine", page=2)
    assert len(second.items) == 1 and second.next is None
    repo = acme.fake.state.repo("bob", "find-20.workflow")
    repo.marked = None
    short = await workflows.search(setup, bob, "find-", scope="mine")
    assert len(short.items) == 19 and short.next == 2


async def test_description_backfill_uses_newest_readme_and_keeps_description_without_one(
    setup: Setup, acme: Acme
) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    as_ = AsUser(8, acme.fake.mint(8))
    workflow = await acme.fake.workflows.create_workflow(
        as_, "bob", "tuned", CLASSIC, Visibility.PUBLIC
    )
    await workflows.create_version(setup, bob, "bob", "tuned", "v1")
    repo = acme.fake.state.repo("bob", "tuned.workflow")
    acme.fake.state.commit(repo, {"README.md": b"# New title\n\nNew **words**.\n"}, "README", 8)
    await acme.fake.workflows.create_workflow_version(as_, workflow, "v2")
    await workflows.describe_workflows(setup)
    assert (await acme.fake.workflows.describe_workflow(as_, workflow)).description == "New words."
    acme.fake.state.commit(repo, {"README.md": None}, "Remove README", 8)
    await workflows.create_version(setup, bob, "bob", "tuned", "v3")
    assert repo.description == "New words."


async def test_withdrawn_pin_keeps_published_task_and_refuses_new_use_then_undoes(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    ada = await signed_in(setup, acme.fake, 7)
    as_ = AsUser(7, acme.fake.mint(7))
    await acme.fake.workflows.create_workflow(as_, "acme", "tuned", CLASSIC, Visibility.PUBLIC)
    for version in ("v1", "v2"):
        await workflows.create_version(setup, ada, "acme", "tuned", version)
    old = await acme.fake.content.read_file(PLATFORM, sum_task, "task.yaml")
    await acme.fake.content.write_file(
        PLATFORM,
        sum_task,
        "task.yaml",
        old.content.replace(b"unicon/classic@v5", b"acme/tuned@v1"),
        expected=old.token,
        message="Choose workflow",
    )
    await publish(setup, acme, sum_task)
    await workflows.yank(setup, ada, "acme", "tuned", "v1", "Use the fixed checker")
    assert isinstance(await publish(setup, acme, sum_task), Published)
    form = await task_save.workflow_form(setup, acme.ada, sum_task)
    assert form.yanked is not None and form.yanked.reason == "Use the fixed checker"
    await workflows.yank(setup, ada, "acme", "tuned", "v2", "Also withdrawn")
    async with setup.unit_of_work() as ctx:
        assert await workflows.newer_version(ctx, as_, parse_workflow_ref("acme/tuned@v1")) is None
    before = await acme.fake.content.read_file(PLATFORM, sum_task, "task.yaml")
    answer = await task_save.save(
        setup,
        acme.ada,
        sum_task,
        {
            "task.yaml": Edit(
                before.content.replace(b"acme/tuned@v1", b"acme/tuned@v2"), before.token
            )
        },
        confirm=True,
    )
    assert isinstance(answer, Draft)
    assert any("Also withdrawn" in error["message"] for error in answer.errors)
    await workflows.unyank(setup, ada, "acme", "tuned", "v2")
    async with setup.unit_of_work() as ctx:
        assert await workflows.newer_version(ctx, as_, parse_workflow_ref("acme/tuned@v1")) == "v2"
    assert isinstance(await publish(setup, acme, sum_task), Published)
    bob = await signed_in(setup, acme.fake, 8)
    with pytest.raises(Forbidden):
        await workflows.yank(setup, bob, "acme", "tuned", "v2", "Not mine")
    page = await workflows.search(setup, ada, scope="orgs")
    selected = next(item for item in page.items if item.name == "tuned")
    assert selected.yanked["v1"].by == 7
    assert "v2" not in selected.yanked


async def test_an_uploaded_readme_supplies_version_and_backfill_descriptions(
    setup: Setup, acme: Acme
) -> None:
    import hashlib

    from forge.domain.content import Uploaded
    from forge.services import uploads

    bob = await signed_in(setup, acme.fake, 8)
    as_ = AsUser(8, acme.fake.mint(8))
    await acme.fake.workflows.create_workflow(as_, "bob", "readme", CLASSIC, Visibility.PUBLIC)
    content = b"# Uploaded\n\nFind **uploaded words** here.\n"
    slot = await workflows.file_slot(
        setup,
        bob,
        "bob",
        "readme",
        path="README.md",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    door = await uploads.door(setup, bob, slot.id, length=len(content))
    acme.fake.uploads.send(door.path, door.authorization, content)
    await workflows.complete_file(setup, bob, "bob", "readme", slot.id)
    page = await workflows.view(setup, bob, "bob", "readme")
    assert page.draft is not None
    await workflows.save(
        setup,
        bob,
        "bob",
        "readme",
        page.draft.text,
        page.draft.token,
        changes={
            "README.md": Edit(
                Uploaded(slot.id),
                (await workflows.read_file(setup, bob, "bob", "readme", "README.md")).token,
            )
        },
    )
    await workflows.create_version(setup, bob, "bob", "readme", "v1")
    repo = acme.fake.state.repo("bob", "readme.workflow")
    assert repo.description == "Find uploaded words here."
    repo.description = ""
    await workflows.describe_workflows(setup)
    assert repo.description == "Find uploaded words here."


async def test_a_renamed_withdrawn_workflow_keeps_the_same_publication_pin(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    from dataclasses import replace

    bob = await signed_in(setup, acme.fake, 8)
    as_ = AsUser(8, acme.fake.mint(8))
    await acme.fake.workflows.create_workflow(as_, "bob", "tuned", CLASSIC, Visibility.PUBLIC)
    await workflows.create_version(setup, bob, "bob", "tuned", "v1")
    old = await acme.fake.content.read_file(PLATFORM, sum_task, "task.yaml")
    await acme.fake.content.write_file(
        PLATFORM,
        sum_task,
        "task.yaml",
        old.content.replace(b"unicon/classic@v5", b"bob/tuned@v1"),
        expected=old.token,
        message="Choose workflow",
    )
    await publish(setup, acme, sum_task)
    await workflows.yank(setup, bob, "bob", "tuned", "v1", "Not for new tasks")
    repo = acme.fake.state.repos.pop(("bob", "tuned.workflow"))
    repo.owner = "robert"
    acme.fake.state.repos[("robert", "tuned.workflow")] = repo
    acme.fake.state.users[8] = replace(acme.fake.state.users[8], username="robert")
    current = await acme.fake.content.read_file(PLATFORM, sum_task, "task.yaml")
    result = await task_save.save(
        setup,
        acme.ada,
        sum_task,
        {
            "task.yaml": Edit(
                current.content.replace(b"bob/tuned@v1", b"robert/tuned@v1"), current.token
            )
        },
        confirm=True,
    )
    assert isinstance(result, Published), result


async def test_org_search_merges_the_same_per_org_pages_as_the_adapter(
    setup: Setup, acme: Acme
) -> None:
    from forge.services import orgs

    await orgs.create_by_operator(setup, "beta", description="Beta", admin_username="ada")
    ada = await signed_in(setup, acme.fake, 7)
    for number in range(30):
        await workflows.create(setup, ada, "acme", f"sample-{number}")
    await workflows.create(setup, ada, "beta", "sample-beta")
    pages = [
        await workflows.search(setup, ada, "sample", scope="orgs", page=page) for page in (1, 2, 3)
    ]
    assert [len(page.items) for page in pages] == [11, 10, 10]
    assert [page.next for page in pages] == [2, 3, None]
    assert len({(item.owner, item.name) for page in pages for item in page.items}) == 31


async def test_an_oversized_readme_leaves_the_description_without_an_unbounded_read(
    setup: Setup, acme: Acme
) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    as_ = AsUser(8, acme.fake.mint(8))
    await acme.fake.workflows.create_workflow(
        as_,
        "bob",
        "large",
        {**CLASSIC, "README.md": b"x" * (workflows.MAX_DEFINITION + 1)},
        Visibility.PUBLIC,
    )
    repo = acme.fake.state.repo("bob", "large.workflow")
    repo.description = "Kept description"
    await workflows.create_version(setup, bob, "bob", "large", "v1")
    assert repo.description == "Kept description"
    assert (
        acme.fake.calls_to("read_workflow_media")[-1].arguments["max_size"]
        == workflows.MAX_DEFINITION
    )
