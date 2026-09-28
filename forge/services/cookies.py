"""What goes into the two cookies the hosting process sets, and what comes
out of them. The session cookie carries a session id and nothing else, signed
under `UNICON_SESSION_SIGNING_KEY` so a forged id is refused without a
database read. The sign-in cookie carries what checks a sign-in's answer, for
the few minutes a sign-in takes. The key never leaves the package: the host
puts the string this module makes into a header, hands back the string it
reads from one, and names and flags the cookies itself.
"""

import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta

from itsdangerous import BadSignature, URLSafeTimedSerializer

from forge.domain.sessions import Session
from forge.runtime.context import ActionSetup
from forge.runtime.held import setup_or_held
from forge.services.sign_in import SignInAttempt
from forge.settings import Settings

SESSION_SALT = "unicon-session"
SIGN_IN_SALT = "unicon-sign-in"


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
    try:
        raw = _serializer(settings, SESSION_SALT).loads(
            value, max_age=_seconds(settings.session_hard_ttl)
        )
        return uuid.UUID(hex=str(raw))
    except BadSignature, ValueError:
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
    try:
        payload = _serializer(settings, SIGN_IN_SALT).loads(
            value, max_age=_seconds(settings.sign_in_ttl)
        )
        return SignInAttempt(**payload)
    except BadSignature, TypeError, ValueError:
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


def _serializer(settings: Settings, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_signing_key_bytes, salt=salt)


def _seconds(lifetime: timedelta) -> int:
    return int(lifetime.total_seconds())
