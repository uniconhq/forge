"""Events the forge pushes to the platform. Each org's push is signed with
that org's own secret: the forge sends the hex HMAC-SHA256 of the raw body
in `X-Forgejo-Signature`, and `check` compares the two as bytes in constant
time, so a signature holding characters no digest has is refused as wrong,
like any other. An org with no secret is refused the same way, so the door
says nothing to an unauthenticated caller about which orgs exist. What to do
with an event is a later feature; this is the door, and `EVENTS_PATH` and
`SIGNATURE_HEADERS` are where it is and what the host reads the signature
from.
"""

import hashlib
import hmac

from forge.domain.errors import Forbidden, NotFound
from forge.domain.ids import OrgId
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import org_accounts

log = get_logger(__name__)

EVENTS_PATH = "/api/v1/events/forge"
"""Where the platform takes an org's events, under its internal URL, followed
by the org's name."""
SIGNATURE_HEADERS = ("X-Forgejo-Signature", "X-Gitea-Signature")
"""The headers the forge sends a signature in, under its two names."""


@action
async def check(ctx: Context, org: OrgId, body: bytes, signature: str) -> None:
    """Refuse the event with `Forbidden` unless `signature` is the org's
    secret over `body`, including for an org that has no secret.
    """
    try:
        secret = await org_accounts.event_secret_of(ctx, org)
    except NotFound:
        log.info("events.refused", org=org, reason="unknown_org")
        raise Forbidden("The event's signature does not match.") from None
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest().encode()
    if not hmac.compare_digest(expected, signature.strip().lower().encode()):
        log.info("events.refused", org=org, reason="signature")
        raise Forbidden("The event's signature does not match.")
