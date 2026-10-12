"""A task's public files, served one by one by their path in the task to
whoever may read its statement: a visitor, for a released task of a contest
for everyone; a signed-in person, for a task whose page they read; and
whoever holds a role reaching the task, whether or not it is released. Each
is read as the task's latest publication holds it, never a draft saved
after it, except by whoever holds a role reaching the task, who reads it as
the task's head holds it, the draft the statement's preview draws; an upload
by its bytes; a path outside `public/`, or not there, is
no such file, and a file larger than a page is served is refused with a
refusal of its own.
"""

import hashlib

import pytest

from forge.domain.content import Edit, Uploaded
from forge.domain.errors import NotFound, PublicFileTooLarge
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId
from forge.domain.roles import Role, Scope
from forge.domain.uploads import pointer_text
from forge.runtime.setup import Setup
from forge.services import contest_home, contests, landing, publications, uploads
from forge.services.published import PUBLIC_MOST
from forge.testing import register_entrant
from tests.services.conftest import (
    ACME,
    RUNNING,
    SPRING,
    Acme,
    make_task,
    organiser,
    signed_in,
    write_contest,
)

LATER = RUNNING + "  - {{id: later, release_at: 2026-09-26T13:00:00Z}}\n"
FIGURE = b"\x89PNG\r\n\x1a\n a figure"
NOTES = b"Read the input with care.\n"
DATA = b"a dataset, pretend it is large\n" * 32


async def _save(setup: Setup, acme: Acme, task: TaskId, files: dict[str, bytes]) -> None:
    """ada's save of `files` over the task, published, confirmed, since a
    save asks first once the contest has started.
    """
    head = await acme.fake.content.list_files(PLATFORM, task)
    result = await publications.save(
        setup,
        acme.ada,
        task,
        {path: Edit(content, head.tokens.get(path)) for path, content in files.items()},
        confirm=True,
    )
    assert isinstance(result, publications.Published), result


@pytest.fixture
async def public(setup: Setup, acme: Acme, sum_task: TaskId) -> TaskId:
    """acme/spring for everyone and running, its task sum published with a
    figure and notes under `public/`, and a task `later` published with a
    figure of its own and released only later.
    """
    later = await make_task(setup, acme, "later")
    await write_contest(acme.fake, LATER.format(visibility="everyone"))
    await _save(setup, acme, sum_task, {"public/figure.png": FIGURE, "public/a/notes.txt": NOTES})
    await _save(setup, acme, later, {"public/figure.png": FIGURE})
    acme.fake.reset_calls()
    return sum_task


async def test_a_visitor_reads_a_public_file_of_a_released_task_of_a_contest_for_everyone(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    figure = await landing.public_file(setup, public, "public/figure.png")
    notes = await landing.public_file(setup, public, "public/a/notes.txt")

    assert (figure.path, figure.content) == ("public/figure.png", FIGURE)
    assert notes.content == NOTES
    assert {call.identity for call in acme.fake.calls} == {PLATFORM}


async def test_a_visitor_reads_nothing_of_a_task_not_released_or_a_contest_not_for_everyone(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    with pytest.raises(NotFound, match="no such task"):
        await landing.public_file(setup, TaskId("acme/spring/later"), "public/figure.png")

    await write_contest(acme.fake, LATER.format(visibility="signed-in"))
    with pytest.raises(NotFound, match="no such task"):
        await landing.public_file(setup, public, "public/figure.png")


async def test_a_signed_in_person_reads_what_they_read_the_page_of(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    await write_contest(acme.fake, LATER.format(visibility="signed-in"))
    bob = await signed_in(setup, acme.fake, 8)

    found = await contest_home.public_file(setup, bob, public, "public/figure.png")

    assert found.content == FIGURE
    with pytest.raises(NotFound, match="no such task"):
        await contest_home.public_file(setup, bob, TaskId("acme/spring/later"), "public/figure.png")


async def test_a_hidden_contests_files_are_its_contestants_and_nobody_elses(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    await write_contest(acme.fake, LATER.format(visibility="hidden"))
    acme.fake.add_user(20, "cyd")
    await register_entrant(setup, SPRING, 8)
    await register_entrant(setup, SPRING, 20, status="pending")
    bob, cyd = await signed_in(setup, acme.fake, 8), await signed_in(setup, acme.fake, 20)

    assert (await contest_home.public_file(setup, bob, public, "public/figure.png")).content == (
        FIGURE
    )
    with pytest.raises(NotFound, match="no such task"):
        await contest_home.public_file(setup, cyd, public, "public/figure.png")
    with pytest.raises(NotFound, match="no such task"):
        await landing.public_file(setup, public, "public/figure.png")


@pytest.mark.parametrize(
    "scope",
    [Scope("acme"), Scope("acme", "spring"), Scope("acme", "spring", "later")],
    ids=["org", "contest", "task"],
)
async def test_whoever_holds_a_role_reaching_the_task_reads_it_before_its_release(
    setup: Setup, acme: Acme, public: TaskId, scope: Scope
) -> None:
    await write_contest(acme.fake, LATER.format(visibility="hidden").replace("published", "draft"))
    acme.fake.add_user(50, "olive")
    await acme.fake.orgs.grant_role(50, scope, Role.OBSERVER)
    olive = await signed_in(setup, acme.fake, 50)

    found = await contest_home.public_file(
        setup, olive, TaskId("acme/spring/later"), "public/figure.png"
    )

    assert found.content == FIGURE


async def test_a_role_at_another_task_reads_nothing_unreleased(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    acme.fake.add_user(50, "olive")
    await acme.fake.orgs.grant_role(50, Scope("acme", "spring", "sum"), Role.MANAGER)
    olive = await signed_in(setup, acme.fake, 50)

    with pytest.raises(NotFound, match="no such task"):
        await contest_home.public_file(
            setup, olive, TaskId("acme/spring/later"), "public/figure.png"
        )


async def test_an_organiser_reads_a_task_never_published_as_its_head_holds_it(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    unsaved = await make_task(setup, acme, "unsaved")
    draft = await publications.save(
        setup, acme.ada, unsaved, {"public/figure.png": Edit(FIGURE)}, keep_as_draft=True
    )
    assert isinstance(draft, publications.Draft), draft
    ada = await signed_in(setup, acme.fake, 7)
    bob = await signed_in(setup, acme.fake, 8)

    found = await contest_home.public_file(setup, ada, unsaved, "public/figure.png")

    assert found.content == FIGURE
    with pytest.raises(NotFound, match="no such file"):
        await contest_home.public_file(setup, ada, unsaved, "public/missing.png")
    with pytest.raises(NotFound, match="no such task"):
        await contest_home.public_file(setup, bob, unsaved, "public/figure.png")


async def test_a_file_is_read_as_the_latest_publication_holds_it_and_as_the_head_for_organisers(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    head = await acme.fake.content.list_files(PLATFORM, public)
    draft = await publications.save(
        setup,
        acme.ada,
        public,
        {
            "public/figure.png": Edit(b"a new figure", head.tokens["public/figure.png"]),
            "public/new.png": Edit(b"new"),
        },
        keep_as_draft=True,
    )
    assert isinstance(draft, publications.Draft), draft
    ada = await signed_in(setup, acme.fake, 7)
    bob = await signed_in(setup, acme.fake, 8)

    assert (await landing.public_file(setup, public, "public/figure.png")).content == FIGURE
    assert (await contest_home.public_file(setup, bob, public, "public/figure.png")).content == (
        FIGURE
    )
    with pytest.raises(NotFound, match="no such file"):
        await landing.public_file(setup, public, "public/new.png")
    # The statement being written draws its pictures as the draft holds them.
    assert (await contest_home.public_file(setup, ada, public, "public/figure.png")).content == (
        b"a new figure"
    )
    assert (await contest_home.public_file(setup, ada, public, "public/new.png")).content == b"new"


async def test_what_a_publication_holds_is_listed_once_for_every_reader(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    acme.fake.reset_calls()

    await landing.public_file(setup, public, "public/figure.png")
    await landing.public_file(setup, public, "public/a/notes.txt")
    await contest_home.public_file(setup, bob, public, "public/figure.png")

    assert len(acme.fake.calls_to("list_files")) == 1


@pytest.mark.parametrize(
    "path",
    [
        "statement.md",
        "task.yaml",
        "plans/plan.json",
        "tests/main/1/input",
        "public/../task.yaml",
        "public/./figure.png",
        "public//figure.png",
        "public/",
        "public",
        "public/a",
        "public/missing.png",
        "/public/figure.png",
        "public\\figure.png",
        "",
    ],
)
async def test_a_path_outside_public_or_not_there_is_no_such_file(
    setup: Setup, acme: Acme, public: TaskId, path: str
) -> None:
    bob = await signed_in(setup, acme.fake, 8)
    ada = await signed_in(setup, acme.fake, 7)

    with pytest.raises(NotFound, match="no such file") as visitor:
        await landing.public_file(setup, public, path)
    with pytest.raises(NotFound, match="no such file"):
        await contest_home.public_file(setup, bob, public, path)
    with pytest.raises(NotFound, match="no such file"):
        await contest_home.public_file(setup, ada, public, path)

    assert visitor.value.detail == "There is no such file."
    assert acme.fake.calls_to("read_media") == []


async def test_an_upload_under_public_is_served_by_its_bytes(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    digest = hashlib.sha256(DATA).hexdigest()
    slot = await uploads.task_file_slot(
        setup, acme.ada, public, path="public/data.bin", size=len(DATA), sha256=digest
    )
    ada = await signed_in(setup, acme.fake, 7)
    door = await uploads.door(setup, ada, slot.id, length=len(DATA))
    acme.fake.uploads.send(door.path, door.authorization, DATA)
    await uploads.complete(setup, ada, public, slot.id)
    head = await acme.fake.content.list_files(PLATFORM, public)
    result = await publications.save(
        setup,
        acme.ada,
        public,
        {"public/data.bin": Edit(Uploaded(slot.id), head.tokens.get("public/data.bin"))},
        confirm=True,
    )
    assert isinstance(result, publications.Published), result
    stored = await acme.fake.content.read_file(PLATFORM, public, "public/data.bin")
    assert stored.content == pointer_text(digest, len(DATA))

    served = await landing.public_file(setup, public, "public/data.bin")

    assert served.content == DATA


async def test_a_file_larger_than_a_page_is_served_is_refused_with_its_limit(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    await _save(
        setup,
        acme,
        public,
        {"public/big.bin": b"x" * (PUBLIC_MOST + 1), "public/edge.bin": b"x" * PUBLIC_MOST},
    )

    with pytest.raises(PublicFileTooLarge) as refused:
        await landing.public_file(setup, public, "public/big.bin")

    assert refused.value.code == "public_file_too_large"
    assert refused.value.extra == {"limit": PUBLIC_MOST}
    assert "acme" not in refused.value.detail and "/" not in refused.value.detail
    assert len((await landing.public_file(setup, public, "public/edge.bin")).content) == PUBLIC_MOST


async def test_another_orgs_contest_is_no_such_task(
    setup: Setup, acme: Acme, public: TaskId
) -> None:
    manager = await organiser(setup, acme.fake, 7, Scope("acme"), Role.MANAGER)
    await contests.create(setup, manager, ACME, "autumn")
    bob = await signed_in(setup, acme.fake, 8)

    for task in (TaskId("acme/autumn/sum"), TaskId("acme/nowhere/sum"), TaskId("nowhere/x/y")):
        with pytest.raises(NotFound):
            await landing.public_file(setup, task, "public/figure.png")
        with pytest.raises(NotFound):
            await contest_home.public_file(setup, bob, task, "public/figure.png")
