"""Sign-in through the host's OpenID Connect provider. `start` builds the
redirect and what checks the answer; `complete`, an action, exchanges the
code the host sends back, checks the state and the nonce, creates the
session, and ends the one the browser had before, together. `sign_up_url`
is where a person creates an account at the host.
"""

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from hmac import compare_digest

from forge.domain.errors import Forbidden, SignInInvalid
from forge.domain.next_path import safe_next
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import ActionSetup, Context
from forge.runtime.held import held
from forge.services import sessions

log = get_logger(__name__)

STATE_BYTES = 32
VERIFIER_BYTES = 32
NONCE_BYTES = 16
STATE_PREFIX = 8


@dataclass(frozen=True, slots=True)
class SignInAttempt:
    """What the caller keeps between the redirect and the answer, in a signed
    short-lived cookie: the verifier, the state, the nonce and where to land.
    """

    state: str
    verifier: str
    nonce: str
    next: str


@dataclass(frozen=True, slots=True)
class SignInStart:
    url: str
    attempt: SignInAttempt


def start(next_candidate: str | None, *, setup: ActionSetup | None = None) -> SignInStart:
    forge = (setup or held()).forge
    attempt = SignInAttempt(
        state=secrets.token_urlsafe(STATE_BYTES),
        verifier=_new_verifier(),
        nonce=secrets.token_urlsafe(NONCE_BYTES),
        next=safe_next(next_candidate),
    )
    url = forge.identity.sign_in_url(
        state=attempt.state, code_challenge=_challenge(attempt.verifier), nonce=attempt.nonce
    )
    return SignInStart(url=url, attempt=attempt)


def sign_up_url(*, setup: ActionSetup | None = None) -> str | None:
    """Where a person creates an account at the host, or none while sign-up
    is closed.
    """
    return (setup or held()).forge.identity.sign_up_url()


@action
async def complete(
    ctx: Context,
    *,
    code: str,
    state: str,
    attempt: SignInAttempt | None,
    ip: str | None,
    user_agent: str | None,
    previous_session_id: uuid.UUID | None = None,
) -> tuple[Session, str]:
    """Finish a sign-in and return the new session and where to land. The
    session the browser already had, `previous_session_id`, is ended in the
    same transaction, so the new one is never saved while the old one stays
    working. A mismatched state or nonce is refused, no session is created
    and the previous one is left as it was.
    """
    if attempt is None:
        raise SignInInvalid("This sign-in did not start here, or took too long.")
    if not compare_digest(attempt.state.encode(), state.encode()):
        log.info("sign_in.refused", reason="state", state=state[:STATE_PREFIX])
        raise SignInInvalid("This sign-in did not start here.")
    try:
        signed = await ctx.forge.identity.complete_sign_in(code=code, verifier=attempt.verifier)
    except Forbidden as exc:
        log.info("sign_in.refused", reason="code", state=state[:STATE_PREFIX])
        raise SignInInvalid("This sign-in could not be completed.") from exc
    if signed.nonce is not None and not compare_digest(signed.nonce, attempt.nonce):
        log.info("sign_in.refused", reason="nonce", state=state[:STATE_PREFIX])
        raise SignInInvalid("This sign-in did not start here.")
    session = await sessions.create(
        ctx, user=signed.user, credential=signed.credential, ip=ip, user_agent=user_agent
    )
    if previous_session_id is not None:
        await sessions.revoke(ctx, previous_session_id)
    log.info("sign_in.completed", user_id=signed.user.id, session=str(session.id))
    return session, attempt.next


def _new_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(VERIFIER_BYTES)).decode().rstrip("=")


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")
