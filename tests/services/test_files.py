"""A contest's or a task's files, read and written as the organiser. Reading
needs observer and writing manager, both made as the organiser's own
identity; a stale token is a conflict; `contest.yaml` is refused whole when
it is not valid, when an entry names no task of the contest (C1), or when
it moves a task's timeline behind what rows already did or drops the entry of a task that
opened or has submissions (C2), a contest never published moving freely,
and a manager's change to an admin-only key is refused naming it, with
nothing written either way; a task's write is its save; and a rollback
writes the old content as a new change.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from forge.domain.clock import FakeClock
from forge.domain.content import ConflictToken, Edit, start_of
from forge.domain.definitions import parse_contest
from forge.domain.errors import (
    AdminOnly,
    ConfirmationRequired,
    Conflict,
    Forbidden,
    InvalidPath,
    NotFound,
)
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId, VersionId
from forge.domain.roles import Role, Scope
from forge.domain.submissions import SubmittedInput
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import files, submissions, taking_out, task_save, timelines, uploads
from forge.services.access import Organiser
from forge.services.task_save import Published
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    Entered,
    checked,
    make_task,
    organiser,
    publish,
    upload,
    write_contest,
)


@pytest.fixture
async def manager(setup: Setup, acme: Acme, spring: ContestId) -> Organiser:
    """bob, a manager of the contest acme/spring."""
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring"), Role.MANAGER)
    found = await organiser(setup, acme.fake, 8, Scope("acme", "spring"), Role.MANAGER)
    acme.fake.reset_calls()
    return found


async def test_the_history_names_an_author_who_no_longer_holds_a_role(
    setup: Setup, acme: Acme, spring: ContestId, manager: Organiser
) -> None:
    before = await files.read(setup, manager, spring, "contest.yaml")
    changed = before.content.replace(b"end: 2026-10-03T17:00:00Z", b"end: 2026-10-03T18:00:00Z")
    await files.write(setup, manager, spring, "contest.yaml", changed, before.token)
    await acme.fake.orgs.revoke_role(8, Scope("acme", "spring"), Role.MANAGER)
    bob = acme.fake.state.users[8].username

    latest, *_ = await files.history(setup, acme.ada, spring, "contest.yaml")
    await acme.fake.orgs.grant_role(9, Scope("acme", "spring"), Role.OBSERVER)
    observer = await organiser(setup, acme.fake, 9, Scope("acme", "spring"), Role.OBSERVER)
    seen, *_ = await files.history(setup, observer, spring, "contest.yaml")

    assert (latest.author_id, latest.author) == (8, bob)
    assert seen.author == bob
    assert acme.fake.calls_to("find_user") == []


async def test_the_history_leaves_an_author_the_forge_does_not_know_unnamed(
    setup: Setup, acme: Acme, spring: ContestId, manager: Organiser
) -> None:
    before = await files.read(setup, manager, spring, "contest.yaml")
    changed = before.content.replace(b"end: 2026-10-03T17:00:00Z", b"end: 2026-10-03T18:00:00Z")
    await files.write(setup, manager, spring, "contest.yaml", changed, before.token)
    del acme.fake.state.users[8]

    latest, *rest = await files.history(setup, acme.ada, spring, "contest.yaml")

    assert (latest.author_id, latest.author) == (8, None)
    assert all(change.author is not None for change in rest if change.author_id is not None)


async def test_an_observer_reads_the_file_the_tree_and_the_history_as_themself(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring", "sum"), Role.OBSERVER)
    bob = await organiser(setup, acme.fake, 8, Scope("acme", "spring", "sum"))

    found = await files.read(setup, bob, sum_task, "task.yaml")
    listed = await files.tree(setup, bob, sum_task)
    folder = await files.tree(setup, bob, sum_task, "tests")
    changes = await files.history(setup, bob, sum_task, "statement.md")

    assert found.content.startswith(b"# The task's settings")
    assert [entry.path for entry in listed] == ["statement.md", "task.yaml", "tests"]
    assert [entry.path for entry in folder] == ["tests/1.ans", "tests/1.in"]
    assert [change.message for change in changes] == ["Create"]
    assert {
        call.identity
        for call in acme.fake.calls
        if call.operation in {"read_file", "list_tree", "history"}
    } == {bob.identity}
    with pytest.raises(Forbidden, match="manager role at acme/spring/sum"):
        await files.write(setup, bob, sum_task, "notes.md", b"x", None)


async def test_someone_outside_the_place_reads_nothing(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    await acme.fake.orgs.grant_role(8, Scope("acme", "autumn"), Role.ADMIN)
    bob = await organiser(setup, acme.fake, 8, Scope("acme", "autumn"))

    with pytest.raises(Forbidden, match="observer role at acme/spring"):
        await files.read(setup, bob, spring, "contest.yaml")
    with pytest.raises(NotFound):
        await files.read(setup, bob, ContestId("acme"), "contest.yaml")
    assert acme.fake.calls_to("read_file") == []


async def test_a_manager_writes_a_manager_key_and_a_stale_token_is_a_conflict(
    setup: Setup, acme: Acme, spring: ContestId, manager: Organiser
) -> None:
    before = await files.read(setup, manager, spring, "contest.yaml")
    changed = before.content.replace(b"end: 2026-10-03T17:00:00Z", b"end: 2026-10-03T18:00:00Z")
    assert changed != before.content

    written = await files.write(setup, manager, spring, "contest.yaml", changed, before.token)

    assert isinstance(written, files.Written)
    assert isinstance(written.version, str)
    (call,) = acme.fake.calls_to("write_file")
    assert (call.identity, call.arguments["author"]) == (PLATFORM, manager.user.id)
    after = await files.read(setup, manager, spring, "contest.yaml")
    assert parse_contest(after.content).end.hour == 18
    assert (await files.history(setup, manager, spring, "contest.yaml"))[0].author_id == 8
    with pytest.raises(Conflict):
        await files.write(setup, manager, spring, "contest.yaml", changed, before.token)


@pytest.mark.parametrize(
    ("old", "new", "keys"),
    [
        (b'name: "Spring 2026"', b'name: "Mine"', ["name"]),
        (b"state: draft", b"state: published", ["state"]),
        (
            b"visibility: signed-in",
            b"visibility: signed-in\nregistration: {ways: [{invite: {}}]}",
            ["registration"],
        ),
        (b"visibility: signed-in", b"visibility: everyone", ["visibility"]),
    ],
)
async def test_a_managers_change_to_an_admin_only_key_is_refused_and_an_admins_is_not(
    setup: Setup,
    acme: Acme,
    spring: ContestId,
    manager: Organiser,
    old: bytes,
    new: bytes,
    keys: list[str],
) -> None:
    before = await files.read(setup, manager, spring, "contest.yaml")
    changed = before.content.replace(old, new)
    assert changed != before.content

    with pytest.raises(AdminOnly) as refused:
        await files.write(setup, manager, spring, "contest.yaml", changed, before.token)

    assert refused.value.code == "admin_only"
    assert refused.value.extra["keys"] == keys
    assert acme.fake.calls_to("write_file") == []
    await files.write(setup, acme.ada, spring, "contest.yaml", changed, before.token)
    assert len(acme.fake.calls_to("write_file")) == 1


async def test_an_invalid_contest_yaml_is_refused_whole_naming_each_path(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    before = await files.read(setup, acme.ada, spring, "contest.yaml")
    broken = before.content.replace(b"state: draft", b"state: open") + b"team_size: 1\n"

    with pytest.raises(InvalidDefinition) as refused:
        await files.write(setup, acme.ada, spring, "contest.yaml", broken, before.token)

    assert {problem["path"] for problem in refused.value.errors} == {"state", "team_size"}
    assert acme.fake.calls_to("write_file") == []


async def test_a_rollback_writes_the_old_content_as_a_new_change(
    setup: Setup, acme: Acme, spring: ContestId
) -> None:
    first = await files.read(setup, acme.ada, spring, "contest.yaml")
    (created,) = await files.history(setup, acme.ada, spring, "contest.yaml")
    edited = first.content + b"entrants: {most: 5}\n"
    await files.write(setup, acme.ada, spring, "contest.yaml", edited, first.token)
    current = await files.read(setup, acme.ada, spring, "contest.yaml")

    rolled_back = await files.rollback(
        setup, acme.ada, spring, "contest.yaml", created.version, current.token
    )

    assert (await files.read(setup, acme.ada, spring, "contest.yaml")).content == first.content
    history = await files.history(setup, acme.ada, spring, "contest.yaml")
    assert isinstance(rolled_back, files.Written)
    assert history[0].version == rolled_back.version
    assert len(history) == 3
    assert history[0].message == f"Roll back contest.yaml to {created.version}"
    assert history[2].version == created.version
    with pytest.raises(NotFound):
        await files.rollback(setup, acme.ada, spring, "nothing.md", created.version, current.token)


async def test_a_tasks_write_is_a_save_and_its_rollback_a_save_too(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    statement = await files.read(setup, acme.ada, sum_task, "statement.md")
    (created,) = await files.history(setup, acme.ada, sum_task, "statement.md")

    first = await files.write(setup, acme.ada, sum_task, "statement.md", b"Add.\n", statement.token)
    current = await files.read(setup, acme.ada, sum_task, "statement.md")
    second = await files.rollback(
        setup, acme.ada, sum_task, "statement.md", VersionId(created.version), current.token
    )

    assert isinstance(first, Published) and isinstance(second, Published)
    assert (first.number, second.number) == (1, 2)
    assert (await files.read(setup, acme.ada, sum_task, "statement.md")).content == (
        statement.content
    )
    assert len(acme.fake.calls_to("write_file")) == 0
    assert len(acme.fake.calls_to("save_files")) == 2


@pytest.mark.parametrize(
    "path", ["", "/etc/passwd", "../other.task/task.yaml", "data//x", "a/./b", "x?ref=main", "a#b"]
)
async def test_a_path_that_is_not_plainly_inside_the_place_is_refused_unread(
    setup: Setup, acme: Acme, spring: ContestId, manager: Organiser, path: str
) -> None:
    with pytest.raises(InvalidPath):
        await files.read(setup, manager, spring, path)
    with pytest.raises(InvalidPath):
        await files.write(setup, manager, spring, path, b"x", None)
    assert acme.fake.calls_to("read_file") == acme.fake.calls_to("write_file") == []


@pytest.mark.parametrize(
    ("old", "new", "said"),
    [
        (
            b"visibility: everyone",
            b"visibility: public",
            'This is now called "everyone". Choose that instead.',
        ),
        (
            b"  - id: sum\n",
            b"  - id: sum\n    points: 50\n",
            "A task no longer gives points",
        ),
        (
            b"  - id: sum\n",
            b"  - id: sum\n    worth: 50\n",
            "A task no longer gives points",
        ),
        (
            b"visibility: everyone",
            b"visibility: everyone\non_system_error: last_result",
            "This setting is no longer used: a grading that fails is tried again once by "
            "itself, and a submission keeps its last result until a newer one replaces it. "
            "Remove this.",
        ),
    ],
    ids=["visibility", "points", "worth", "on_system_error"],
)
async def test_an_old_contest_key_is_refused_with_what_replaced_it(
    setup: Setup, acme: Acme, spring: ContestId, old: bytes, new: bytes, said: str
) -> None:
    refused = await _refused(setup, acme, _running(SUM_ENTRY).replace(old, new))

    assert any(message.startswith(said) for _, message in refused)


SUM_ENTRY = "  - id: sum\n"


def _running(entry: str) -> bytes:
    """The running contest with `entry` as sum's entry."""
    return RUNNING.format(visibility="everyone").replace(SUM_ENTRY, entry).encode()


async def _write_contest(setup: Setup, acme: Acme, content: bytes) -> VersionId:
    """`contest.yaml` written by ada through the editor's write."""
    current = await files.read(setup, acme.ada, SPRING, "contest.yaml")
    written = await files.write(
        setup, acme.ada, SPRING, "contest.yaml", content, current.token, confirm=True
    )
    assert isinstance(written, files.Written)
    return written.version


async def _refused(setup: Setup, acme: Acme, content: bytes) -> list[tuple[str, str]]:
    """What a write of `contest.yaml` is refused with, each problem at its
    path, and nothing written.
    """
    acme.fake.reset_calls()
    with pytest.raises(InvalidDefinition) as refused:
        await _write_contest(setup, acme, content)
    assert acme.fake.calls_to("write_file") == []
    return [(problem["path"], problem["message"]) for problem in refused.value.errors]


async def _submitted(setup: Setup, acme: Acme, entered: Entered) -> None:
    """bob's submission to sum, made now, at 12:00."""
    made = await upload(setup, acme.fake, entered.session, entered.task, b"print(3)\n")
    await submissions.submit(
        setup,
        entered.session,
        entered.task,
        {
            "code": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key="key-0001-aaaa",
    )


async def test_a_task_that_gives_no_points_may_have_a_due(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    current = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")
    head = await acme.fake.content.list_files(PLATFORM, entered.task)
    pointless = current.content.replace(b"main: {each: 100}", b"main: {}")
    saved = await task_save.save(
        setup,
        acme.ada,
        entered.task,
        {"task.yaml": Edit(pointless, head.tokens["task.yaml"])},
        confirm=True,
    )
    assert isinstance(saved, Published), saved

    # Any task may have a due (stage 5, phase 5.7).
    assert await _write_contest(
        setup,
        acme,
        _running(SUM_ENTRY + "    due: 2026-09-26T14:00:00Z\n    closes: 2026-09-26T14:00:00Z\n"),
    )


async def test_a_timeline_that_moves_behind_no_row_is_saved(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submitted(setup, acme, entered)
    timeline = _running(
        SUM_ENTRY + "    due: 2026-09-26T14:00:00Z\n    closes: 2026-09-26T14:30:00Z\n"
    )

    version = await _write_contest(setup, acme, timeline)

    written = await files.read(setup, acme.ada, SPRING, "contest.yaml")
    assert written.content == timeline
    assert acme.fake.state.repos[("acme", "spring.contest")].head == version
    (entry,) = parse_contest(written.content).tasks
    assert (entry.due, entry.closes) == (
        datetime(2026, 9, 26, 14, tzinfo=UTC),
        datetime(2026, 9, 26, 14, 30, tzinfo=UTC),
    )


async def test_a_release_that_has_passed_is_not_moved_later(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    refused = await _refused(
        setup, acme, _running(SUM_ENTRY + "    release_at: 2026-09-26T11:00:00Z\n")
    )
    started_later = await _refused(
        setup, acme, _running(SUM_ENTRY).replace(b"T10:00:00Z", b"T11:00:00Z")
    )

    said = (
        "sum was released on 26 September 2026 at 10:00 UTC, so entrants have seen it, and a later "
        "release time would hide it again."
    )
    assert refused == [("tasks[0].release_at", said)]
    assert started_later == [("start", said)]


async def test_a_due_is_not_moved_earlier_past_a_submission_it_would_make_late(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _write_contest(setup, acme, _running(SUM_ENTRY + "    due: 2026-09-26T14:00:00Z\n"))
    await _submitted(setup, acme, entered)

    refused = await _refused(setup, acme, _running(SUM_ENTRY + "    due: 2026-09-26T11:30:00Z\n"))
    earlier = await _write_contest(
        setup, acme, _running(SUM_ENTRY + "    due: 2026-09-26T12:30:00Z\n")
    )

    assert refused == [
        (
            "tasks[0].due",
            "sum's submission 1, made on 26 September 2026 at 12:00 UTC, was on time, and an "
            "earlier due time would make it late.",
        )
    ]
    assert earlier


async def test_a_close_is_not_moved_earlier_than_a_submission_already_made(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submitted(setup, acme, entered)

    refused = await _refused(
        setup, acme, _running(SUM_ENTRY + "    closes: 2026-09-26T11:59:00Z\n")
    )

    assert refused == [
        (
            "tasks[0].closes",
            "sum's submission 1, made on 26 September 2026 at 12:00 UTC, would then be after the "
            "task closed. Close it later.",
        )
    ]


async def test_a_close_is_not_moved_later_once_the_task_has_revealed(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submitted(setup, acme, entered)
    await _write_contest(setup, acme, _running(SUM_ENTRY + "    closes: 2026-09-26T12:05:00Z\n"))
    clock.advance(timedelta(minutes=10))

    refused = await _refused(
        setup, acme, _running(SUM_ENTRY + "    closes: 2026-09-26T13:00:00Z\n")
    )

    assert refused == [
        (
            "tasks[0].closes",
            "sum's hidden results were shown on 26 September 2026 at 12:05 UTC, so a later closing "
            "time would let entrants submit knowing them.",
        )
    ]


async def test_an_end_moved_before_a_submission_or_later_after_the_reveal_is_refused_at_end(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submitted(setup, acme, entered)
    contest = _running(SUM_ENTRY)

    before = await _refused(setup, acme, contest.replace(b"T15:00:00Z", b"T11:59:00Z"))
    await _write_contest(setup, acme, contest.replace(b"T15:00:00Z", b"T12:05:00Z"))
    clock.advance(timedelta(minutes=10))
    after = await _refused(setup, acme, contest)

    assert before == [
        (
            "end",
            "sum's submission 1, made on 26 September 2026 at 12:00 UTC, would then be after the "
            "task closed. Close it later.",
        )
    ]
    assert after == [
        (
            "end",
            "sum's hidden results were shown on 26 September 2026 at 12:05 UTC, so a later closing "
            "time would let entrants submit knowing them.",
        )
    ]


async def test_a_task_the_contest_did_not_list_never_opened_so_its_entry_may_open_it_later(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    product = await make_task(setup, acme, "product")
    await publish(setup, acme, product, statement=b"Multiply two numbers.\n")
    await write_contest(acme.fake, _running(SUM_ENTRY).decode())
    listed = _running(SUM_ENTRY + "  - {id: product, release_at: 2026-09-26T13:00:00Z}\n")

    version = await _write_contest(setup, acme, listed)

    assert version
    entry = parse_contest(listed).entry("product")
    assert entry is not None and entry.release_at is not None


async def test_an_entry_naming_no_task_of_the_contest_is_refused_at_its_id(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    added = await _refused(setup, acme, _running(SUM_ENTRY + "  - id: summ\n"))
    misspelled = await _refused(setup, acme, _running("  - id: summ\n"))

    assert added == [
        (
            "tasks[1].id",
            "summ is not one of this contest's tasks. Remove it here, or make a task called "
            "summ first.",
        )
    ]
    assert misspelled == [
        (
            "tasks[0].id",
            "summ is not one of this contest's tasks. Remove it here, or make a task called "
            "summ first.",
        ),
        (
            "tasks",
            "sum was released on 26 September 2026 at 10:00 UTC, so entrants have seen it. "
            "Remove it from the contest's Tasks card instead, which deletes the task too.",
        ),
    ]


async def test_an_entry_of_a_task_still_at_the_forge_is_never_dropped_by_hand(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    # Dropped by hand, a task never released and with no submissions would
    # stay at the forge with its name taken and no row to take it out from
    # (#452); one with submissions is withdrawn, not removed.
    await make_task(setup, acme, "product")
    await _submitted(setup, acme, entered)
    product = b"  - id: product\n"
    draft = _running(SUM_ENTRY).replace(b"state: published", b"state: draft")
    await write_contest(acme.fake, (draft + product).decode())

    refused = await _refused(setup, acme, draft.replace(SUM_ENTRY.encode(), product))
    unreached = await _refused(setup, acme, draft)

    assert refused == [
        (
            "tasks",
            "sum has 1 submission, so it cannot be removed from the contest. Withdraw it "
            "instead: entrants no longer see it and no leaderboard counts it, and its "
            "submissions are kept.",
        )
    ]
    assert unreached == [
        (
            "tasks",
            "product is one of this contest's tasks. Remove it from the contest's Tasks card "
            "instead, which deletes the task too.",
        )
    ]


async def test_a_withdrawn_tasks_entry_is_not_dropped_and_says_it_is_withdrawn(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await make_task(setup, acme, "product")
    await _submitted(setup, acme, entered)
    product = "  - id: product\n"
    withdrawn = SUM_ENTRY.rstrip("\n") + "\n    withdrawn: true\n"
    await write_contest(acme.fake, _running(withdrawn + product).decode())

    refused = await _refused(setup, acme, _running(product))

    assert refused == [
        (
            "tasks",
            "sum is withdrawn and has 1 submission, so its entry stays: its submissions are "
            "kept. Put it back from the contest's Tasks card, or leave it withdrawn.",
        )
    ]


async def test_withdrawn_is_written_by_hand_only_on_a_task_with_submissions(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    # A task nobody submitted to is removed, not withdrawn (#450).
    await write_contest(acme.fake, _running(SUM_ENTRY).decode())
    withdrawn = SUM_ENTRY.rstrip("\n") + "\n    withdrawn: true\n"

    refused = await _refused(setup, acme, _running(withdrawn))

    assert refused == [
        (
            "tasks[0].withdrawn",
            "Nobody has submitted to sum, so it is removed, not withdrawn. Remove it from the "
            "contest's Tasks card instead.",
        )
    ]


async def test_a_contest_opens_only_once_each_task_is_published_with_its_statement_written(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    # The same grounds the opening checklist blocks on (M57), for a save
    # through the API: a task not published, or published with its
    # statement still the starter's, keeps the contest from opening.
    draft = (await files.read(setup, acme.ada, SPRING, "contest.yaml")).content
    opened = draft.replace(b"state: draft", b"state: published")

    unpublished = await _refused(setup, acme, opened)
    await publish(setup, acme, sum_task)
    placeholder = await _refused(setup, acme, opened)
    statement = await files.read(setup, acme.ada, sum_task, "statement.md")
    await files.write(
        setup, acme.ada, sum_task, "statement.md", b"Add two numbers.\n", statement.token
    )
    await checked(setup, sum_task)

    assert unpublished == [("state", "A · sum: it is not published yet.")]
    assert placeholder == [("state", "A · sum: its statement is not written yet.")]
    assert await _write_contest(setup, acme, opened)


async def test_a_withdrawn_task_does_not_keep_a_contest_from_opening_nor_does_a_later_one(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await publish(setup, acme, sum_task)
    statement = await files.read(setup, acme.ada, sum_task, "statement.md")
    await files.write(
        setup, acme.ada, sum_task, "statement.md", b"Add two numbers.\n", statement.token
    )
    await checked(setup, sum_task)
    await make_task(setup, acme, "product")
    draft = (await files.read(setup, acme.ada, SPRING, "contest.yaml")).content
    withdrawn = draft.replace(b"- id: product", b"- {id: product, withdrawn: true}")
    assert withdrawn != draft
    # Withdrawn as the action leaves it, which no draft's save may write.
    await write_contest(acme.fake, withdrawn.decode())

    assert await _write_contest(
        setup, acme, withdrawn.replace(b"state: draft", b"state: published")
    )
    # Open, a task made since and not published yet stops no save.
    await make_task(setup, acme, "quotient")
    current = (await files.read(setup, acme.ada, SPRING, "contest.yaml")).content
    assert b"- id: quotient" in current
    assert await _write_contest(setup, acme, current.replace(b"name: ", b"name: Late ", 1))


async def test_an_open_contest_goes_back_to_draft_until_its_start_and_is_only_archived_after(
    setup: Setup, acme: Acme, sum_task: TaskId, clock: FakeClock
) -> None:
    open_ = _running(SUM_ENTRY)
    drafted = open_.replace(b"state: published", b"state: draft")
    await write_contest(acme.fake, open_.decode())

    refused = await _refused(setup, acme, drafted)
    archived = await _write_contest(
        setup, acme, open_.replace(b"state: published", b"state: archived")
    )

    assert refused == [("state", timelines.STARTED_STAYS_OPEN)]
    assert archived
    clock.advance(-timedelta(hours=3))
    await write_contest(acme.fake, open_.decode())
    assert await _write_contest(setup, acme, drafted)


async def test_a_started_contest_never_goes_to_draft_through_archived(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    open_ = _running(SUM_ENTRY)
    await write_contest(acme.fake, open_.decode())
    await _write_contest(setup, acme, open_.replace(b"state: published", b"state: archived"))

    refused = await _refused(setup, acme, open_.replace(b"state: published", b"state: draft"))

    assert refused == [("state", timelines.STARTED_STAYS_OPEN)]


async def test_a_started_contest_keeps_every_team_it_has_within_its_sizes(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    open_ = _running(SUM_ENTRY)
    await write_contest(acme.fake, open_.decode())

    alone_to_teams = await _refused(setup, acme, open_ + b"entrants: {most: 3}\n")
    await write_contest(acme.fake, open_.decode() + "entrants: {most: 3}\n")
    more_needed = await _refused(setup, acme, open_ + b"entrants: {most: 3, fewest: 2}\n")
    fewer_allowed = await _refused(setup, acme, open_ + b"entrants: {most: 2}\n")

    assert [path for path, _ in alone_to_teams] == ["entrants.most"]
    assert [path for path, _ in more_needed] == ["entrants.fewest"]
    assert [path for path, _ in fewer_allowed] == ["entrants.most"]
    assert await _write_contest(setup, acme, open_ + b"entrants: {most: 4}\n")


async def test_a_save_over_settings_that_do_not_read_is_checked_against_the_last_that_did(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    open_ = _running(SUM_ENTRY)
    await write_contest(acme.fake, open_.decode())
    await write_contest(acme.fake, open_.decode() + "registration:\n  invite_only: true\n")

    refused = await _refused(setup, acme, open_.replace(b"state: published", b"state: draft"))

    assert refused == [("state", timelines.STARTED_STAYS_OPEN)]


async def test_a_contest_that_was_never_published_moves_its_passed_dates_freely(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    draft = (
        _running(SUM_ENTRY + "    closes: 2026-09-26T11:00:00Z\n")
        .replace(b"state: published", b"state: draft")
        .decode()
    )
    await write_contest(acme.fake, draft)

    moved = draft.replace("2026-09-26T1", "2026-10-20T1").encode()
    version = await _write_contest(setup, acme, moved)

    assert version
    assert parse_contest(moved).start.month == 10


async def test_a_listing_gives_each_small_text_files_start_in_one_read_of_them_all(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    head = await acme.fake.content.list_files(PLATFORM, sum_task)
    saved = await task_save.save(
        setup,
        acme.ada,
        sum_task,
        {
            "tests/1.in": Edit(b"1 2\n" + b"9 " * 600, head.tokens["tests/1.in"]),
            "tests/1.ans": Edit(b"", head.tokens["tests/1.ans"]),
            "tests/picture.bin": Edit(b"\x89PNG\x00\x01", None),
            "tests/latin.txt": Edit("café".encode("latin-1"), None),
            "tests/long.txt": Edit("ü".encode() * 100, None),
        },
    )
    assert isinstance(saved, (Published, task_save.Draft))
    acme.fake.reset_calls()

    listed = {entry.path: entry for entry in await files.tree(setup, acme.ada, sum_task, "tests")}

    starts = {path.rsplit("/", 1)[1]: entry.start for path, entry in listed.items()}
    # Over a kilobyte, not text, or not UTF-8: no start; empty: an empty one.
    assert starts == {
        "1.ans": "",
        "1.in": None,
        "latin.txt": None,
        "long.txt": "ü" * 80,
        "picture.bin": None,
    }
    assert listed["tests/1.in"].size == 4 + 1200
    (batch,) = acme.fake.calls_to("read_blobs")
    assert len(batch.arguments["tokens"]) == 4
    assert acme.fake.calls_to("read_file") == []
    assert start_of(b"") == "" and start_of(b"a\x00b") is None
    assert all(entry.token is not None for entry in listed.values())


WRITES = (
    "save",
    "task write",
    "task rollback",
    "task upload",
    "task upload slot",
    "contest write",
    "contest rollback",
    "take out",
    "put back",
)
"""Every way a contest's or a task's files are written."""
MINE = b"Mine.\n"


async def _write(
    setup: Setup, write: str, who: Organiser, task: TaskId, token: ConflictToken
) -> object:
    if write == "save":
        return await task_save.save(setup, who, task, {"statement.md": Edit(MINE, token)})
    if write == "task write":
        return await files.write(setup, who, task, "statement.md", MINE, token)
    if write == "task rollback":
        return await files.rollback(setup, who, task, "statement.md", VersionId("v"), token)
    if write == "task upload":
        return await files.write_upload(setup, who, task, "data/big.bin", uuid.uuid4(), None)
    if write == "task upload slot":
        return await uploads.task_file_slot(
            setup, who, task, path="data/big.bin", size=4, sha256="0" * 64
        )
    if write == "contest write":
        return await files.write(setup, who, SPRING, "notes.md", MINE, None)
    if write == "take out":
        return await taking_out.take_out(setup, who, task, taking_out.TakeOut.REMOVE)
    if write == "put back":
        return await taking_out.put_back(setup, who, task)
    return await files.rollback(setup, who, SPRING, "contest.yaml", VersionId("v"), None)


@pytest.mark.parametrize("write", WRITES)
@pytest.mark.parametrize("who", ["observer", "nobody"])
async def test_every_write_refuses_an_observer_and_someone_with_no_role_writing_nothing(
    setup: Setup, acme: Acme, sum_task: TaskId, write: str, who: str
) -> None:
    # The platform writes a contest's and a task's files at the forge for
    # whoever saves (#443), so the forge's own check is no backstop: the
    # package's is the only gate, and it refuses before anything is written.
    scope = Scope("acme", "spring") if who == "observer" else Scope("acme", "autumn")
    await acme.fake.orgs.grant_role(8, scope, Role.OBSERVER if who == "observer" else Role.ADMIN)
    bob = await organiser(setup, acme.fake, 8, scope)
    head = await acme.fake.content.list_files(PLATFORM, sum_task)
    acme.fake.reset_calls()

    with pytest.raises(Forbidden, match="manager role at acme/spring"):
        await _write(setup, write, bob, sum_task, head.tokens["statement.md"])

    written = {
        "save_files",
        "write_file",
        "publish",
        "read_file",
        "deactivate",
        "delete_submission_place",
        "delete_place",
    }
    assert [call for call in acme.fake.calls if call.operation in written] == []


async def test_a_manager_is_refused_an_admin_only_key_of_a_task_and_of_a_contest(
    setup: Setup, acme: Acme, sum_task: TaskId, manager: Organiser
) -> None:
    task_yaml = await acme.fake.content.read_file(PLATFORM, sum_task, "task.yaml")
    contest_yaml = await acme.fake.content.read_file(PLATFORM, SPRING, "contest.yaml")
    acme.fake.reset_calls()

    with pytest.raises(AdminOnly):
        await files.write(
            setup,
            manager,
            sum_task,
            "task.yaml",
            task_yaml.content.replace(b"scoring:", b"submissions: {max: 3}\nscoring:"),
            task_yaml.token,
        )
    with pytest.raises(AdminOnly):
        await files.write(
            setup,
            manager,
            SPRING,
            "contest.yaml",
            contest_yaml.content.replace(b"state: draft", b"state: published"),
            contest_yaml.token,
        )
    assert [
        call for call in acme.fake.calls if call.operation in {"save_files", "write_file"}
    ] == []


@pytest.mark.parametrize(
    ("old", "new", "path"),
    [
        ("", "  - id: sum\n", "tasks[0]"),
        (
            "  - {id: sum, withdrawn: true}\n",
            "  - id: sum\n",
            "tasks[0]",
        ),
        (
            "  - {id: sum, release_at: 2026-09-26T15:00:00Z}\n",
            "  - {id: sum, release_at: 2026-09-26T13:00:00Z}\n",
            "tasks[0].release_at",
        ),
    ],
)
async def test_an_open_contest_checks_only_tasks_newly_scheduled_to_show(
    setup: Setup, acme: Acme, sum_task: TaskId, old: str, new: str, path: str
) -> None:
    before = _running(old) if old else _running(old).replace(b"tasks:\n", b"tasks: []\n")
    await write_contest(acme.fake, before.decode())
    refused = await _refused(setup, acme, _running(new))
    assert refused == [(path, "A · sum: it is not published yet.")]


async def test_an_open_contest_checks_a_task_its_later_end_schedules(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    held = _running("  - {id: sum, release_at: 2026-09-26T15:00:00Z}\n")
    await write_contest(acme.fake, held.decode())
    later = held.replace(b"end: 2026-09-26T15:00:00Z", b"end: 2026-09-26T16:00:00Z")
    assert await _refused(setup, acme, later) == [("end", "A · sum: it is not published yet.")]


async def test_an_open_contest_refuses_a_scheduled_placeholder_statement(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await publish(setup, acme, sum_task)
    await write_contest(acme.fake, _running("").replace(b"tasks:\n", b"tasks: []\n").decode())
    assert await _refused(setup, acme, _running(SUM_ENTRY)) == [
        ("tasks[0]", "A · sum: its statement is not written yet.")
    ]


@pytest.mark.parametrize("statement", [b"", b"Write the statement entrants read here.\n", None])
async def test_a_scheduled_task_cannot_publish_an_unwritten_statement(
    setup: Setup, acme: Acme, sum_task: TaskId, statement: bytes | None
) -> None:
    current = await files.read(setup, acme.ada, sum_task, "statement.md")
    await write_contest(acme.fake, _running(SUM_ENTRY).decode())
    result = await task_save.save(
        setup,
        acme.ada,
        sum_task,
        {"statement.md": Edit(statement, current.token)},
        confirm=True,
    )
    assert isinstance(result, task_save.Draft)
    assert result.errors == (
        {"path": "statement.md", "message": "A · sum: its statement is not written yet."},
    )


async def test_an_unscheduled_task_can_publish_an_unwritten_statement(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await write_contest(
        acme.fake,
        _running("  - {id: sum, release_at: 2026-09-26T15:00:00Z}\n").decode(),
    )
    result = await task_save.save(setup, acme.ada, sum_task, {}, confirm=True)
    assert isinstance(result, Published)


async def test_a_running_contest_due_change_asks_and_confirms_its_version(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    before = await files.read(setup, acme.ada, SPRING, "contest.yaml")
    changed = before.content.replace(
        b"  - id: sum\n", b"  - id: sum\n    due: 2026-09-26T14:00:00Z\n"
    )
    with pytest.raises(ConfirmationRequired) as question:
        await files.write(setup, acme.ada, SPRING, "contest.yaml", changed, before.token)
    assert before.version is not None
    assert question.value.extra["asked"] == before.version
    assert "due" in question.value.extra["changes"][0]
    assert (await files.read(setup, acme.ada, SPRING, "contest.yaml")).content == before.content
    written = await files.write(
        setup,
        acme.ada,
        SPRING,
        "contest.yaml",
        changed,
        before.token,
        confirm=True,
        asked=question.value.extra["asked"],
    )
    assert isinstance(written, files.Written)
    with pytest.raises(ConfirmationRequired):
        await files.rollback(
            setup,
            acme.ada,
            SPRING,
            "contest.yaml",
            before.version,
            (await files.read(setup, acme.ada, SPRING, "contest.yaml")).token,
        )


async def test_a_contest_confirmation_refuses_a_stale_question(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    before = await files.read(setup, acme.ada, SPRING, "contest.yaml")
    renamed = before.content.replace(b"Spring 2026", b"Autumn 2026")
    await files.write(setup, acme.ada, SPRING, "contest.yaml", renamed, before.token)
    current = await files.read(setup, acme.ada, SPRING, "contest.yaml")
    with pytest.raises(Conflict):
        await files.write(
            setup,
            acme.ada,
            SPRING,
            "contest.yaml",
            renamed + b"description: Changed.\n",
            current.token,
            confirm=True,
            asked=str(before.version),
        )


async def test_a_contest_change_before_its_start_does_not_ask(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    clock.advance(-timedelta(hours=3))
    before = await files.read(setup, acme.ada, SPRING, "contest.yaml")
    changed = before.content.replace(
        b"  - id: sum\n", b"  - id: sum\n    due: 2026-09-26T14:00:00Z\n"
    )
    written = await files.write(setup, acme.ada, SPRING, "contest.yaml", changed, before.token)
    assert isinstance(written, files.Written)
