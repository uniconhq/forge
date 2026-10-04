"""What happens when a step of making an org, a contest or a task fails.

The person is told in fixed words: the forge's own words name its hosts,
its paths and the keys things are filed under, so they go to the log, and
the person is handed the same kind of error with a fixed sentence for its
code (`failure`).

What the try already made at the forge and the CI is removed again before
the error goes back, the latest first (`Made`). Each create notes a thing
the moment it may be removed, with the id or key the step answered with or
the try itself made, and never looks one up by its name, so an undo removes
only what this try made. It is best effort: a removal that fails, often
because the forge is down, which may be why the step failed, is logged as
`<subject>.undo_left` with what was left, the rest still run, and the
person gets the step's own error. The undo runs whenever the unit of work
rolls back, so a commit that fails after every step worked undoes them too.
A process that dies halfway leaves what it made, under keys no name points
at.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from forge.domain.errors import Conflict, Forbidden, Misconfigured, NotFound, PortError, Rejected
from forge.log import get_logger
from forge.runtime.context import Context

log = get_logger(__name__)

SAID: dict[type[PortError], str] = {
    Misconfigured: "The forge refused the platform's own registration.",
    Conflict: "The forge already holds something by that name; try again.",
    NotFound: "The forge did not find something this needs; try again.",
    Forbidden: "The forge refused this; try again, or tell the operator.",
    Rejected: "The forge refused this; try again, or tell the operator.",
}
NO_ANSWER = "The forge or the CI did not answer; try again in a moment."


def failure(exc: PortError, event: str, **fields: str) -> PortError:
    """The error to raise for `exc`: its own class, in fixed words. What the
    forge said goes to the log as `event`.
    """
    log.warning(event, error=type(exc).__name__, detail=exc.detail, **fields)
    for kind, sentence in SAID.items():
        if isinstance(exc, kind):
            return type(exc)(sentence)
    return type(exc)(NO_ANSWER)


type Removal = Callable[[], Awaitable[None]]


@dataclass
class Made:
    """What one try at making something has made so far, each thing with its
    kind, the key or id it is known by, and how to remove it. `subject` names
    the log events, `orgs.undo_left` and `orgs.undone` for an org, and
    `fields` go on each of them.
    """

    subject: str
    fields: dict[str, str]
    removals: list[tuple[str, str, Removal]] = field(default_factory=list)

    def add(self, kind: str, key: str, remove: Removal) -> None:
        """Note one more thing the try made, removed before anything noted
        earlier.
        """
        self.removals.append((kind, key, remove))

    async def undo(self) -> None:
        """Remove everything noted, the latest first. One that fails is
        logged with its kind and key and the rest still run; when none fails
        that is logged once.
        """
        left = 0
        for kind, key, remove in reversed(self.removals):
            try:
                await remove()
            except Exception as exc:
                left += 1
                detail = exc.detail if isinstance(exc, PortError) else None
                log.warning(
                    f"{self.subject}.undo_left",
                    kind=kind,
                    key=key,
                    error=type(exc).__name__,
                    detail=detail,
                    **self.fields,
                )
        if self.removals and not left:
            log.info(f"{self.subject}.undone", removed=len(self.removals), **self.fields)


async def place(made: Made, kind: str, key: str, call: Awaitable[object], remove: Removal) -> None:
    """Make a contest's or a task's place and note it in `made`. Making it
    and writing its starter files are two steps at the forge, so a call that
    fails may have left the place behind, and it is noted all the same; a
    removal of a place not there changes nothing. Only `Conflict`, a place
    already there that this call did not make, is never noted.
    """
    try:
        await call
    except Conflict:
        raise
    except BaseException:
        made.add(kind, key, remove)
        raise
    made.add(kind, key, remove)


def undo_on_rollback(ctx: Context, subject: str, **fields: str) -> Made:
    """A record of what this try makes, removed again if the unit of work
    rolls back.
    """
    made = Made(subject, fields)
    ctx.after_rollback(made.undo)
    return made
