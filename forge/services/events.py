"""Events the forge pushes to the platform. Each org's push is signed with
that org's own secret: the forge sends the hex HMAC-SHA256 of the raw body
in `X-Forgejo-Signature`, and `check` compares the two as bytes in constant
time, so a signature holding characters no digest has is refused as wrong,
like any other. An org with no secret is refused the same way, so the door
says nothing to an unauthenticated caller about which orgs exist.
`EVENTS_PATH`, `SIGNATURE_HEADERS` and `KIND_HEADERS` are where the door is
and what the host reads the signature and the event's kind from.

The host answers the push as soon as `check` passes, so the forge never
waits on the work behind it, and then hands the same body to `publish`,
which reads what changed through the port and nudges the streams that may
hear it (`live`): a clarification its asker and the organisers who observe
its contest; an announcement of a contest that contest's contestants and
organisers; and one of a task the task's organisers, and its contestants
only once the task is released, so nobody learns of a task they cannot see.
An event about anything but a thread, or about a place in another org than
the one whose secret signed it, nudges nobody.
"""

import hashlib
import hmac

from forge.domain import release as rules
from forge.domain.errors import Forbidden, NotFound, PortError
from forge.domain.ids import OrgId
from forge.domain.live import Nudge, NudgeKind
from forge.domain.names import UserOwner
from forge.domain.roles import contest_scope, task_scope
from forge.domain.threads import ThreadChange, ThreadKind
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import org_accounts, published

log = get_logger(__name__)

EVENTS_PATH = "/api/v1/events/forge"
"""Where the platform takes an org's events, under its internal URL, followed
by the org's name."""
SIGNATURE_HEADERS = ("X-Forgejo-Signature", "X-Gitea-Signature")
"""The headers the forge sends a signature in, under its two names."""
KIND_HEADERS = ("X-Forgejo-Event", "X-Gitea-Event")
"""The headers the forge names an event's kind in, under its two names."""


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


@action
async def publish(ctx: Context, org: OrgId, kind: str, body: bytes) -> None:
    """Nudge whoever may hear of what a push that `check` let in says
    changed. Anything it cannot read is passed over: the push was answered
    already, and there is nobody to tell.
    """
    change = ctx.forge.threads.read_event(kind, body)
    if change is None:
        return
    if contest_scope(change.contest).org != org:
        log.warning("events.other_org", org=org, contest=change.contest)
        return
    nudge = await _nudge(ctx, change)
    if nudge is not None:
        ctx.nudge(nudge)


async def _nudge(ctx: Context, change: ThreadChange) -> Nudge | None:
    if change.kind is ThreadKind.CLARIFICATION:
        asker = change.asker.user_id if isinstance(change.asker, UserOwner) else None
        return Nudge(
            NudgeKind.CLARIFICATION,
            change.thread,
            user=asker,
            scope=contest_scope(change.contest),
        )
    if change.task is None:
        return Nudge(
            NudgeKind.ANNOUNCEMENT,
            change.thread,
            scope=contest_scope(change.contest),
            contest=change.contest,
        )
    return Nudge(
        NudgeKind.ANNOUNCEMENT,
        change.thread,
        scope=task_scope(change.task),
        contest=change.contest if await _released(ctx, change) else None,
    )


async def _released(ctx: Context, change: ThreadChange) -> bool:
    """Whether the change's task is released now, so its contestants may
    hear of it. A forge that does not answer says no.
    """
    assert change.task is not None
    try:
        settings = await published.contest(ctx, change.contest)
        found = await published.task(ctx, change.task, settings)
    except PortError:
        return False
    return found is not None and rules.visible(settings, found.definition, ctx.now)
