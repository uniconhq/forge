"""A session is the platform's own record that a user is signed in. It has two
lifetimes, a hard limit from creation and an idle limit from last use, and the
first to pass ends it.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

TOUCH_INTERVAL = timedelta(minutes=1)
REFRESH_MARGIN = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Session:
    """The session as the rest of the platform sees it. It carries no secret."""

    id: uuid.UUID
    user_id: int
    username: str
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class SessionTimes:
    expires_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None


def is_expired(times: SessionTimes, now: datetime, idle_ttl: timedelta) -> bool:
    if times.revoked_at is not None:
        return True
    if now >= times.expires_at:
        return True
    return now - times.last_seen_at > idle_ttl


def needs_touch(last_seen_at: datetime, now: datetime) -> bool:
    return now - last_seen_at >= TOUCH_INTERVAL


def refresh_due(credential_expires_at: datetime, now: datetime) -> bool:
    return credential_expires_at - now <= REFRESH_MARGIN


def is_fresh(created_at: datetime, now: datetime, window: timedelta) -> bool:
    return now - created_at <= window
