"""What goes into the two cookies the hosting process sets, and what comes
out of them. The session cookie carries a session id and nothing else, signed
under `UNICON_SESSION_SIGNING_KEY` so a forged id is refused without a
database read. The sign-in cookie carries what checks a sign-in's answer, for
the few minutes a sign-in takes. The key never leaves the package: the host
puts the string this module makes into a header, hands back the string it
reads from one, and names and flags the cookies itself.

A cookie signed up to `CLOCK_SLACK` in the future still reads: one process
may sign it with a clock a second ahead of the one that reads it, or the
machine's clock may be stepped back a moment after it was signed, and a
person whose sign-in was refused for that would see only the sign-in page
again. Every refusal is logged with its reason and never the cookie.
"""

import time
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer

from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.context import ActionSetup
from forge.runtime.held import setup_or_held
from forge.services.sign_in import SignInAttempt
from forge.settings import Settings

log = get_logger(__name__)

SESSION_SALT = "unicon-session"
SIGN_IN_SALT = "unicon-sign-in"
CLOCK_SLACK = timedelta(seconds=2)


@dataclass(frozen=True, slots=True)
class CookiePolicy:
    """How the host marks the two cookies: `Secure` or not, and how many
    seconds each lives.
    """

    secure: bool
    session_max_age: int
    sign_in_max_age: int


def session_value(session: Session, *, setup: ActionSetup | None = None) -> str:
    return _serializer(_settings(setup), SESSION_SALT).dumps(session.id.hex)


def session_id(value: str | None, *, setup: ActionSetup | None = None) -> uuid.UUID | None:
    """The session id a cookie carries, or none when it is empty, forged or
    older than the session's hard lifetime.
    """
    if not value:
        return None
    settings = _settings(setup)
    raw = _read(settings, SESSION_SALT, value, settings.session_hard_ttl, "session")
    try:
        return None if raw is None else uuid.UUID(hex=str(raw))
    except ValueError:
        log.info("cookies.refused", cookie="session", reason="not a session id")
        return None


def sign_in_value(attempt: SignInAttempt, *, setup: ActionSetup | None = None) -> str:
    return _serializer(_settings(setup), SIGN_IN_SALT).dumps(asdict(attempt))


def sign_in_attempt(value: str | None, *, setup: ActionSetup | None = None) -> SignInAttempt | None:
    """The sign-in a cookie carries, or none when it is empty, forged or
    older than a sign-in may take.
    """
    if not value:
        return None
    settings = _settings(setup)
    payload = _read(settings, SIGN_IN_SALT, value, settings.sign_in_ttl, "sign_in")
    try:
        return None if payload is None else SignInAttempt(**payload)
    except TypeError, ValueError:
        log.info("cookies.refused", cookie="sign_in", reason="not a sign-in")
        return None


def policy(*, setup: ActionSetup | None = None) -> CookiePolicy:
    settings = _settings(setup)
    return CookiePolicy(
        secure=settings.secure_cookies,
        session_max_age=_seconds(settings.session_hard_ttl),
        sign_in_max_age=_seconds(settings.sign_in_ttl),
    )


def _settings(setup: ActionSetup | None) -> Settings:
    return setup_or_held(setup).settings


def _read(settings: Settings, salt: str, value: str, lifetime: timedelta, cookie: str) -> Any:
    """What a cookie signed under `salt` carries, or none when it is forged,
    older than `lifetime`, or signed more than `CLOCK_SLACK` from now.
    """
    try:
        payload, signed = _serializer(settings, salt).loads(value, return_timestamp=True)
    except BadSignature:
        log.info("cookies.refused", cookie=cookie, reason="signature")
        return None
    age = time.time() - signed.timestamp()
    if age > lifetime.total_seconds():
        log.info("cookies.refused", cookie=cookie, reason="expired", age=int(age))
        return None
    if age < -CLOCK_SLACK.total_seconds():
        log.warning("cookies.refused", cookie=cookie, reason="signed in the future", age=int(age))
        return None
    return payload


def _serializer(settings: Settings, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_signing_key_bytes, salt=salt)


def _seconds(lifetime: timedelta) -> int:
    return int(lifetime.total_seconds())
