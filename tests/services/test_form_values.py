"""A contest's and a task's settings read and saved as a form's values: the
file's data with leaderboards, columns, groups and values as named lists; a
save writes the values into the file as it is, keeping its comments, through
the same checks as the file's own save, and refuses a stale token, an
admin-only key changed by a manager and two entries with one name.
"""

import pytest

from forge.domain import settings_values
from forge.domain.content import ConflictToken, Edit, Values
from forge.domain.errors import AdminOnly, Conflict, Forbidden, Rejected
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId
from forge.domain.roles import Role, Scope
from forge.domain.settings_values import MAX_FILE
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import form_values, task_save
from forge.services.access import Organiser
from forge.services.task_save import Draft
from tests.services.conftest import SPRING, Acme, organiser, write_contest

COMMENTED = """\
# The contest's settings.
name: "Spring 2026"  # its name
start: 2026-10-03T12:00:00Z
end: 2026-10-03T17:00:00Z
state: draft
visibility: signed-in

# Teams.
entrants:
  most: 3  # at most three
tasks:
  - id: sum  # the first
leaderboards:
  main:  # everyone's
    title: Main
    columns:
      total: {block: count, over: {results: sum}}  # the score
"""


@pytest.fixture
async def manager(setup: Setup, acme: Acme, spring: ContestId) -> Organiser:
    await acme.fake.orgs.grant_role(8, Scope("acme", "spring"), Role.MANAGER)
    return await organiser(setup, acme.fake, 8, Scope("acme", "spring"), Role.MANAGER)


async def test_a_contests_settings_read_as_values_with_named_lists(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await write_contest(acme.fake, COMMENTED)

    read = await form_values.read(setup, acme.ada, SPRING)

    assert read.unreadable is None
    assert read.token is not None
    assert read.values is not None
    assert read.values["entrants"] == {"most": 3}
    assert read.values["leaderboards"] == [
        {
            "key": "main",
            "title": "Main",
            "columns": [
                {"name": "total", "formula": {"block": "count", "over": {"results": "sum"}}}
            ],
        }
    ]


async def test_settings_that_do_not_read_say_why(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await write_contest(acme.fake, "name: [unclosed\n")

    read = await form_values.read(setup, acme.ada, SPRING)

    assert read.values is None
    assert read.unreadable
    assert read.token is not None


async def test_saving_values_keeps_every_comment_of_the_file(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await write_contest(acme.fake, COMMENTED)
    read = await form_values.read(setup, acme.ada, SPRING)
    assert read.values is not None
    values = read.values
    values["entrants"]["most"] = 4
    values["leaderboards"][0]["columns"][0]["name"] = "points"

    await form_values.write_contest(setup, acme.ada, SPRING, values, read.token)

    after = await acme.fake.content.read_file(PLATFORM, SPRING, "contest.yaml")
    assert after.content.decode() == COMMENTED.replace("most: 3", "most: 4").replace(
        "total:", "points:"
    )


async def test_a_stale_token_is_refused_with_nothing_written(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    read = await form_values.read(setup, acme.ada, SPRING)
    assert read.values is not None
    await write_contest(acme.fake, COMMENTED)
    acme.fake.reset_calls()

    with pytest.raises(Conflict):
        await form_values.write_contest(setup, acme.ada, SPRING, read.values, read.token)

    assert acme.fake.calls_to("write_file") == []


async def test_a_manager_may_not_change_an_admin_only_key_by_values(
    setup: Setup, acme: Acme, sum_task: TaskId, manager: Organiser
) -> None:
    read = await form_values.read(setup, manager, SPRING)
    assert read.values is not None
    values = {**read.values, "state": "published"}
    acme.fake.reset_calls()

    with pytest.raises(AdminOnly):
        await form_values.write_contest(setup, manager, SPRING, values, read.token)

    assert acme.fake.calls_to("write_file") == []


async def test_an_observer_reads_and_may_not_save(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await acme.fake.orgs.grant_role(9, Scope("acme", "spring"), Role.OBSERVER)
    observer = await organiser(setup, acme.fake, 9, Scope("acme", "spring"), Role.OBSERVER)

    read = await form_values.read(setup, observer, SPRING)
    assert read.values is not None

    with pytest.raises(Forbidden):
        await form_values.write_contest(setup, observer, SPRING, read.values, read.token)


@pytest.mark.parametrize(
    ("change", "path"),
    [
        (
            lambda boards: boards.append({"key": "main", "title": "Again"}),
            ("leaderboards", "main"),
        ),
        (
            lambda boards: boards[0]["columns"].append({"name": "total", "formula": 1}),
            ("leaderboards", "main", "columns", "total"),
        ),
    ],
)
async def test_two_entries_with_one_name_are_refused_at_that_name(
    setup: Setup, acme: Acme, sum_task: TaskId, change: object, path: tuple[str, ...]
) -> None:
    await write_contest(acme.fake, COMMENTED)
    read = await form_values.read(setup, acme.ada, SPRING)
    assert read.values is not None
    assert callable(change)
    change(read.values["leaderboards"])

    with pytest.raises(InvalidDefinition) as refused:
        await form_values.write_contest(setup, acme.ada, SPRING, read.values, read.token)

    assert [problem["path"] for problem in refused.value.errors] == [path]


@pytest.mark.parametrize(
    ("column", "path"),
    [
        ({"name": "", "formula": 1}, ("leaderboards", "main", "columns", "")),
        ({"name": "rest"}, ("leaderboards", "main", "columns", "rest")),
    ],
)
async def test_a_column_with_no_name_or_no_formula_is_refused_at_its_path(
    setup: Setup, acme: Acme, sum_task: TaskId, column: dict[str, object], path: tuple[str, ...]
) -> None:
    await write_contest(acme.fake, COMMENTED)
    read = await form_values.read(setup, acme.ada, SPRING)
    assert read.values is not None
    read.values["leaderboards"][0]["columns"].append(column)

    with pytest.raises(InvalidDefinition) as refused:
        await form_values.write_contest(setup, acme.ada, SPRING, read.values, read.token)

    assert [problem["path"][: len(path)] for problem in refused.value.errors] == [path]


async def test_a_hold_with_no_end_is_refused_at_the_hold(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    read = await form_values.read(setup, acme.ada, SPRING)
    assert read.values is not None
    read.values["hold"] = {"from": "2026-10-03T16:00:00Z"}

    with pytest.raises(InvalidDefinition) as refused:
        await form_values.write_contest(setup, acme.ada, SPRING, read.values, read.token)

    assert [problem["path"] for problem in refused.value.errors] == [("hold", "until")]


async def test_a_tasks_values_save_with_the_task_keeping_its_comments(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    read = await form_values.read(setup, acme.ada, sum_task)
    assert read.values is not None
    values = read.values
    assert values["scoring"]["values"][0]["name"] == "score"
    values["name"] = "Sum of Three"
    values["scoring"]["groups"] = [{"name": "first", "picks": {"input": ["tests/1.in"]}}]

    await task_save.save(setup, acme.ada, sum_task, {"task.yaml": Edit(Values(values), read.token)})

    after = await acme.fake.content.read_file(PLATFORM, sum_task, "task.yaml")
    text = after.content.decode()
    assert text.startswith("# The task's settings.")
    assert 'name: "Sum of Three"\n' in text
    assert "  groups:\n    first: {input: [tests/1.in]}\n" in text


async def test_a_task_group_that_picks_nothing_is_refused_at_the_group(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    read = await form_values.read(setup, acme.ada, sum_task)
    assert read.values is not None
    read.values["scoring"]["groups"] = [{"name": "first", "picks": {}}]

    saved = await task_save.save(
        setup, acme.ada, sum_task, {"task.yaml": Edit(Values(read.values), read.token)}
    )

    assert isinstance(saved, Draft)
    assert [error["path"] for error in saved.errors] == [("scoring", "groups", "first")]


@pytest.mark.parametrize(
    ("groups", "values", "path"),
    [
        ([{"name": "", "picks": {"input": ["tests/1.in"]}}], None, ("scoring", "groups", "")),
        (None, [{"name": "", "formula": 1}], ("scoring", "values", "")),
        (
            None,
            [{"name": "score", "formula": 1}, {"name": "score", "formula": 2}],
            ("scoring", "values", "score"),
        ),
    ],
)
async def test_a_group_or_value_with_no_name_or_a_repeated_one_is_refused(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    groups: list[object] | None,
    values: list[object] | None,
    path: tuple[str, ...],
) -> None:
    read = await form_values.read(setup, acme.ada, sum_task)
    assert read.values is not None
    if groups is not None:
        read.values["scoring"]["groups"] = groups
    if values is not None:
        read.values["scoring"]["values"] = values

    try:
        saved = await task_save.save(
            setup, acme.ada, sum_task, {"task.yaml": Edit(Values(read.values), read.token)}
        )
    except InvalidDefinition as refused:
        paths = [problem["path"] for problem in refused.errors]
    else:
        assert isinstance(saved, Draft)
        paths = [error["path"] for error in saved.errors]

    assert path in [tuple(each[: len(path)]) for each in paths]


async def test_a_tasks_values_with_a_stale_token_are_refused(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    read = await form_values.read(setup, acme.ada, sum_task)
    assert read.values is not None
    acme.fake.reset_calls()

    with pytest.raises(Conflict):
        await task_save.save(
            setup,
            acme.ada,
            sum_task,
            {"task.yaml": Edit(Values(read.values), ConflictToken("stale"))},
        )

    assert acme.fake.calls_to("save_files") == []


async def test_settings_values_over_the_most_a_file_holds_are_refused_before_any_write(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    contest = await form_values.read(setup, acme.ada, SPRING)
    task = await form_values.read(setup, acme.ada, sum_task)
    big = {"name": "x" * MAX_FILE}

    def unwritten(*_: object) -> str:
        raise AssertionError("values over the limit were written")

    monkeypatch.setattr(settings_values, "write_values", unwritten)
    acme.fake.reset_calls()

    with pytest.raises(Rejected, match="Settings can be at most 256 KB"):
        await form_values.write_contest(setup, acme.ada, SPRING, big, contest.token)
    with pytest.raises(Rejected, match="Settings can be at most 256 KB"):
        await task_save.save(
            setup, acme.ada, sum_task, {"task.yaml": Edit(Values(big), task.token)}
        )

    assert acme.fake.calls_to("write_file") == []
