"""Every error the package raises to its callers. Each carries a stable `code`
a client can switch on and a `detail` written for a person; `extra` holds any
structured members of the refusal.

The port fails in five ways and no others: `NotFound`, `Forbidden`,
`Conflict`, `Rejected` and `Unavailable`. Every implementation behind the port
raises only those, and the services raise the rest.
"""

from typing import Any


class UniconError(Exception):
    code = "error"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra: dict[str, Any] = extra


class NotFound(UniconError):
    """The thing named does not exist at the forge or in a table."""

    code = "not_found"


class Forbidden(UniconError):
    """The identity the call was made under may not do this."""

    code = "forbidden"


class Conflict(UniconError):
    """The thing already exists, or a write started from a version that has
    since moved.
    """

    code = "conflict"


class Rejected(UniconError):
    """The forge refused with a reason of its own, passed through in `detail`."""

    code = "rejected"


class Unavailable(UniconError):
    """The forge did not answer, after the retries the implementation makes."""

    code = "forge_unavailable"


class Unauthenticated(UniconError):
    """No session was presented, or the one presented names no row."""

    code = "unauthenticated"


class SessionExpired(UniconError):
    """The session has passed one of its two lifetimes, was revoked, or can no
    longer act at the forge.
    """

    code = "session_expired"


class FreshSignInRequired(UniconError):
    """The action needs a session created within the fresh sign-in window."""

    code = "fresh_sign_in_required"


class SignInInvalid(UniconError):
    """The sign-in attempt did not start here, has expired, or its state or
    nonce does not match.
    """

    code = "sign_in_invalid"


class SignInDenied(UniconError):
    """The user declined on the forge's consent page."""

    code = "sign_in_denied"


class Misconfigured(UniconError):
    """The forge refused the platform itself rather than the user: a wrong
    client registration or redirect.
    """

    code = "forge_misconfigured"


class SoleAdmin(UniconError):
    """The user is the only admin of the scopes in `scopes`, so the account
    cannot be removed until someone else holds each of them.
    """

    code = "sole_admin"


class SharedWorkflowOwner(UniconError):
    """The user owns the shared or public workflows in `workflows`, which other
    people may be using.
    """

    code = "shared_workflow_owner"
