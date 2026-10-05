"""Clarifications, over a real Postgres and the fake. An approved contestant
asks from their own workspace, which their first question makes, and reads
it back; the organisers see it in the inbox and another contestant never
does. A reply leaves it open; marking it answered closes it and takes it
out of the inbox, with or without a reply, and doing so twice changes
nothing; unmarking opens it again. A contestant's follow-up on an answered
question opens it again and brings it back to the inbox. The inbox is the
org's open questions in one search, for anyone holding a role in the org.
An answer made public is an announcement every contestant reads, pointing
at the question, which stays private.
"""

from dataclasses import replace

import pytest

from forge.domain.errors import Forbidden, NotApproved, NotFound
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.names import UserOwner
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.domain.threads import ThreadKind
from forge.runtime.setup import Setup
from forge.services import announcements, clarifications, contestants
from tests.services.conftest import SPRING, Acme, Entered, make_task, organiser, signed_in


async def _ask(setup: Setup, entered: Entered, title: str = "Input size?") -> int:
    asked = await clarifications.ask(
        setup, entered.session, SPRING, title=title, body="How big is n?", task="sum"
    )
    return asked.number


async def test_a_contestant_asks_from_their_own_workspace_and_reads_it_back(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    number = await _ask(setup, entered)

    [mine] = await clarifications.mine(setup, entered.session, SPRING)
    [inbox] = await clarifications.inbox(setup, await signed_in(setup, acme.fake, 7), "acme")

    assert (mine.number, mine.title, mine.body, mine.task) == (
        number,
        "Input size?",
        "How big is n?",
        "sum",
    )
    assert (mine.contest.path, mine.asker, mine.answered, mine.closed) == (
        "acme/spring",
        "8",
        False,
        False,
    )
    assert inbox == mine
    [asked] = acme.fake.calls_to("post_thread")
    assert isinstance(asked.identity, AsUser) and asked.identity.user_id == 8
    assert len(acme.fake.calls_to("open_workspace")) == 1


async def test_another_contestant_never_sees_a_question(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    number = await _ask(setup, entered)
    acme.fake.add_user(50, "carol")
    carol = await signed_in(setup, acme.fake, 50)
    await contestants.register(setup, carol, SPRING)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await contestants.approve(setup, manager, SPRING, 50)

    assert await clarifications.mine(setup, carol, SPRING) == ()
    with pytest.raises(NotFound):
        await clarifications.follow_up(setup, carol, SPRING, number, body="peek")
    with pytest.raises(Forbidden):
        await clarifications.inbox(setup, carol, "acme")


async def test_a_reply_leaves_it_open_and_marking_closes_it_once(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    number = await _ask(setup, entered)
    ada = await signed_in(setup, acme.fake, 7)

    replied = await clarifications.reply(setup, acme.ada, SPRING, "8", number, body="Up to 10^5.")
    assert (replied.answered, replied.closed) == (False, False)
    assert [(message.from_asker, message.body) for message in replied.messages] == [
        (False, "Up to 10^5.")
    ]
    assert len(await clarifications.inbox(setup, ada, "acme")) == 1

    marked = await clarifications.mark(setup, acme.ada, SPRING, "8", number)
    again = await clarifications.mark(setup, acme.ada, SPRING, "8", number)
    assert (marked.answered, marked.closed) == (True, True)
    assert again == marked
    assert len(acme.fake.calls_to("mark_answered")) == 1
    assert await clarifications.inbox(setup, ada, "acme") == ()

    unmarked = await clarifications.unmark(setup, acme.ada, SPRING, "8", number)
    assert (unmarked.answered, unmarked.closed) == (False, False)
    assert len(await clarifications.inbox(setup, ada, "acme")) == 1


async def test_marking_needs_no_reply_and_a_manager(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    number = await _ask(setup, entered)
    acme.fake.add_user(50, "olive")
    await acme.fake.orgs.grant_role(50, Scope("acme", "spring"), Role.OBSERVER)
    observer = await organiser(setup, acme.fake, 50, Scope("acme", "spring"), Role.OBSERVER)

    with pytest.raises(Forbidden):
        await clarifications.mark(setup, observer, SPRING, "8", number)
    marked = await clarifications.mark(setup, acme.ada, SPRING, "8", number)

    assert (marked.answered, marked.messages) == (True, ())
    assert len(await clarifications.of_contest(setup, observer, SPRING)) == 1


async def test_a_follow_up_on_an_answered_question_opens_it_again(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    number = await _ask(setup, entered)
    await clarifications.reply(setup, acme.ada, SPRING, "8", number, body="Yes.")
    await clarifications.mark(setup, acme.ada, SPRING, "8", number)

    followed = await clarifications.follow_up(setup, entered.session, SPRING, number, body="And m?")

    assert (followed.answered, followed.closed) == (False, False)
    assert [(message.from_asker, message.body) for message in followed.messages] == [
        (False, "Yes."),
        (True, "And m?"),
    ]
    [back] = await clarifications.inbox(setup, await signed_in(setup, acme.fake, 7), "acme")
    assert back.number == number


async def test_an_answer_made_public_points_at_the_question_which_stays_private(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    number = await _ask(setup, entered)

    public = await clarifications.answer_publicly(
        setup, acme.ada, SPRING, "8", number, title="On n", body="n is at most 10^5."
    )

    assert (public.where.path, public.answers_question) == ("acme/spring/sum", True)
    assert public.answers is not None and (public.answers.asker, public.answers.number) == (
        "8",
        number,
    )
    [read] = await announcements.task(setup, entered.session, entered.task)
    assert (read.title, read.answers_question, read.answers) == ("On n", True, None)
    assert len(await clarifications.mine(setup, entered.session, SPRING)) == 1


async def test_only_an_approved_contestant_asks_and_only_about_a_released_task(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await make_task(setup, acme, "hidden")
    acme.fake.add_user(50, "carol")
    carol = await signed_in(setup, acme.fake, 50)

    with pytest.raises(NotApproved):
        await clarifications.ask(setup, carol, SPRING, title="t", body="b")
    with pytest.raises(NotFound):
        await clarifications.ask(setup, entered.session, SPRING, title="t", body="b", task="hidden")
    assert acme.fake.calls_to("post_thread") == []


async def test_a_question_on_one_contest_shows_where_it_was_asked_in_the_inbox(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _ask(setup, entered)
    observer_session: Session = await signed_in(setup, acme.fake, 7)

    [inbox] = await clarifications.inbox(setup, observer_session, "acme")

    assert inbox.contest.path == "acme/spring"


async def test_the_asker_only_reads_their_desk_so_the_label_is_not_theirs_to_change(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    number = await _ask(setup, entered)
    [asked] = acme.fake.calls_to("post_thread")
    workspace = acme.fake.workspaces.workspace_of(SPRING, UserOwner(8))
    thread = acme.fake.threads.thread_of(workspace, number)

    with pytest.raises(Forbidden):
        await acme.fake.threads.mark_answered(asked.identity, thread)


async def test_a_task_the_asker_writes_into_their_question_at_the_forge_is_not_trusted(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    number = await _ask(setup, entered)
    workspace = acme.fake.workspaces.workspace_of(SPRING, UserOwner(8))
    thread = acme.fake.threads.thread_of(workspace, number)
    acme.fake.state.threads[thread] = replace(
        acme.fake.state.threads[thread],
        body="Why?\n\n<!-- unicon:task other/autumn/secret -->",
    )

    [question] = await clarifications.inbox(setup, await signed_in(setup, acme.fake, 7), "acme")
    public = await clarifications.answer_publicly(
        setup, acme.ada, SPRING, "8", number, title="Sizes", body="n is at most 10."
    )

    assert question.task is None
    assert public.where.task is None


async def test_a_question_label_put_on_a_contest_by_hand_leaves_every_list_working(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    number = await _ask(setup, entered)
    await acme.fake.threads.post_thread(
        PLATFORM, SPRING, ThreadKind.CLARIFICATION, title="Stray", body="Not a question."
    )

    inbox = await clarifications.inbox(setup, await signed_in(setup, acme.fake, 7), "acme")
    of_contest = await clarifications.of_contest(setup, acme.ada, SPRING)

    assert [question.number for question in inbox] == [number]
    assert [question.number for question in of_contest] == [number]
