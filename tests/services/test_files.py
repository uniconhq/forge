"""A contest's or a task's files, read and written as the organiser. Reading
needs observer and writing manager, both made as the organiser's own
identity; a stale token is a conflict; `contest.yaml` is refused whole when
it is not valid, when an entry names no task of the contest or gives a worth
or a due to a task that gives no points (C1), or when it moves a task's
timeline behind what rows already did or drops the entry of a task that
opened or has submissions (C2), a contest never published moving freely,
and a manager's change to an admin-only key is refused naming it, with
nothing written either way; a task's write is its save; and a rollback
writes the old content as a new change.
"""

from datetime import timedelta

import pytest

from forge.domain.clock import FakeClock
from forge.domain.content import Edit
from forge.domain.definitions import parse_contest
from forge.domain.errors import AdminOnly, Conflict, Forbidden, InvalidPath, NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId, VersionId
from forge.domain.roles import Role, Scope
from forge.domain.submissions import SubmittedInput
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import files, publications, submissions
from forge.services.access import Organiser
from forge.services.publications import Published
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    Entered,
    make_task,
    organiser,
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
    bob = (await acme.fake.identity.find_user(8)).username

    latest, *_ = await files.history(setup, acme.ada, spring, "contest.yaml")

    assert (latest.author_id, latest.author) == (8, bob)


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
    assert [entry.path for entry in listed] == ["public", "statement.md", "task.yaml", "tests"]
    assert [entry.path for entry in folder] == ["tests/main"]
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

    version = await files.write(setup, manager, spring, "contest.yaml", changed, before.token)

    assert isinstance(version, str)
    (call,) = acme.fake.calls_to("write_file")
    assert call.identity == manager.identity
    written = await files.read(setup, manager, spring, "contest.yaml")
    assert parse_contest(written.content).end.hour == 18
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
            b"visibility: signed-in\nregistration: {invite_only: true}",
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
    edited = first.content + b"team_size: 5\n"
    await files.write(setup, acme.ada, spring, "contest.yaml", edited, first.token)
    current = await files.read(setup, acme.ada, spring, "contest.yaml")

    version = await files.rollback(
        setup, acme.ada, spring, "contest.yaml", created.version, current.token
    )

    assert (await files.read(setup, acme.ada, spring, "contest.yaml")).content == first.content
    history = await files.history(setup, acme.ada, spring, "contest.yaml")
    assert history[0].version == version
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
        (b"visibility: everyone", b"visibility: public", "public is now everyone."),
        (b"  - id: sum\n", b"  - id: sum\n    points: 50\n", "points is now worth."),
    ],
    ids=["visibility", "points"],
)
async def test_an_old_contest_key_is_refused_with_what_replaced_it(
    setup: Setup, acme: Acme, spring: ContestId, old: bytes, new: bytes, said: str
) -> None:
    refused = await _refused(setup, acme, _running(SUM_ENTRY).replace(old, new))

    assert said in [message for _, message in refused]


SUM_ENTRY = "  - id: sum\n"


def _running(entry: str) -> bytes:
    """The running contest with `entry` as sum's entry."""
    return RUNNING.format(visibility="everyone").replace(SUM_ENTRY, entry).encode()


async def _write_contest(setup: Setup, acme: Acme, content: bytes) -> VersionId:
    """`contest.yaml` written by ada through the editor's write."""
    current = await files.read(setup, acme.ada, SPRING, "contest.yaml")
    written = await files.write(setup, acme.ada, SPRING, "contest.yaml", content, current.token)
    assert isinstance(written, str)
    return VersionId(written)


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
            "submission": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key="key-0001-aaaa",
    )


async def test_a_worth_or_a_due_on_a_task_that_gives_no_points_is_refused_at_its_line(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    current = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")
    head = await acme.fake.content.list_files(PLATFORM, entered.task)
    pointless = current.content.replace(b"main: {each: 100}", b"main: {}")
    saved = await publications.save(
        setup, acme.ada, entered.task, {"task.yaml": Edit(pointless, head.tokens["task.yaml"])}
    )
    assert isinstance(saved, Published), saved

    refused = await _refused(
        setup, acme, _running(SUM_ENTRY + "    worth: 50\n    due: 2026-09-26T14:00:00Z\n")
    )

    assert refused == [
        ("tasks[0].worth", "sum gives no points, so it has no worth: rank it on a value."),
        ("tasks[0].due", "sum gives no points, so a due would change nothing; use closes."),
    ]
    assert await _write_contest(
        setup, acme, _running(SUM_ENTRY + "    closes: 2026-09-26T14:00:00Z\n")
    )


async def test_a_timeline_that_moves_behind_no_row_is_saved(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submitted(setup, acme, entered)
    timeline = _running(
        SUM_ENTRY + "    worth: 80\n    due: 2026-09-26T14:00:00Z\n    late_per_day: 0.5\n"
        "    closes: 2026-09-26T14:30:00Z\n"
    )

    version = await _write_contest(setup, acme, timeline)

    written = await files.read(setup, acme.ada, SPRING, "contest.yaml")
    assert written.content == timeline
    assert acme.fake.state.repos[("acme", "spring.contest")].head == version
    (entry,) = parse_contest(written.content).tasks
    assert (entry.worth, entry.late_per_day) == (80, 0.5)


async def test_a_release_that_has_passed_is_not_moved_later(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    refused = await _refused(
        setup, acme, _running(SUM_ENTRY + "    release_at: 2026-09-26T11:00:00Z\n")
    )
    started_later = await _refused(
        setup, acme, _running(SUM_ENTRY).replace(b"T10:00:00Z", b"T11:00:00Z")
    )

    said = "sum opened at 2026-09-26T10:00:00+00:00; it cannot be hidden again."
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
            "sum's submission 1, made 2026-09-26T12:00:00+00:00 was on time; an earlier due "
            "would make it late.",
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
            "sum's submission 1, made 2026-09-26T12:00:00+00:00 would be after the task closed; "
            "close it later.",
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
            "sum's hidden results were shown at 2026-09-26T12:05:00+00:00; a later close would "
            "let rows submit knowing them.",
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
            "sum's submission 1, made 2026-09-26T12:00:00+00:00 would be after the task closed; "
            "close it later.",
        )
    ]
    assert after == [
        (
            "end",
            "sum's hidden results were shown at 2026-09-26T12:05:00+00:00; a later close would "
            "let rows submit knowing them.",
        )
    ]


async def test_a_task_the_contest_did_not_list_never_opened_so_its_entry_may_open_it_later(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await make_task(setup, acme, "product")
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

    assert added == [("tasks[1].id", "summ is not a task of this contest.")]
    assert misspelled == [
        ("tasks[0].id", "summ is not a task of this contest."),
        ("tasks", "sum opened at 2026-09-26T10:00:00+00:00; it cannot be hidden again."),
    ]


async def test_an_entry_is_dropped_only_from_a_task_that_neither_opened_nor_has_submissions(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await make_task(setup, acme, "product")
    await _submitted(setup, acme, entered)
    product = b"  - id: product\n"
    draft = _running(SUM_ENTRY).replace(b"state: published", b"state: draft")
    await write_contest(acme.fake, (draft + product).decode())

    refused = await _refused(setup, acme, draft.replace(SUM_ENTRY.encode(), product))
    version = await _write_contest(setup, acme, draft)

    assert refused == [("tasks", "sum has 1 submission; it cannot be hidden, so keep its entry.")]
    assert version


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
