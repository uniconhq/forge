"""Making something at the forge is recorded step by step: a failure leaves
the row naming the step and the error, and a rerun starts at the step after
the last one that completed.
"""

import pytest

from forge.context import Context
from forge.domain.errors import Conflict, Unavailable
from forge.domain.ids import OrgName
from forge.forges.fake import FakeForge
from forge.services import orgs, provisioning
from forge.services.provisioning import Attempt, Step


class Steps:
    """Three steps that record their runs, the second of which fails while
    `broken` is set.
    """

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.broken = True

    def all(self) -> list[Step]:
        return [Step("one", self._one), Step("two", self._two), Step("three", self._three)]

    async def _one(self, attempt: Attempt) -> None:
        self.ran.append("one")

    async def _two(self, attempt: Attempt) -> None:
        self.ran.append("two")
        if self.broken:
            raise Unavailable("the forge went away")

    async def _three(self, attempt: Attempt) -> None:
        self.ran.append(f"three@{attempt.number}")


async def test_a_failure_names_the_step_and_the_error_and_a_rerun_starts_after_the_last_done(
    ctx: Context,
) -> None:
    steps = Steps()

    with pytest.raises(Unavailable):
        await provisioning.run(ctx, "contest", "acme/spring", steps.all())
    await ctx.db.rollback()

    failed = await provisioning.record_of(ctx, "contest", "acme/spring")
    assert failed is not None
    assert failed.status == "failed"
    assert failed.last_step == "one"
    assert failed.error == "two: the forge went away"
    assert failed.attempts == 1

    steps.broken = False
    done = await provisioning.run(ctx, "contest", "acme/spring", steps.all())

    assert steps.ran == ["one", "two", "two", "three@2"]
    assert done.status == "ready"
    assert done.last_step == "three"
    assert done.error is None
    assert done.attempts == 2
    assert done.ready_at == ctx.now


async def test_a_thing_already_made_is_not_made_again(ctx: Context) -> None:
    steps = Steps()
    steps.broken = False
    await provisioning.run(ctx, "task", "acme/spring/sum", steps.all())

    again = await provisioning.run(ctx, "task", "acme/spring/sum", steps.all())

    assert steps.ran == ["one", "two", "three@1"]
    assert again.status == "ready"
    assert again.attempts == 1


async def test_the_record_survives_the_callers_rollback(ctx: Context) -> None:
    steps = Steps()
    steps.broken = False

    await provisioning.run(ctx, "workspace", "acme/spring/@bob", steps.all())
    await ctx.db.rollback()

    record = await provisioning.record_of(ctx, "workspace", "acme/spring/@bob")
    assert record is not None
    assert record.status == "ready"


async def test_an_org_is_made_in_three_recorded_steps(ctx: Context, fake: FakeForge) -> None:
    done = await orgs.provision(ctx, OrgName("acme"), description="Acme")

    assert done.status == "ready"
    assert [call.operation for call in fake.calls] == [
        "create_org",
        "create_roles",
        "create_thread_labels",
    ]
    assert fake.state.orgs["acme"].roles_ready is True
    assert fake.state.orgs["acme"].labels == {"announcement", "clarification", "answered"}


async def test_an_org_whose_roles_failed_is_finished_by_a_rerun(
    ctx: Context, fake: FakeForge
) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    fake.unavailable = True
    fake.reset_calls()

    with pytest.raises(Unavailable):
        await orgs.provision(ctx, OrgName("acme"), description="Acme")
    await ctx.db.rollback()
    failed = await provisioning.record_of(ctx, "org", "acme")
    assert failed is not None
    assert (failed.status, failed.last_step) == ("failed", None)
    assert failed.error is not None
    assert failed.error.startswith("org: ")

    fake.unavailable = False
    fake.orgs = fake.orgs
    done = await orgs.provision(ctx, OrgName("acme"), description="Acme")

    assert done.status == "ready"
    assert [call.operation for call in fake.calls] == [
        "create_org",
        "create_org",
        "create_roles",
        "create_thread_labels",
    ]
    assert fake.state.orgs["acme"].roles_ready is True


async def test_a_name_someone_else_holds_is_a_conflict_on_the_first_try(
    ctx: Context, fake: FakeForge
) -> None:
    await fake.orgs.create_org(OrgName("acme"), description="Someone else's")

    with pytest.raises(Conflict):
        await orgs.provision(ctx, OrgName("acme"), description="Acme")
    await ctx.db.rollback()

    failed = await provisioning.record_of(ctx, "org", "acme")
    assert failed is not None
    assert failed.status == "failed"
