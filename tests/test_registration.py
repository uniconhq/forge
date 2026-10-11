"""The rules a person is checked against when they join an entrant, and the
decisions on an entrant: the window counts each end from its own instant; a
person qualifies by any one of the contest's ways, an invite way needing an
accepted invite, a sign-up every condition it gives, a code the contest's
own, a domain the whole part after an address's last `@`, a pattern the
whole address, whatever its case; the most lenient way that admits them
wins; when none does, the refusal says what would help; a code is asked for
only when a code would let them in. Only a pending entrant is decided, a
rejection needs a reason, and an extension is at least nothing and at most
a year.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from forge.domain.definitions import Approval, InviteWay, Registration, SignupWay, Way
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
    WayKind,
    asks_code,
    checked_extension,
    checked_reason,
    checked_tasks,
    domain_matches,
    matches,
    qualify,
    refuse_closed,
    refuse_undecidable,
    window_open,
)

OPENS = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
CLOSES = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
WINDOW = Registration(opens=OPENS, closes=CLOSES)
AUTO, MANUAL = Approval.AUTO, Approval.MANUAL


def ways(*listed: InviteWay | SignupWay) -> Registration:
    return Registration(
        ways=tuple(
            Way(invite=way) if isinstance(way, InviteWay) else Way(signup=way) for way in listed
        )
    )


def signup(**conditions: Any) -> SignupWay:
    return SignupWay(**conditions)


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


def test_the_default_way_lets_anyone_sign_up_for_an_organiser_to_approve() -> None:
    found = qualify(Registration(), emails=(), code=None, invited=False)

    assert (found.way, found.kind, found.name, found.approval, found.eligibility) == (
        0,
        WayKind.SIGNUP,
        None,
        MANUAL,
        {},
    )


def test_a_contest_with_no_way_takes_nobody() -> None:
    with pytest.raises(RegistrationClosed, match="Nobody can register"):
        qualify(Registration(ways=()), emails=(), code=None, invited=True)


def test_the_invite_way_needs_an_accepted_invite_and_approves_straight_away() -> None:
    rules = ways(InviteWay())

    with pytest.raises(InviteRequired) as refused:
        qualify(rules, emails=(), code=None, invited=False)

    assert refused.value.code == "invite_required"
    found = qualify(rules, emails=(), code=None, invited=True)
    assert (found.kind, found.approval, found.eligibility) == (
        WayKind.INVITE,
        AUTO,
        {"invited": True},
    )


@pytest.mark.parametrize("given", ["", "Sesame", "sesame "])
def test_a_code_has_to_be_the_contests_own(given: str) -> None:
    rules = ways(signup(code="sesame"))

    with pytest.raises(WrongInviteCode) as refused:
        qualify(rules, emails=(), code=given, invited=False)

    assert refused.value.code == "wrong_invite_code"
    assert qualify(rules, emails=(), code="sesame", invited=False).eligibility == {"code": True}


def test_a_code_left_out_is_asked_for() -> None:
    with pytest.raises(WrongInviteCode, match="asks for a code"):
        qualify(ways(signup(code="sesame")), emails=(), code=None, invited=False)


@pytest.mark.parametrize(
    ("email", "passes"),
    [
        ("ada@u.nus.edu", True),
        ("Ada@U.NUS.EDU", True),
        ("ada@comp.u.nus.edu", False),
        ("ada@u.nus.edu.evil.test", False),
        ("u.nus.edu@evil.test", False),
        ("ada@nus.edu", False),
        ("not-an-address", False),
    ],
)
def test_a_domain_is_the_whole_part_after_the_last_at_whatever_its_case(
    email: str, passes: bool
) -> None:
    assert domain_matches("u.nus.edu", email) is passes
    rules = ways(signup(domains=("u.nus.edu",)))
    if passes:
        assert qualify(rules, emails=(email,), code=None, invited=False).eligibility == {
            "email": email
        }
    else:
        with pytest.raises(DomainNotAllowed) as refused:
            qualify(rules, emails=(email,), code=None, invited=False)
        assert refused.value.code == "domain_not_allowed"


def test_an_address_the_forge_never_confirmed_matches_no_domain() -> None:
    # Only confirmed addresses reach the rules: someone whose student
    # address is unconfirmed has none.
    with pytest.raises(DomainNotAllowed):
        qualify(ways(signup(domains=("u.nus.edu",))), emails=(), code=None, invited=False)


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
    rules = ways(signup(email_pattern=r".*@u\.nus\.edu"))

    emails = (email,) if email is not None else ()
    if passes:
        assert qualify(rules, emails=emails, code=None, invited=False).eligibility == {
            "email": email
        }
    else:
        with pytest.raises(DomainNotAllowed):
            qualify(rules, emails=emails, code=None, invited=False)


def test_every_condition_of_a_sign_up_holds_with_one_address() -> None:
    rules = ways(
        signup(code="sesame", domains=("u.nus.edu",), email_pattern=r"e\d+@.*", approval=AUTO)
    )

    with pytest.raises(WrongInviteCode):
        qualify(rules, emails=("e1@u.nus.edu",), code="wrong", invited=False)
    with pytest.raises(DomainNotAllowed):
        qualify(rules, emails=("ada@u.nus.edu", "e1@x.test"), code="sesame", invited=False)
    found = qualify(rules, emails=("ada@u.nus.edu", "e1@u.nus.edu"), code="sesame", invited=False)
    assert found.eligibility == {"code": True, "email": "e1@u.nus.edu"}


def test_the_most_lenient_way_that_admits_wins_and_among_equals_the_first() -> None:
    rules = ways(
        signup(name="Anyone", approval=MANUAL),
        InviteWay(approval=AUTO),
        signup(name="Students", domains=("u.nus.edu",), approval=AUTO),
    )

    invited = qualify(rules, emails=("ada@u.nus.edu",), code=None, invited=True)
    student = qualify(rules, emails=("ada@u.nus.edu",), code=None, invited=False)
    anyone = qualify(rules, emails=("ada@x.test",), code=None, invited=False)

    assert (invited.way, invited.kind, invited.approval) == (1, WayKind.INVITE, AUTO)
    assert (student.way, student.name, student.approval) == (2, "Students", AUTO)
    assert (anyone.way, anyone.name, anyone.approval) == (0, "Anyone", MANUAL)


def test_the_refusal_says_what_would_help() -> None:
    coded = ways(InviteWay(), signup(code="sesame", domains=("u.nus.edu",)))
    students = ways(InviteWay(), signup(domains=("u.nus.edu",)))

    # The right code would let a student in.
    with pytest.raises(WrongInviteCode):
        qualify(coded, emails=("ada@u.nus.edu",), code=None, invited=False)
    # No code would let someone in at another address.
    with pytest.raises(DomainNotAllowed):
        qualify(coded, emails=("eve@x.test",), code="sesame", invited=False)
    with pytest.raises(DomainNotAllowed):
        qualify(students, emails=("eve@x.test",), code=None, invited=False)
    with pytest.raises(InviteRequired, match="only the people it invites"):
        qualify(ways(InviteWay()), emails=("eve@x.test",), code="sesame", invited=False)


def test_a_code_is_asked_for_only_when_a_code_would_let_the_person_in() -> None:
    rules = ways(InviteWay(), signup(code="sesame", domains=("u.nus.edu",)))

    assert asks_code(rules, emails=("ada@u.nus.edu",), invited=False)
    assert not asks_code(rules, emails=("ada@u.nus.edu",), invited=True)
    assert not asks_code(rules, emails=("eve@x.test",), invited=False)
    assert not asks_code(ways(signup(code="sesame"), signup()), emails=(), invited=False)
    assert asks_code(ways(signup(code="sesame")), emails=(), invited=False)
    assert not asks_code(Registration(), emails=(), invited=False)


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
    rules = ways(signup(email_pattern=r".*@u\.nus\.edu"))

    found = qualify(rules, emails=("ada@example.test", "ada@u.nus.edu"), code=None, invited=False)

    assert found.eligibility == {"email": "ada@u.nus.edu"}


def test_a_pattern_that_takes_too_long_lets_nobody_through_and_holds_nobody_up() -> None:
    started = time.monotonic()

    assert not matches(r"(x+x+)+y", "x" * 200)
    assert not matches(r".*", "x" * (EMAIL_MAX + 1))
    assert not domain_matches("x.test", "a@" + "x" * EMAIL_MAX + ".test")
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
