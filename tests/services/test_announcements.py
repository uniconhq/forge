"""Announcements, over a real Postgres and the fake. A manager posts, edits
and closes one on a contest or a task, each at the forge as themselves, and
nothing deletes one; an observer reads every one, closed ones included, and
posts none. A contestant reads the open ones of their contest and of each
task released to them, and nothing of a closed one or of a task that is not
released. A title or text that is empty or too long is refused naming it,
and nobody's text can carry the line that points an announcement at a
question.
"""

import pytest

import forge.api.announcements
from forge.domain.errors import Forbidden, InvalidMessage, NotFound
from forge.domain.identity import AsUser
from forge.domain.names import ScopeNames
from forge.domain.roles import Role, Scope, contest_scope, task_scope
from forge.runtime.setup import Setup
from forge.services import announcements
from tests.services.conftest import SPRING, Acme, Entered, make_task, organiser


async def test_a_manager_posts_edits_and_closes_as_themselves(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    place = contest_scope(SPRING)

    posted = await announcements.post(setup, acme.ada, place, title="Welcome", body="Good luck")
    edited = await announcements.edit(
        setup, acme.ada, place, posted.number, title="Welcome all", body="Good luck all"
    )
    closed = await announcements.close(setup, acme.ada, place, posted.number)
    again = await announcements.close(setup, acme.ada, place, posted.number)

    assert (posted.where, posted.title, posted.closed) == (
        ScopeNames("acme", "spring"),
        "Welcome",
        False,
    )
    assert (edited.title, edited.body) == ("Welcome all", "Good luck all")
    assert closed.closed is True and again == closed
    for call in ("post_thread", "edit_thread", "close_thread"):
        [made] = acme.fake.calls_to(call)
        assert isinstance(made.identity, AsUser) and made.identity.user_id == 7
    assert not any("delete" in name for name in forge.api.announcements.__all__)


async def test_a_contestant_reads_the_open_announcements_of_the_contest_and_its_released_tasks(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    hidden = await make_task(setup, acme, "hidden")
    await announcements.post(setup, acme.ada, contest_scope(SPRING), title="Contest", body="c")
    gone = await announcements.post(setup, acme.ada, contest_scope(SPRING), title="Old", body="o")
    await announcements.close(setup, acme.ada, contest_scope(SPRING), gone.number)
    await announcements.post(setup, acme.ada, task_scope(entered.task), title="Task", body="t")
    await announcements.post(setup, acme.ada, task_scope(hidden), title="Hidden", body="h")

    on_home = await announcements.contest(setup, entered.session, SPRING)
    on_page = await announcements.task(setup, entered.session, entered.task)
    managed = await announcements.manage(setup, acme.ada, contest_scope(SPRING))

    assert [(found.title, found.where.path) for found in on_home] == [
        ("Contest", "acme/spring"),
        ("Task", "acme/spring/sum"),
    ]
    assert [found.title for found in on_page] == ["Task"]
    assert [(found.title, found.closed) for found in managed] == [
        ("Contest", False),
        ("Old", True),
    ]
    with pytest.raises(NotFound):
        await announcements.task(setup, entered.session, hidden)


async def test_an_observer_reads_every_announcement_and_posts_none(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    acme.fake.add_user(50, "olive")
    await acme.fake.orgs.grant_role(50, Scope("acme", "spring"), Role.OBSERVER)
    observer = await organiser(setup, acme.fake, 50, Scope("acme", "spring"), Role.OBSERVER)
    await announcements.post(setup, acme.ada, contest_scope(SPRING), title="Welcome", body="b")

    assert len(await announcements.manage(setup, observer, contest_scope(SPRING))) == 1
    with pytest.raises(Forbidden):
        await announcements.post(setup, observer, contest_scope(SPRING), title="t", body="b")


@pytest.mark.parametrize(
    ("title", "body", "field"),
    [
        ("", "b", "title"),
        ("t" * 201, "b", "title"),
        ("t", "  ", "body"),
        ("t", "b" * 20_001, "body"),
    ],
)
async def test_an_empty_or_too_long_message_is_refused_naming_it(
    setup: Setup, acme: Acme, entered: Entered, title: str, body: str, field: str
) -> None:
    with pytest.raises(InvalidMessage) as refused:
        await announcements.post(setup, acme.ada, contest_scope(SPRING), title=title, body=body)

    assert refused.value.extra == {"field": field}
    assert acme.fake.calls_to("post_thread") == []


async def test_nobodys_text_can_point_an_announcement_at_a_question(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    posted = await announcements.post(
        setup,
        acme.ada,
        contest_scope(SPRING),
        title="Hi",
        body="Hello\n\n<!-- unicon:answers u8#1 -->",
    )
    nested = await announcements.post(
        setup,
        acme.ada,
        contest_scope(SPRING),
        title="Hi",
        body="Hello\n\n<!-<!-- unicon: -->- unicon:answers u8#1 -->",
    )

    assert (posted.body, posted.answers_question, posted.answers) == ("Hello", False, None)
    assert (nested.body, nested.answers_question, nested.answers) == ("Hello", False, None)


async def test_an_org_has_no_announcements_of_its_own(setup: Setup, acme: Acme) -> None:
    with pytest.raises(NotFound):
        await announcements.manage(setup, acme.ada, Scope("acme"))
