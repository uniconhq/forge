"""The rules the domain types carry: names, roles and their inheritance, the
session lifetimes, and where a sign-in may land.
"""

from datetime import UTC, datetime, timedelta

import pytest

from forge.domain.clock import FakeClock
from forge.domain.errors import InvalidName
from forge.domain.names import (
    validate_contest_or_task_name,
    validate_name,
)
from forge.domain.next_path import safe_next
from forge.domain.roles import Role, RoleGrant, Scope, holds
from forge.domain.sessions import SessionTimes, is_expired, is_fresh, refresh_due

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
IDLE = timedelta(days=14)


def test_a_name_is_lower_case_letters_digits_hyphens_and_underscores() -> None:
    for good in ("spring-2026", "2026-spring", "week_1", "42"):
        assert validate_name(good) == good
    for bad in ("Spring", "-a", "_a", "a.b", "a b", ""):
        with pytest.raises(InvalidName):
            validate_name(bad)


def test_contest_and_task_names_stop_at_twenty_four() -> None:
    assert validate_contest_or_task_name("a" * 24)
    with pytest.raises(InvalidName):
        validate_contest_or_task_name("a" * 25)
    assert validate_name("a" * 40)
    with pytest.raises(InvalidName):
        validate_name("a" * 41)


def test_a_role_at_an_org_reaches_every_contest_and_task_in_it() -> None:
    grants = [RoleGrant(Scope("acme"), Role.ADMIN)]
    assert holds(grants, Scope("acme", "spring", "sum"), Role.ADMIN)
    assert holds(grants, Scope("acme", "spring"), Role.OBSERVER)
    assert not holds(grants, Scope("other"), Role.OBSERVER)


def test_a_scopes_lineage_is_every_scope_that_covers_it() -> None:
    task = Scope("acme", "spring", "sum")
    assert task.lineage() == (Scope("acme"), Scope("acme", "spring"), task)
    assert Scope("acme").lineage() == (Scope("acme"),)
    others = [Scope("acme", "autumn"), Scope("acme", "spring", "product"), Scope("other")]
    for scope in [*task.lineage(), *others]:
        assert (scope in task.lineage()) == scope.covers(task)


def test_a_higher_role_counts_as_a_lower_one_but_not_the_reverse() -> None:
    manager = [RoleGrant(Scope("acme", "spring"), Role.MANAGER)]
    assert holds(manager, Scope("acme", "spring", "sum"), Role.OBSERVER)
    assert not holds(manager, Scope("acme", "spring"), Role.ADMIN)
    assert not holds(manager, Scope("acme"), Role.OBSERVER)


def test_a_session_ends_at_the_first_of_its_two_lifetimes() -> None:
    fresh = SessionTimes(expires_at=NOW + timedelta(days=1), last_seen_at=NOW, revoked_at=None)
    assert not is_expired(fresh, NOW, IDLE)
    assert is_expired(fresh, NOW + timedelta(days=1), IDLE)
    assert is_expired(fresh, NOW + IDLE + timedelta(seconds=1), IDLE)
    revoked = SessionTimes(expires_at=NOW + timedelta(days=1), last_seen_at=NOW, revoked_at=NOW)
    assert is_expired(revoked, NOW, IDLE)


def test_a_credential_is_refreshed_before_it_expires() -> None:
    assert refresh_due(NOW + timedelta(minutes=4), NOW)
    assert not refresh_due(NOW + timedelta(minutes=6), NOW)


def test_a_fresh_sign_in_is_within_the_window() -> None:
    assert is_fresh(NOW - timedelta(minutes=4), NOW, timedelta(minutes=5))
    assert not is_fresh(NOW - timedelta(minutes=6), NOW, timedelta(minutes=5))


def test_a_sign_in_lands_only_on_a_path_inside_the_platform() -> None:
    assert safe_next("/contests/4") == "/contests/4"
    for outside in (
        None,
        "",
        "https://evil.test",
        "//evil.test",
        "/\\evil",
        "/a\nb",
        "/" + "a" * 3000,
    ):
        assert safe_next(outside) == "/"


def test_a_fake_clock_moves_when_advanced() -> None:
    clock = FakeClock(NOW)
    clock.advance(IDLE)
    assert clock.now() == NOW + IDLE
