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


class RegistrationRefused(ServiceError):
    """A registration the contest's rules turn away. Each rule has an error of
    its own below, and its `code` says which, so a page can say what happened.
    """


class RegistrationClosed(RegistrationRefused):
    """The contest's registration window is not open."""

    code = "registration_closed"


class IsStaff(RegistrationRefused):
    """The user holds a role at the contest, at one of its tasks or at its org,
    and nobody is an organiser and a contestant of one contest.
    """

    code = "is_staff"


class AlreadyRegistered(RegistrationRefused):
    """The user already has a registration for the contest, whatever became of
    it.
    """

    code = "already_registered"


class InviteRequired(RegistrationRefused):
    """The contest is invite-only and the user has no accepted invite to it."""

    code = "invite_required"


class WrongInviteCode(RegistrationRefused):
    """The contest asks for a code and the one given, if any, is not it."""

    code = "wrong_invite_code"


class DomainNotAllowed(RegistrationRefused):
    """The user's email address, or its absence, does not match the contest's
    pattern.
    """

    code = "domain_not_allowed"


class ContestFull(RegistrationRefused):
    """Every place the contest's capacity allows is taken."""

    code = "contest_full"


class WrongStatus(ServiceError):
    """A decision the registration's status does not allow, such as approving
    one already rejected. `current` is the status it has.
    """

    code = "wrong_status"


class InvalidReason(ServiceError):
    """A rejection without a reason the contestant can read, or with one longer
    than the limit.
    """

    code = "invalid_reason"


class InvalidExtension(ServiceError):
    """A time extension below nothing or above the most one may be."""

    code = "invalid_extension"


class SubmitRefused(ServiceError):
    """An upload or a submit the task's rules turn away before anything is
    written. Each rule has an error of its own below, and its `code` says
    which, so a page can say what happened and, where one applies, the limit.
    """


class TaskClosed(SubmitRefused):
    """The task takes no submissions from this contestant now: the contest's
    end plus their own extension has passed, or its organisers closed
    submissions. `reason` says which, `ended` or `submissions_closed`.
    """

    code = "task_closed"


class Archived(SubmitRefused):
    """The contest is archived: its tasks are kept for reading and take no
    submissions.
    """

    code = "archived"


class NotApproved(SubmitRefused):
    """The person is not an approved contestant of the task's contest."""

    code = "not_approved"


class WorkspaceNotReady(SubmitRefused):
    """The contestant's place to submit the task is still being made."""

    code = "workspace_not_ready"


class TooLarge(SubmitRefused):
    """A file, or the submission as a whole, is larger than the task allows.
    `limit` is the most allowed in bytes, and `input` the input whose limit it
    is, or none for the task's limit on a whole submission.
    """

    code = "too_large"


class UploadNotReady(SubmitRefused):
    """An upload named is not a complete, checked file that no submission has
    used yet: its bytes have not all arrived, what arrived is not what was
    declared, or it was submitted already. `uploads` lists each id refused.
    """

    code = "upload_not_ready"


class UploadLimit(SubmitRefused):
    """The person holds as many uploads for the task as one person may before
    a submit uses them: `limit` of them, or `bytes` declared by them
    together. An upload stops counting once a submit uses it, and goes two
    days after it was asked for.
    """

    code = "upload_limit"


class InvalidInputs(SubmitRefused):
    """What was given does not fit the task's contestant inputs. `errors`
    lists each problem as `{"input", "message"}`, `input` being the id of
    the input it is about.
    """

    code = "invalid_inputs"
