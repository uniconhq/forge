"""Sign-in: the redirect carries the checks, a real answer produces a session
for the right user, and a tampered state or nonce is refused.
"""

from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from forge.context import Context
from forge.domain.errors import SessionExpired, SignInInvalid
from forge.forges.fake import FakeForge
from forge.services import identity, sign_in
from forge.services.sign_in import SignInAttempt


def _answer(fake: FakeForge, started: sign_in.SignInStart) -> tuple[str, str]:
    query = parse_qs(urlsplit(fake.consent_redirect(started.url)).query)
    return query["code"][0], query["state"][0]


def test_the_redirect_carries_the_challenge_and_state_and_keeps_the_rest(
    fake: FakeForge,
) -> None:
    started = sign_in.start(fake, "/contests/4")

    query = parse_qs(urlsplit(started.url).query)
    assert query["state"] == [started.attempt.state]
    assert query["nonce"] == [started.attempt.nonce]
    assert query["code_challenge"] != [started.attempt.verifier]
    assert started.attempt.next == "/contests/4"
    assert sign_in.start(fake, "https://evil.test").attempt.next == "/"


async def test_a_real_answer_produces_a_session_for_the_right_user(
    ctx: Context, fake: FakeForge
) -> None:
    started = sign_in.start(fake, "/contests/4")
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


@pytest.mark.parametrize("tampered", ["state", "nonce", "missing"])
async def test_a_tampered_answer_is_refused_and_leaves_no_session(
    ctx: Context, fake: FakeForge, tampered: str
) -> None:
    started = sign_in.start(fake, "/")
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


async def test_a_spent_code_is_refused(ctx: Context, fake: FakeForge) -> None:
    started = sign_in.start(fake, "/")
    code, state = _answer(fake, started)
    await sign_in.complete(
        ctx, code=code, state=state, attempt=started.attempt, ip=None, user_agent=None
    )

    with pytest.raises(SignInInvalid):
        await sign_in.complete(
            ctx, code=code, state=state, attempt=started.attempt, ip=None, user_agent=None
        )


async def test_a_credential_the_forge_refuses_ends_the_session_whatever_the_caller_does(
    ctx: Context, fake: FakeForge
) -> None:
    """The revocation is written in a transaction of its own, so the caller
    rolling its unit of work back, which is what a raise makes it do, does
    not bring the session back.
    """
    started = sign_in.start(fake, "/")
    code, state = _answer(fake, started)
    session, _ = await sign_in.complete(
        ctx, code=code, state=state, attempt=started.attempt, ip=None, user_agent=None
    )
    await ctx.db.commit()
    fake.state.revoke_credentials(7)

    with pytest.raises(SessionExpired):
        await identity.whoami(ctx, session)
    await ctx.db.rollback()

    with pytest.raises(SessionExpired):
        await identity.current(ctx, session.id)
