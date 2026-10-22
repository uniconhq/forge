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


class VariablesDiffer(Rejected):
    """The CI asked about a run started with variables other than those the
    platform starts it with, so it is answered with nothing.
    """

    code = "variables_differ"


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
    """The user is a member of an entrant in the contests in `contests`,
    pending or approved, and nobody is an organiser and a contestant of one
    contest.
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
    """An action that asks first was sent without its confirmation, and
    nothing changed; the detail is the words to show. A release of a task's
    results, or a take-back, carries `action`, which one. A save during a
    running contest would publish. `changes` lists what
    would change how the task grades, in words a person reads, `regrades`
    how many submissions the save would grade again, for a run task once
    its save is checked, and `results_change`, `summary`, `runs_again`,
    `scores` and `reading` what it does (`domain.save_effect`), and `asked`
    the head and latest publication it was worked out from; the same save
    sent again with the confirmation and `asked` publishes it while neither
    has moved.
    """

    code = "confirmation_required"


class NotARunTask(ServiceError):
    """An action only a run task has, such as releasing its results, asked
    of a task that is not one.
    """

    code = "not_a_run_task"


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
    """The user is in an entrant of the contest already, whatever became of
    it.
    """

    code = "already_registered"


class InviteRequired(RegistrationRefused):
    """No way of the contest's lets the user in, and the only ones that would
    are for people an organiser invited.
    """

    code = "invite_required"


class WrongInviteCode(RegistrationRefused):
    """A sign-up of the contest's would let the user in with its code, and the
    one given, if any, is not it.
    """

    code = "wrong_invite_code"


class DomainNotAllowed(RegistrationRefused):
    """No confirmed email address of the user's, or none at all, is one the
    contest's sign-ups take.
    """

    code = "domain_not_allowed"


class ContestFull(RegistrationRefused):
    """Every place the contest's capacity allows is taken: as many entrants
    as it holds are pending or approved.
    """

    code = "contest_full"


class InvalidMessage(ServiceError):
    """An announcement's or a clarification's title or text that is empty or
    too long. `field` names which.
    """

    code = "invalid_message"


class WrongStatus(ServiceError):
    """A decision the thing's status does not allow, such as approving a
    registration already rejected or cancelling a grading that is done.
    `current` is the status it has.
    """

    code = "wrong_status"


class InvalidReason(ServiceError):
    """A rejection, or a grading's cancel, without a reason the contestant
    can read, or with one longer than the limit.
    """

    code = "invalid_reason"


class InvalidExtension(ServiceError):
    """A time extension below nothing or above the most one may be."""

    code = "invalid_extension"


class InvalidMark(ServiceError):
    """A mark entered for a value that takes none in the task's live
    publication, or that is not a number within the bounds its `entered`
    block gives.
    """

    code = "invalid_mark"


class SubmitRefused(ServiceError):
    """An upload or a submit the task's rules turn away before anything is
    written. Each rule has an error of its own below, and its `code` says
    which, so a page can say what happened and, where one applies, the limit.
    """


class TaskClosed(SubmitRefused):
    """The task takes no submissions from this row now: the task's close
    plus the row's extension on it has passed. `reason` is `closed`.
    """

    code = "task_closed"


class TaskWithdrawn(SubmitRefused):
    """The task was withdrawn from its contest, so it takes no submissions;
    its organisers may put it back.
    """

    code = "task_withdrawn"


class TakeOutChanged(ServiceError):
    """Taking a task out of its contest would now do the other thing than the
    one confirmed: someone submitted to it since, so it can no longer be
    removed, only withdrawn, or the other way. `submissions` is how many it
    has now, and `take_out` what taking it out would now do.
    """

    code = "take_out_changed"


class TakeOutRefused(ServiceError):
    """A task cannot be taken out of its contest by the platform as things
    stand, for the reason in `detail`: a leaderboard names it, or the
    contest's settings are written in a way that cannot be edited line by
    line.
    """

    code = "take_out_refused"


class Archived(SubmitRefused):
    """The contest is archived: its tasks are kept for reading and take no
    submissions.
    """

    code = "archived"


class NotApproved(SubmitRefused):
    """The person is in no approved entrant of the task's contest."""

    code = "not_approved"


class SubmissionLimit(SubmitRefused):
    """The contestant has made every submission the task allows. `limit` is
    how many that is.
    """

    code = "submission_limit"


class RateLimited(SubmitRefused):
    """The contestant submitted as often as the task's rate allows within its
    window. `rate` is the rate, such as `1 per 30s`, and `retry_at` the first
    instant another submission is taken, in ISO 8601.
    """

    code = "rate_limited"


class TooLarge(SubmitRefused):
    """A file, or the submission as a whole, is larger than the task allows.
    `limit` is the most allowed in bytes, and `input` the input whose limit it
    is, or none for the task's limit on a whole submission.
    """

    code = "too_large"


class UploadNotYours(SubmitRefused):
    """An upload named is not one this person made for this task. One that
    does not exist is answered the same way, so the answer says nothing
    about anyone else's. `uploads` lists each id refused.
    """

    code = "upload_not_yours"


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


class LogTooLarge(ServiceError):
    """A run log is larger than the platform reads back for an organiser,
    which no harness writes. `limit` is the most shown, in bytes.
    """

    code = "log_too_large"


class PublicFileTooLarge(ServiceError):
    """A file under a task's `public/` is larger than the platform serves
    of it to a reader. `limit` is the most served, in bytes.
    """

    code = "public_file_too_large"


class InvalidIdempotencyKey(ServiceError):
    """A submit's idempotency key is missing or not the short random text a
    browser makes once per submit.
    """

    code = "invalid_idempotency_key"


class CiRequestRefused(ServiceError):
    """A request the CI made about a run that the platform will not answer:
    its signature does not verify, it names no grading, or the grading it
    names is not one to start now. The detail says no more than that; the
    reason goes to the log.
    """

    code = "ci_request_refused"


class InvalidToken(ServiceError):
    """A grading run's report carries no token, or not the token of the
    grading it is for. A token of another grading is refused the same way.
    """

    code = "invalid_token"


class GradingClosed(ServiceError):
    """The grading takes no more from its run: it is not the run's to report
    on now, because it finished or was cancelled, or its deadline passed.
    """

    code = "grading_closed"


class InvalidCallback(ServiceError):
    """A grading run's report is not one: not a JSON object, an event this
    platform does not take, or a progress report without its counts.
    """

    code = "invalid_callback"


class InvalidInvite(ServiceError):
    """An invite that cannot be made as asked: naming both a username and an
    address or neither, an address that is not one, a lifetime out of range,
    or a grant that does not fit its scope.
    """

    code = "invalid_invite"


class AlreadyInvited(ServiceError):
    """The same person already holds a pending invite for the same grant at the
    same scope, which can be sent again instead.
    """

    code = "already_invited"


class InviteLimit(ServiceError):
    """An invite made or mailed again too often: an org past its invites for
    the day, or an invite mailed again within minutes. `retry_at` is when to
    ask again, in ISO 8601.
    """

    code = "invite_limit"


class InviteExpired(ServiceError):
    """The invite's lifetime is over, so it can be neither accepted nor
    declined.
    """

    code = "invite_expired"


class EntrantsAlone(ServiceError):
    """The contest's entrants are one person each (`entrants.most` is 1), so
    an entrant has no name and nobody invites anyone into one.
    """

    code = "entrants_alone"


class InvalidEntrantName(ServiceError):
    """An entrant's name that is empty, too long, or not printable."""

    code = "invalid_entrant_name"


class EntrantNameRequired(ServiceError):
    """The contest's entrants may be more than one person, so a new one needs
    a name.
    """

    code = "entrant_name_required"


class EntrantNameTaken(ServiceError):
    """Another entrant of the contest has that name, ignoring case."""

    code = "entrant_name_taken"


class EntrantFull(ServiceError):
    """The entrant's members and the people invited into it already reach
    what the contest's settings allow. `limit` is that many.
    """

    code = "entrant_full"


class InEntrant(ServiceError):
    """The person is in an entrant of the contest already and cannot leave it
    for another: its roster has locked, it has submitted, or it was turned
    away; or they are a member of the entrant named already.
    """

    code = "in_entrant"


class RosterLocked(ServiceError):
    """The entrant's roster has locked, at the contest's start or at the
    entrant's first submission as the contest says, or the contest has
    ended: from then on only organisers change who is in it.
    """

    code = "roster_locked"


class EntrantIncomplete(ServiceError):
    """The entrant has fewer members than the contest's `entrants.fewest`, so
    it is neither approved nor takes submissions. `fewest` is how many it
    needs and `members` how many it has.
    """

    code = "entrant_incomplete"


class NoInviteWay(ServiceError):
    """An organiser's invite to a contest whose `registration.ways` has no
    `invite` way, which nobody could come in by.
    """

    code = "no_invite_way"


class EntrantHasSubmissions(ServiceError):
    """An entrant that has submitted is kept with its results, so it is not
    deleted, and the last member of one does not move out of it.
    """

    code = "entrant_has_submissions"


class EntrantChanged(ServiceError):
    """The person's entrant changed while what they asked for was being done,
    so it is asked again.
    """

    code = "entrant_changed"


class MarksOff(ServiceError):
    """No board with `select: marked` covers the task, so it takes no marks."""

    code = "marks_off"


class MarksFrozen(ServiceError):
    """The row's close on the task has passed, so its marks are frozen."""

    code = "marks_frozen"


class MarkLimit(ServiceError):
    """The row holds as many marks on the task as it may. `limit` is that
    many.
    """

    code = "mark_limit"
