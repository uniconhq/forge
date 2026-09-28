"""The two cookies' contents: a signed session id that authenticates, and is
refused when altered, signed under another key or too old; the sign-in
attempt round-trips; a cookie signed before the signing moved into the
package still reads; and the policy follows the settings.
"""

import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer

from forge import cookies
from forge.domain.sessions import Session
from forge.services.sign_in import SignInAttempt
from forge.settings import Settings
from forge.setup import Setup
from forge.testing import CALLBACK_PATH

OTHER_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAE"

MakeSetup = Callable[..., Setup]


@pytest.fixture
async def setup_of() -> AsyncIterator[MakeSetup]:
    """Setups over the test settings with `overrides`, none of which touches
    a database, all stopped when the test ends.
    """
    built: list[Setup] = []

    def make(**overrides: Any) -> Setup:
        made = Setup.build(Settings.for_tests(**overrides), callback_path=CALLBACK_PATH)
        built.append(made)
        return made

    try:
        yield make
    finally:
        for made in built:
            await made.stop()


def _session() -> Session:
    now = datetime.now(UTC)
    return Session(
        id=uuid.uuid7(),
        user_id=7,
        username="ada",
        created_at=now,
        expires_at=now + timedelta(days=30),
        last_seen_at=now,
    )


def test_a_signed_session_cookie_authenticates(setup_of: MakeSetup) -> None:
    setup, session = setup_of(), _session()

    value = cookies.session_value(session, setup=setup)

    assert cookies.session_id(value, setup=setup) == session.id


def test_an_altered_or_empty_session_cookie_is_refused(setup_of: MakeSetup) -> None:
    setup = setup_of()
    value = cookies.session_value(_session(), setup=setup)
    altered = value[:-3] + ("aaa" if not value.endswith("aaa") else "bbb")

    assert cookies.session_id(altered, setup=setup) is None
    assert cookies.session_id("", setup=setup) is None
    assert cookies.session_id(None, setup=setup) is None


def test_a_cookie_under_another_key_is_refused(setup_of: MakeSetup) -> None:
    value = cookies.session_value(_session(), setup=setup_of(session_signing_key=OTHER_KEY))

    assert cookies.session_id(value, setup=setup_of()) is None


def test_a_session_cookie_older_than_the_hard_lifetime_is_refused(
    setup_of: MakeSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = setup_of(session_hard_ttl=timedelta(hours=1), session_idle_ttl=timedelta(hours=1))
    value = cookies.session_value(_session(), setup=setup)
    later = time.time() + timedelta(hours=2).total_seconds()

    monkeypatch.setattr(time, "time", lambda: later)

    assert cookies.session_id(value, setup=setup) is None


def test_a_cookie_signed_before_the_signing_moved_here_still_reads(setup_of: MakeSetup) -> None:
    session_id = uuid.uuid7()
    earlier = URLSafeTimedSerializer(
        Settings.for_tests().session_signing_key_bytes, salt="unicon-session"
    )

    assert cookies.session_id(earlier.dumps(session_id.hex), setup=setup_of()) == session_id


def test_the_sign_in_cookie_round_trips_the_attempt(setup_of: MakeSetup) -> None:
    setup = setup_of()
    attempt = SignInAttempt(state="s", verifier="v", nonce="n", next="/contests/4")

    value = cookies.sign_in_value(attempt, setup=setup)

    assert cookies.sign_in_attempt(value, setup=setup) == attempt
    assert cookies.sign_in_attempt("garbage", setup=setup) is None
    assert cookies.sign_in_attempt(None, setup=setup) is None


def test_a_session_cookie_is_not_a_sign_in_cookie(setup_of: MakeSetup) -> None:
    setup = setup_of()

    assert (
        cookies.sign_in_attempt(cookies.session_value(_session(), setup=setup), setup=setup) is None
    )


def test_the_policy_follows_the_settings(setup_of: MakeSetup) -> None:
    plain = cookies.policy(setup=setup_of(public_url="http://localhost:8080"))
    served = cookies.policy(setup=setup_of(public_url="https://unicon.example.test"))

    assert plain == cookies.CookiePolicy(
        secure=False, session_max_age=30 * 24 * 3600, sign_in_max_age=600
    )
    assert served.secure is True
