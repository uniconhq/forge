"""A saved task stays on its publication until its packages are ready."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text, update

from forge.db.tables import EnvironmentPreparation, Grading
from forge.domain.content import ConflictToken
from forge.domain.environment import PLATFORMS, Environment, EnvironmentLock
from forge.domain.errors import ConfirmationRequired, Rejected, TooLarge
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import PublicationId, TaskId, VersionId
from forge.domain.yaml_models import problems_of
from forge.port.content import ContentPlace
from forge.port.packages import PreparedEnvironment
from forge.runtime.setup import Setup
from forge.services.task_save import preparing
from forge.services.task_save.records import Draft, Preparing, Published
from forge.testing import FakeClock
from tests.services.conftest import RUNNING, Acme
from tests.services.test_run_task_saves import _published, _save


@pytest.mark.parametrize(
    "entry",
    [
        "numpy @ https://localhost/file.whl",
        "-r requirements.txt",
        "--index-url https://localhost",
        "./numpy",
        "numpy; python_version>'3'",
        "https://localhost/file.whl",
    ],
)
def test_only_public_package_names_and_versions_are_accepted(entry: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Environment.model_validate({"python": {"packages": [entry]}})
    assert [problem["message"] for problem in problems_of(caught.value)] == [
        f"{entry} is not a package and version. Name a package from PyPI, such as numpy==2.3.*"
    ]


def test_names_extras_and_pep508_specifiers_are_kept() -> None:
    value = Environment.model_validate(
        {"python": {"packages": ["numpy==2.3.*", "requests[socks]>=2,<3"]}}
    )
    assert value.requirements()["python"]["packages"] == [
        "numpy==2.3.*",
        "requests[socks]<3,>=2",
    ]


async def _change(setup: Setup, acme: Acme, task: TaskId, package: str = "numpy") -> Preparing:
    file = await acme.fake.content.read_file(PLATFORM, task, "task.yaml")
    result = await _save(
        setup,
        acme,
        task,
        {
            "task.yaml": file.content
            + f"\nenvironment:\n  python:\n    packages: [{package}]\n".encode()
        },
        confirm=True,
        wait_for_preparation=False,
    )
    assert isinstance(result, Preparing)
    return result


async def test_save_answers_before_resolution_then_publishes_its_dual_platform_lock_and_reuses_it(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = await _published(setup, acme, sum_task, "islands")
    calls_before = len(acme.fake.packages.calls)
    entered, release = asyncio.Event(), asyncio.Event()
    resolver = acme.fake.packages.prepare

    @asynccontextmanager
    async def delayed(
        asked: Environment,
        *,
        cap: int,
        previous: EnvironmentLock | None = None,
        proceed: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[PreparedEnvironment]:
        entered.set()
        await release.wait()
        async with resolver(asked, cap=cap, previous=previous, proceed=proceed) as result:
            yield result

    monkeypatch.setattr(acme.fake.packages, "prepare", delayed)
    made = await _change(setup, acme, sum_task)
    await entered.wait()
    assert (await preparing.read(setup, acme.ada, sum_task)).status == "preparing"
    assert (await acme.fake.workspaces.list_publications(sum_task))[-1].id == previous.publication
    release.set()
    await setup.settle()
    done = await preparing.read(setup, acme.ada, sum_task)
    assert done.id == made.preparation and done.status == "ready", done.reason
    publications = await acme.fake.workspaces.list_publications(sum_task)
    assert len(publications) == 2
    lock = await acme.fake.content.read_file(
        PLATFORM, sum_task, "environment.lock", at=publications[-1].version
    )
    parsed = EnvironmentLock.model_validate_json(lock.content)
    assert set(parsed.platforms) == set(PLATFORMS)
    assert all(
        language["python"].asked == {"packages": ["numpy"]}
        for language in parsed.platforms.values()
    )
    same = await _save(setup, acme, sum_task, {}, confirm=True)
    assert isinstance(same, Published)
    assert len(acme.fake.packages.calls) == calls_before + 1


async def test_first_packages_warn_about_the_python_steps_before_preparing(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge.runtime.context import Context
    from forge.services import gradings

    await _published(
        setup, acme, sum_task, "islands", contest=RUNNING.format(visibility="everyone")
    )
    file = await acme.fake.content.read_file(PLATFORM, sum_task, "task.yaml")
    content = file.content + b"\nenvironment:\n  python:\n    packages: [numpy]\n"

    async def five(ctx: Context, task: TaskId, *, limit: int | None = None) -> int:
        return 5

    monkeypatch.setattr(gradings, "to_regrade", five)
    with pytest.raises(ConfirmationRequired) as asked:
        await _save(setup, acme, sum_task, {"task.yaml": content})
    assert asked.value.extra["regrades"] == 5
    assert "The inputs of the step solution changed." in asked.value.extra["changes"]
    assert any("solution" in line for line in asked.value.extra["runs_again"])
    result = await _save(
        setup,
        acme,
        sum_task,
        {"task.yaml": content},
        confirm=True,
        asked=asked.value.extra["asked"],
        wait_for_preparation=False,
    )
    assert isinstance(result, Preparing)
    await setup.settle()
    state = await preparing.read(setup, acme.ada, sum_task)
    assert state.status == "ready", state.reason
    assert state.result is not None
    changed = state.result["changes"]
    assert isinstance(changed, list)
    assert "The inputs of the step solution changed." in changed
    assert "The task's Python packages changed." not in changed


@pytest.mark.parametrize(
    "failure",
    [
        Rejected("There is no Python 3.14 wheel for old-package"),
        TooLarge("The packages total more than the limit"),
    ],
)
async def test_a_refused_preparation_keeps_the_previous_publication(
    setup: Setup,
    acme: Acme,
    sum_task: TaskId,
    failure: Rejected | TooLarge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = await _published(setup, acme, sum_task, "islands")

    @asynccontextmanager
    async def refuse(
        asked: Environment,
        *,
        cap: int,
        previous: EnvironmentLock | None = None,
        proceed: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[PreparedEnvironment]:
        raise failure
        yield

    monkeypatch.setattr(acme.fake.packages, "prepare", refuse)
    await _change(setup, acme, sum_task)
    await setup.settle()
    state = await preparing.read(setup, acme.ada, sum_task)
    assert state.status == "refused" and state.reason == str(failure)
    assert (await acme.fake.workspaces.list_publications(sum_task))[-1].id == previous.publication


async def test_an_unexpected_failure_refuses_the_preparation_instead_of_leaving_it_preparing(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = await _published(setup, acme, sum_task, "islands")

    @asynccontextmanager
    async def broken(
        asked: Environment,
        *,
        cap: int,
        previous: EnvironmentLock | None = None,
        proceed: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[PreparedEnvironment]:
        raise KeyError("an unexpected bug")
        yield

    monkeypatch.setattr(acme.fake.packages, "prepare", broken)
    await _change(setup, acme, sum_task)
    await setup.settle()
    state = await preparing.read(setup, acme.ada, sum_task)
    assert state.status == "refused" and state.reason == preparing.FAILED
    assert (await acme.fake.workspaces.list_publications(sum_task))[-1].id == previous.publication


async def test_a_later_save_replaces_work_before_it_can_write_or_publish(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _published(setup, acme, sum_task, "islands")
    previous_lock = await acme.fake.content.read_file(PLATFORM, sum_task, "environment.lock")
    entered, release = asyncio.Event(), asyncio.Event()
    resolver = acme.fake.packages.prepare

    @asynccontextmanager
    async def delayed(
        asked: Environment,
        *,
        cap: int,
        previous: EnvironmentLock | None = None,
        proceed: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[PreparedEnvironment]:
        entered.set()
        await release.wait()
        async with resolver(asked, cap=cap, previous=previous, proceed=proceed) as result:
            yield result

    monkeypatch.setattr(acme.fake.packages, "prepare", delayed)
    held = dict(acme.fake.uploads.held.get("unicon/packages", {}))
    first = await _change(setup, acme, sum_task)
    await entered.wait()
    second = await _save(
        setup, acme, sum_task, {"statement.md": b"A later saved statement.\n"}, keep_as_draft=True
    )
    assert isinstance(second, Draft)
    assert second.version != first.version
    release.set()
    await setup.settle()
    assert (await preparing.read(setup, acme.ada, sum_task)).status == "replaced"
    # It stopped at its first language, storing nothing more.
    assert acme.fake.uploads.held.get("unicon/packages", {}) == held
    head = await acme.fake.content.list_files(PLATFORM, sum_task)
    assert head.version == second.version
    assert (
        await acme.fake.content.read_file(PLATFORM, sum_task, "environment.lock")
    ).content == previous_lock.content


async def test_a_publication_going_live_during_preparation_is_not_published_over(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _published(setup, acme, sum_task, "islands")
    waiting = await _save(
        setup, acme, sum_task, {"statement.md": b"A statement checked first.\n"}, confirm=True
    )
    assert isinstance(waiting, Published)
    entered, release = asyncio.Event(), asyncio.Event()
    resolver = acme.fake.packages.prepare

    @asynccontextmanager
    async def delayed(
        asked: Environment,
        *,
        cap: int,
        previous: EnvironmentLock | None = None,
        proceed: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[PreparedEnvironment]:
        entered.set()
        await release.wait()
        async with resolver(asked, cap=cap, previous=previous, proceed=proceed) as result:
            yield result

    monkeypatch.setattr(acme.fake.packages, "prepare", delayed)
    await _change(setup, acme, sum_task)
    await entered.wait()
    async with setup.unit_of_work() as ctx:
        # Its check passes, as `checked` has it, while the packages are prepared.
        await ctx.db.execute(
            update(Grading)
            .where(Grading.task_id == sum_task, Grading.pipeline == "save")
            .values(status=GradingStatus.DONE, result={"schema_version": 6, "runs": []})
        )
    release.set()
    await setup.settle()
    state = await preparing.read(setup, acme.ada, sum_task)
    assert state.status == "refused" and "task changed" in (state.reason or ""), state
    assert (await acme.fake.workspaces.list_publications(sum_task))[-1].id == waiting.publication


@pytest.mark.parametrize("during", ["Prepare packages", "Prepare task plan"])
async def test_an_edit_landing_during_the_lock_write_is_not_published(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch, during: str
) -> None:
    previous = await _published(setup, acme, sum_task, "islands")
    original = acme.fake.content.save_files

    async def concurrent(
        as_: Identity,
        place: ContentPlace,
        files: Mapping[str, bytes | None],
        *,
        expected: Mapping[str, ConflictToken | None],
        message: str,
        author: int | None = None,
    ) -> VersionId:
        if message == during:
            head = await acme.fake.content.list_files(PLATFORM, place)
            await original(
                PLATFORM,
                place,
                {"statement.md": b"A concurrent edit"},
                expected={"statement.md": head.tokens["statement.md"]},
                message="Edit statement",
            )
        return await original(as_, place, files, expected=expected, message=message, author=author)

    monkeypatch.setattr(acme.fake.content, "save_files", concurrent)
    await _change(setup, acme, sum_task)
    await setup.settle()
    state = await preparing.read(setup, acme.ada, sum_task)
    assert state.status == "refused" and "task changed" in (state.reason or "")
    assert (await acme.fake.workspaces.list_publications(sum_task))[-1].id == previous.publication


async def test_background_publication_holds_the_contest_and_task_against_other_writes(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge.services import timelines

    await _published(setup, acme, sum_task, "islands")
    original = acme.fake.workspaces.publish
    observed: list[bool] = []

    async def guarded(task: TaskId, at: VersionId, note: str) -> PublicationId:
        async with setup.unit_of_work() as probe:
            for space, place in (
                (timelines.RULES_LOCK, "acme/spring"),
                (timelines.TASK_LOCK, str(task)),
            ):
                available = await probe.db.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:space, hashtext(:place))"),
                    {"space": space, "place": place},
                )
                observed.append(bool(available))
        return await original(task, at, note)

    monkeypatch.setattr(acme.fake.workspaces, "publish", guarded)
    await _change(setup, acme, sum_task)
    await setup.settle()
    assert (await preparing.read(setup, acme.ada, sum_task)).status == "ready"
    assert observed == [False, False]


async def test_only_the_pieces_are_time_limited_and_not_the_publication_after_them(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _published(setup, acme, sum_task, "islands")
    original = acme.fake.workspaces.publish

    async def slow(task: TaskId, at: VersionId, note: str) -> PublicationId:
        made = await original(task, at, note)
        await asyncio.sleep(0.3)
        return made

    monkeypatch.setattr(preparing, "LIMIT", timedelta(seconds=0.1))
    monkeypatch.setattr(acme.fake.workspaces, "publish", slow)
    await _change(setup, acme, sum_task)
    await setup.settle()
    state = await preparing.read(setup, acme.ada, sum_task)
    assert state.status == "ready", state.reason
    assert state.result is not None
    latest = (await acme.fake.workspaces.list_publications(sum_task))[-1]
    assert str(latest.id) == state.result["publication"]
    async with setup.unit_of_work() as ctx:
        rows = await ctx.db.scalars(
            select(Grading.id).where(
                Grading.task_id == sum_task,
                Grading.pipeline == "save",
                Grading.publication_id == latest.id,
            )
        )
        assert list(rows)


async def test_at_most_two_preparations_resolve_at_once(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge.runtime.context import Context

    monkeypatch.setattr(preparing, "ROOM", asyncio.Semaphore(preparing.AT_ONCE))
    inside, most = 0, 0
    two, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def counted(
        asked: Environment,
        *,
        cap: int,
        previous: EnvironmentLock | None = None,
        proceed: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[PreparedEnvironment]:
        nonlocal inside, most
        inside += 1
        most = max(most, inside)
        if inside == 2:
            two.set()
        await release.wait()
        inside -= 1
        yield PreparedEnvironment(EnvironmentLock(platforms={}), {})

    async def unused(ctx: Context, lock: EnvironmentLock, at: VersionId) -> Published:
        raise AssertionError("an old version is never published")

    monkeypatch.setattr(acme.fake.packages, "prepare", counted)
    async with setup.unit_of_work() as ctx:
        for _ in range(3):
            await preparing.start(
                ctx, acme.ada, sum_task, VersionId("a" * 40), Environment(), unused
            )
    await two.wait()
    await asyncio.sleep(0.05)
    assert (inside, most) == (2, 2)
    release.set()
    await setup.settle()
    assert most == 2
    async with setup.unit_of_work() as ctx:
        statuses = await ctx.db.scalars(
            select(EnvironmentPreparation.status).where(
                EnvironmentPreparation.task_id == str(sum_task)
            )
        )
        assert list(statuses) == ["replaced"] * 3


async def test_a_preparation_left_by_a_restart_expires_when_read(
    setup: Setup, acme: Acme, sum_task: TaskId, clock: FakeClock
) -> None:
    async with setup.unit_of_work() as ctx:
        ctx.db.add(
            EnvironmentPreparation(
                task_id=str(sum_task),
                version="a" * 40,
                owner_user_id=7,
                status="preparing",
                reason=None,
                result=None,
                deadline_at=clock.now() - timedelta(minutes=2),
            )
        )
    result = await preparing.read(setup, acme.ada, sum_task)
    assert result.status == "refused" and result.reason == preparing.INTERRUPTED


@pytest.mark.parametrize(
    "asked",
    [
        {"go": {}},
        {"python": {"index-url": "https://elsewhere.test"}},
        {"cpp": {"packages": ["numpy"]}},
    ],
)
def test_only_supported_languages_and_registry_keys_are_accepted(
    asked: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Environment.model_validate(asked)


async def test_a_hand_edited_lock_is_refused(setup: Setup, acme: Acme, sum_task: TaskId) -> None:
    from forge.domain.errors import ReservedPath

    with pytest.raises(ReservedPath):
        await _save(setup, acme, sum_task, {"environment.lock": b"{}"})
