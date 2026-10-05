"""What a live update is: a nudge naming one thing that changed by its kind
and id, never what changed, and who may hear it. A page that hears one asks
for the thing through the ordinary routes, which check the caller as for any
other request; the stream is never a second way to read anything.

Who may hear a nudge is decided before it is sent, and conservatively: the
one person it concerns (`user`), or the members of the one team it
concerns (`team`), the organisers who observe `scope` or a broader one, and
the contestants of `contest`, each as their session's `Audience` holds
them. Anyone else hears nothing of it.

A nudge travels between processes as a short JSON object through
Postgres's `NOTIFY` on `CHANNEL`, whose payloads are limited to 8000 bytes;
a nudge is under two hundred.
"""

import json
from dataclasses import dataclass
from enum import StrEnum

from forge.domain.ids import ContestId
from forge.domain.roles import Role, RoleGrant, Scope, holds

CHANNEL = "unicon_live"


class NudgeKind(StrEnum):
    """What a nudge names: a grading, an announcement, or a clarification.
    `resync` tells a page that nudges may have been missed, so it should
    refetch what it shows.
    """

    GRADING = "grading"
    ANNOUNCEMENT = "announcement"
    CLARIFICATION = "clarification"
    RESYNC = "resync"


@dataclass(frozen=True, slots=True)
class Nudge:
    kind: NudgeKind
    id: str
    user: int | None = None
    scope: Scope | None = None
    contest: ContestId | None = None
    team: str | None = None

    def payload(self) -> str:
        return json.dumps(
            {
                "k": self.kind.value,
                "i": self.id,
                "u": self.user,
                "t": self.team,
                "s": _scope_path(self.scope),
                "c": self.contest,
            },
            separators=(",", ":"),
        )


def read_payload(payload: str) -> Nudge | None:
    """The nudge a payload carries, or none for one that does not read as a
    nudge, which is passed over rather than trusted.
    """
    try:
        found = json.loads(payload)
        kind = NudgeKind(found["k"])
        id_ = found["i"]
        user = found.get("u")
        scope = found.get("s")
        contest = found.get("c")
        team = found.get("t")
    except ValueError, KeyError, TypeError:
        return None
    if not isinstance(id_, str) or (user is not None and not isinstance(user, int)):
        return None
    if scope is not None and not isinstance(scope, str):
        return None
    if contest is not None and not isinstance(contest, str):
        return None
    if team is not None and not isinstance(team, str):
        return None
    return Nudge(
        kind=kind,
        id=id_,
        user=user,
        scope=_scope_of(scope) if scope else None,
        contest=ContestId(contest) if contest else None,
        team=team or None,
    )


@dataclass(frozen=True, slots=True)
class Audience:
    """Who one session's stream speaks to: the person, every role they hold,
    the contests where they are an approved contestant, and the teams they
    are a member of, read when the stream opens and again every few minutes
    while it stays open.
    """

    user_id: int
    grants: tuple[RoleGrant, ...]
    contests: frozenset[ContestId]
    teams: frozenset[str] = frozenset()


def hears(audience: Audience, nudge: Nudge) -> bool:
    """Whether a session with `audience` may be sent `nudge`."""
    if nudge.kind is NudgeKind.RESYNC:
        return True
    if nudge.user is not None and nudge.user == audience.user_id:
        return True
    if nudge.team is not None and nudge.team in audience.teams:
        return True
    if nudge.scope is not None and holds(audience.grants, nudge.scope, Role.OBSERVER):
        return True
    return nudge.contest is not None and nudge.contest in audience.contests


def _scope_path(scope: Scope | None) -> str | None:
    return None if scope is None else scope.path


def _scope_of(path: str) -> Scope | None:
    parts = path.split("/")
    if not 1 <= len(parts) <= 3 or not all(parts):
        return None
    return Scope(*parts)
