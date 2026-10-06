"""The rules a registration is checked against and the decisions on it: the
window counts each end from its own instant, an invite-only contest needs an
invite, a code has to be the contest's own, a pattern has to match the whole
address whatever its case, and what let a request through is what is kept.
Only a pending registration is decided, a rejection needs a reason, and an
extension is at least nothing and at most a year.
"""

import time
from datetime import UTC, datetime, timedelta

import pytest

from forge.domain.definitions import Registration
from forge.domain.errors import (
    DomainNotAllowed,
    InvalidExtension,
    InvalidReason,
    InviteRequired,
    RegistrationClosed,
    WrongInviteCode,
    WrongStatus,
)
from forge.domain.registration import (
    EMAIL_MAX,
    REASON_MAX,
    Status,
    checked_extension,
    checked_reason,
    checked_tasks,
    eligibility,
    matches,
    refuse_closed,
    refuse_undecidable,
    window_open,
)

OPENS = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
CLOSES = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
WINDOW = Registration(opens=OPENS, closes=CLOSES)


@pytest.mark.parametrize(
    ("now", "is_open"),
    [
        (OPENS - timedelta(seconds=1), False),
        (OPENS, True),
        (CLOSES - timedelta(seconds=1), True),
        (CLOSES, False),
    ],
    ids=["before", "at-opening", "last-second", "at-closing"],
)
def test_the_window_counts_each_end_from_its_own_instant(now: datetime, is_open: bool) -> None:
    assert window_open(WINDOW, now) is is_open
    if is_open:
        refuse_closed(WINDOW, now)
    else:
        with pytest.raises(RegistrationClosed):
            refuse_closed(WINDOW, now)


def test_a_contest_without_a_window_takes_registrations_at_any_time() -> None:
    assert window_open(Registration(), datetime(2000, 1, 1, tzinfo=UTC))
    refuse_closed(Registration(), datetime(2100, 1, 1, tzinfo=UTC))


def test_a_contest_that_asks_for_nothing_lets_anyone_through_and_keeps_nothing() -> None:
    assert eligibility(Registration(), emails=(), invite_code=None, invited=False) == {}


def test_an_invite_only_contest_needs_an_accepted_invite() -> None:
    rules = Registration(invite_only=True)

    with pytest.raises(InviteRequired) as refused:
        eligibility(rules, emails=(), invite_code=None, invited=False)

    assert refused.value.code == "invite_required"
    assert eligibility(rules, emails=(), invite_code=None, invited=True) == {"invited": True}


@pytest.mark.parametrize("given", [None, "", "Sesame", "sesame "])
def test_a_code_has_to_be_the_contests_own(given: str | None) -> None:
    rules = Registration(code="sesame")

    with pytest.raises(WrongInviteCode) as refused:
        eligibility(rules, emails=(), invite_code=given, invited=False)

    assert refused.value.code == "wrong_invite_code"
    assert eligibility(rules, emails=(), invite_code="sesame", invited=False) == {
        "invite_code": True
    }


@pytest.mark.parametrize(
    ("email", "passes"),
    [
        ("ada@u.nus.edu", True),
        ("Ada@U.NUS.EDU", True),
        ("ada@u.nus.edu.evil.test", False),
        ("ada@nus.edu", False),
        (None, False),
    ],
)
def test_a_pattern_has_to_match_the_whole_address_whatever_its_case(
    email: str | None, passes: bool
) -> None:
    rules = Registration(email_pattern=r".*@u\.nus\.edu")

    emails = (email,) if email is not None else ()
    if passes:
        assert eligibility(rules, emails=emails, invite_code=None, invited=False) == {
            "email": email
        }
    else:
        with pytest.raises(DomainNotAllowed) as refused:
            eligibility(rules, emails=emails, invite_code=None, invited=False)
        assert refused.value.code == "domain_not_allowed"


def test_the_first_rule_broken_is_the_one_refused_with() -> None:
    rules = Registration(invite_only=True, code="sesame", email_pattern=r".*@u\.nus\.edu")

    with pytest.raises(InviteRequired):
        eligibility(rules, emails=("eve@example.test",), invite_code="wrong", invited=False)
    with pytest.raises(WrongInviteCode):
        eligibility(rules, emails=("eve@example.test",), invite_code="wrong", invited=True)
    with pytest.raises(DomainNotAllowed):
        eligibility(rules, emails=("eve@example.test",), invite_code="sesame", invited=True)
    assert eligibility(rules, emails=("ada@u.nus.edu",), invite_code="sesame", invited=True) == {
        "invited": True,
        "invite_code": True,
        "email": "ada@u.nus.edu",
    }


def test_a_decision_is_refused_from_a_status_it_is_not_allowed_from() -> None:
    refuse_undecidable(Status.PENDING, (Status.PENDING,), "approved")

    with pytest.raises(WrongStatus) as refused:
        refuse_undecidable(Status.REJECTED, (Status.PENDING,), "approved")

    assert refused.value.extra == {"current": Status.REJECTED}
    assert "rejected cannot be approved" in refused.value.detail


@pytest.mark.parametrize("reason", ["", "   ", "x" * (REASON_MAX + 1)])
def test_a_rejection_needs_a_reason_of_a_readable_length(reason: str) -> None:
    with pytest.raises(InvalidReason):
        checked_reason(reason)
    assert checked_reason("  Not a student.  ") == "Not a student."


def test_an_extension_is_whole_seconds_between_nothing_and_a_year() -> None:
    assert checked_extension(timedelta(minutes=30, microseconds=5)) == timedelta(minutes=30)
    assert checked_extension(timedelta(0)) == timedelta(0)
    with pytest.raises(InvalidExtension):
        checked_extension(timedelta(seconds=-1))
    with pytest.raises(InvalidExtension):
        checked_extension(timedelta(days=366))


def test_any_confirmed_address_that_matches_lets_the_person_through() -> None:
    rules = Registration(email_pattern=r".*@u\.nus\.edu")

    outcome = eligibility(
        rules, emails=("ada@example.test", "ada@u.nus.edu"), invite_code=None, invited=False
    )

    assert outcome == {"email": "ada@u.nus.edu"}


def test_a_pattern_that_takes_too_long_lets_nobody_through_and_holds_nobody_up() -> None:
    started = time.monotonic()

    assert not matches(r"(x+x+)+y", "x" * 200)
    assert not matches(r".*", "x" * (EMAIL_MAX + 1))
    assert time.monotonic() - started < 1


def test_an_extension_names_the_tasks_it_is_for_or_none_for_every_task() -> None:
    known = ("a", "b", "c")

    assert checked_tasks(None, known) is None
    assert checked_tasks(["b", "a", "b"], known) == ("b", "a")
    with pytest.raises(InvalidExtension):
        checked_tasks([], known)
    with pytest.raises(InvalidExtension) as refused:
        checked_tasks(["a", "z"], known)
    assert "z is not a task of the contest." in refused.value.detail
