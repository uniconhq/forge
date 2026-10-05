"""Sign-in: the redirect carries the checks, a real answer produces a session
for the right user and ends the one the browser had, and a tampered state or
nonce is refused.
"""

from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from forge.domain.errors import SessionExpired, SignInInvalid
from forge.domain.sessions import Session
from forge.forges.fake import FakeForge
from forge.runtime.context import Context
from forge.runtime.setup import Setup
from forge.services import identity, sessions, sign_in
from forge.services.sign_in import SignInAttempt


def _answer(fake: FakeForge, started: sign_in.SignInStart) -> tuple[str, str]:
    query = parse_qs(urlsplit(fake.consent_redirect(started.url)).query)
    return query["code"][0], query["state"][0]


async def _signed_in(ctx: Context, fake: FakeForge) -> Session:
    session = await sessions.create(
        ctx, user=fake.users[7], credential=fake.mint(7), ip=None, user_agent=None
    )
    await ctx.db.commit()
    return session


def test_the_redirect_carries_the_challenge_and_state_and_keeps_the_rest(setup: Setup) -> None:
    started = sign_in.start("/contests/4", setup=setup)

    query = parse_qs(urlsplit(started.url).query)
    assert query["state"] == [started.attempt.state]
    assert query["nonce"] == [started.attempt.nonce]
    assert query["code_challenge"] != [started.attempt.verifier]
    assert started.attempt.next == "/contests/4"
    assert sign_in.start("https://evil.test", setup=setup).attempt.next == "/"


def test_the_sign_up_url_is_the_hosts_while_sign_up_is_open(setup: Setup, fake: FakeForge) -> None:
    assert sign_in.sign_up_url(setup=setup) == "http://forge.test/user/sign_up"
    fake.identity.sign_ups_open = False
    assert sign_in.sign_up_url(setup=setup) is None


def test_the_forge_url_is_the_hosts_whether_or_not_sign_up_is_open(
    setup: Setup, fake: FakeForge
) -> None:
    assert sign_in.forge_url(setup=setup) == "http://forge.test"
    fake.identity.sign_ups_open = False
    assert sign_in.forge_url(setup=setup) == "http://forge.test"


async def test_a_real_answer_produces_a_session_for_the_right_user(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    started = sign_in.start("/contests/4", setup=setup)
    code, state = _answer(fake, started)

    session, landing = await sign_in.complete(
        ctx, code=code, state=state, attempt=started.attempt, ip=None, user_agent=None
    )
    await ctx.db.commit()
    me = await identity.whoami(ctx, session)

    assert landing == "/contests/4"
    assert session.user_id == 7
    assert me.user.name == "Ada Lovelace"
    assert me.degraded is False


async def test_a_sign_in_ends_the_session_the_browser_had(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    previous = await _signed_in(ctx, fake)
    started = sign_in.start("/", setup=setup)
    code, state = _answer(fake, started)

    session, _ = await sign_in.complete(
        setup,
        code=code,
        state=state,
        attempt=started.attempt,
        ip=None,
        user_agent=None,
        previous_session_id=previous.id,
    )

    with pytest.raises(SessionExpired):
        await identity.current(previous.id, setup=setup)
    assert (await identity.current(session.id, setup=setup)).user_id == 7


@pytest.mark.parametrize("tampered", ["state", "nonce", "missing"])
async def test_a_tampered_answer_is_refused_and_leaves_no_session(
    setup: Setup, ctx: Context, fake: FakeForge, tampered: str
) -> None:
    started = sign_in.start("/", setup=setup)
    code, state = _answer(fake, started)
    attempt: SignInAttempt | None = started.attempt
    if tampered == "state":
        state = "not-this-attempt"
    elif tampered == "nonce":
        attempt = replace(started.attempt, nonce="other")
    else:
        attempt = None

    with pytest.raises(SignInInvalid):
        await sign_in.complete(
            ctx, code=code, state=state, attempt=attempt, ip=None, user_agent=None
        )
    assert fake.calls_to("complete_sign_in") == [] or tampered == "nonce"


async def test_a_refused_sign_in_leaves_the_previous_session_working(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    previous = await _signed_in(ctx, fake)
    started = sign_in.start("/", setup=setup)
    code, _ = _answer(fake, started)

    with pytest.raises(SignInInvalid):
        await sign_in.complete(
            setup,
            code=code,
            state="not-this-attempt",
            attempt=started.attempt,
            ip=None,
            user_agent=None,
            previous_session_id=previous.id,
        )

    assert (await identity.current(previous.id, setup=setup)).id == previous.id


async def test_a_spent_code_is_refused(setup: Setup, ctx: Context, fake: FakeForge) -> None:
    started = sign_in.start("/", setup=setup)
    code, state = _answer(fake, started)
    await sign_in.complete(
        ctx, code=code, state=state, attempt=started.attempt, ip=None, user_agent=None
    )

    with pytest.raises(SignInInvalid):
        await sign_in.complete(
            ctx, code=code, state=state, attempt=started.attempt, ip=None, user_agent=None
        )


async def test_a_credential_the_forge_refuses_ends_the_session_whatever_the_caller_does(
    setup: Setup, ctx: Context, fake: FakeForge
) -> None:
    """The revocation is written once the caller's unit of work has ended, on
    a unit of work of its own, so the caller rolling back, which is what a
    raise makes it do, does not bring the session back.
    """
    started = sign_in.start("/", setup=setup)
    code, state = _answer(fake, started)
    session, _ = await sign_in.complete(
        ctx, code=code, state=state, attempt=started.attempt, ip=None, user_agent=None
    )
    await ctx.db.commit()
    fake.state.revoke_credentials(7)

    with pytest.raises(SessionExpired):
        await identity.whoami(setup, session)

    with pytest.raises(SessionExpired):
        await identity.current(session.id, setup=setup)
