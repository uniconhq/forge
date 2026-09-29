"""Every error the package raises to its callers. Each carries a stable `code`
a client can switch on and a `detail` written for a person; `extra` holds any
structured members of the refusal.

`PortError` is what the port fails with, in five ways and no others:
`NotFound`, `Forbidden`, `Conflict`, `Rejected` and `Unavailable`.
`Misconfigured` is a `Rejected` with its own code, because the platform's own
registration being refused is not something a user can act on. `NotReady`
is what a readiness check fails with. Everything else is a `ServiceError`,
raised by the services.
"""

from typing import Any


class UniconError(Exception):
    code = "error"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra: dict[str, Any] = extra


class PortError(UniconError):
    """A failure of the host behind the port."""


class NotFound(PortError):
    """The thing named does not exist."""

    code = "not_found"


class Forbidden(PortError):
    """The identity the call was made under may not do this."""

    code = "forbidden"


class Conflict(PortError):
    """The thing already exists, or a write started from a version that has
    since moved.
    """

    code = "conflict"


class Rejected(PortError):
    """The host refused with a reason of its own, passed through in `detail`."""

    code = "rejected"


class Misconfigured(Rejected):
    """The host refused the platform itself rather than the user: a wrong
    client registration or redirect.
    """

    code = "forge_misconfigured"


class Unavailable(PortError):
    """The host did not answer, after the retries the implementation makes."""

    code = "forge_unavailable"


class NotReady(UniconError):
    """The database did not answer in time. The cause goes to the log and not
    into the error, so nothing that asks learns what failed underneath.
    """

    code = "not_ready"


class ServiceError(UniconError):
    """A refusal by the package's own rules."""


class InvalidName(ServiceError):
    """A name breaks the character or length rule."""

    code = "invalid_name"


class Unauthenticated(ServiceError):
    """No session was presented, or the one presented names no row."""

    code = "unauthenticated"


class SessionExpired(ServiceError):
    """The session has passed one of its two lifetimes, was revoked, or can no
    longer act at the host.
    """

    code = "session_expired"


class FreshSignInRequired(ServiceError):
    """The action needs a session created within the fresh sign-in window."""

    code = "fresh_sign_in_required"


class SignInInvalid(ServiceError):
    """The sign-in attempt did not start here, has expired, or its state or
    nonce does not match.
    """

    code = "sign_in_invalid"


class SignInDenied(ServiceError):
    """The user declined on the host's consent page."""

    code = "sign_in_denied"


class SoleAdmin(ServiceError):
    """The user is the only admin of the scopes in `scopes`, so the account
    cannot be removed until someone else holds each of them.
    """

    code = "sole_admin"


class ContestantConflict(ServiceError):
    """The user is a contestant in the contests in `contests`, pending or
    approved, and nobody is an organiser and a contestant of one contest.
    """

    code = "contestant_conflict"


class SharedWorkflowOwner(ServiceError):
    """The user owns the shared or public workflows in `workflows`, which other
    people may be using.
    """

    code = "shared_workflow_owner"


class InvalidPath(ServiceError):
    """A file path that is not a plain relative path inside a contest or a
    task: empty, absolute, with an empty, `.` or `..` segment, or with a
    character a URL or git reads as something else. `path` names it.
    """

    code = "invalid_path"


class ReservedPath(ServiceError):
    """A save writes inside `plans/`, where only the compiler writes. `paths`
    lists each path it tried.
    """

    code = "reserved_path"


class AdminOnly(ServiceError):
    """A manager's save changes settings that belong to the scope's admin.
    `keys` names each one: a top-level key of the settings file, or a whole
    file such as `statement.md`.
    """

    code = "admin_only"


class ConfirmationRequired(ServiceError):
    """A save during a running contest would change how the task grades.
    `changes` lists what would change, in words a person reads; the same save
    sent again with the confirmation publishes it.
    """

    code = "confirmation_required"
