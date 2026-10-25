"""The gradings of submissions: the rows a submit, a retry, a rejudge and the
operator's reconcile make, starting each one's run at the CI, what a run of
one is in the port's words, and the organiser's controls over them.

A grading is one row per submission and attempt, and nothing about
one is ever edited into another: a retry and a rejudge make new attempts,
each a new row with a new id and so new secrets, and the old rows stay as
they were, so what was graded when stays readable. Each row is inserted
`queued` with the SHA-256 of its run's callback token, and its run is
started as soon as the unit of work that made it commits (`start`), since
a CI that asks may ask the platform about the grading while the start is
under way. A
start the CI refuses, or does not answer, ends the grading in
`system_error` saying why: whoever reads it sees that at once and tries
again, a contestant by submitting, an organiser with `retry`.

A grading whose run the CI has lost reads as a system error saying so,
like an overdue one, and nothing is written when it is read: `lost` asks
the CI where the runs of `dispatched` gradings are, once they have waited
long enough for that to mean something (`grading.worth_asking`), at most
once every `RUN_STATE_KEPT` for each run however many people are watching
it, and with the reader's connection let go of first.

A latest attempt that ends in `system_error` is tried again once by itself
(`notice`): whatever finds it so, the CI's report, a start that fails, or
any read of it that finds it overdue or lost, starts the retry once its
unit of work commits, in the background (`Context.in_background`), and
nothing runs on a timer. The retry holds the task and the submission's
attempts as a staff retry does, and makes a new attempt, marked
`automatic`, only while the failed one is still the latest and was not
itself made so; so two readers finding the failure at once make one
retry, and a second failure waits for staff. A retry that never starts is
started by the next read that finds the failure.

Of a submission's attempts the latest that finished with a result counts,
on the leaderboards, to its row and against `submissions.max`, until a
newer attempt finishes with one: a failure or a cancel never takes a
result away (`scores.usable`).

A delayed submission, one with no result whose latest attempt reads as a
system error, holds no place under `submissions.max` while it waits, so the
first `max` to get a result are the ones that count: its retry by itself, a
staff retry and a regrade grade it again only while its entrant's
submissions with a result, those waiting that are not delayed, and the
delayed ones made before it are fewer than the most
(`forge.domain.grading.room`), read under the task's hold. Otherwise the
new attempt ends `over_limit` at once, run by nothing, with the most it
reached; the submission then counts on no board and against nothing. One
over the limit is checked again the same way by a staff retry or a
regrade, and a regrade leaves one still without room as it is. No one
grades one past the most; the organisers' list says when a retry would end
over the limit (`retry_limit`), before it runs.

An organiser managing the task reads its gradings and acts on one:

- `cancel` ends a submission whose latest grading reads as a system error,
  stored or because it is overdue or lost while its row still waits, when a
  regrade would only repeat the fault: the grading is `cancelled` with a
  sentence its contestant reads, and a run of it still at the CI is stopped
  once the cancel has committed, so it does not hold a machine. The cancel
  is final: a retry is refused and a rejudge leaves it as it is. A
  submission that never had a result is then void and does not count
  against the task's `submissions.max`; one that had keeps it;
- `retry` makes a new attempt of a submission's latest grading once it is
  finished, against the publication that attempt graded against, unless
  staff cancelled it or another attempt of it is still being graded. One
  that reads as finished only because it is overdue or lost is ended
  first, with the reason written on its row, and the old run is cancelled
  at the CI once the retry has committed, so it does not keep a machine's
  containers going. A delayed or over-the-limit submission is held to its
  entrant's room, as above;
- `rejudge` makes a new attempt of every submission's latest attempt,
  against the task's current publication, as a save that publishes a
  change to how the task grades does (`regrade`).
  A latest attempt still being graded against an older publication is
  cancelled first; one being graded against the current one is left to
  finish, a submission staff cancelled stays cancelled, and delayed and
  over-the-limit ones are held to their entrant's room, the earliest
  first. A rejudge or a save's regrade that makes an attempt is kept as one
  `Regrade`, what started it, who and when, and every attempt it makes,
  and each retry of one of those, names it (`regrade_id`), so what it
  changed can be read (`forge.services.regrades`);
- `run_log` reads a grading's run log, for an organiser observing the
  task: it names every test, hidden ones too, so it is the organisers'
  alone. It is read whole up to `RUN_LOG_MAX` bytes and refused above
  (`log_too_large`), and a store that fails is told in fixed words.

A run task's grading runs as Woodpecker pipelines (`forge.services.pipelines`,
phase 4.9), a row each: the `early` row is the grading as its entrant and
every reader above knows it (`FRONT`), and a `late` row, made when the early
one ends `done`, follows it, told to organisers alone. A row's attempts are
counted on its line, its submission's rows of its pipeline, so a late row is
retried, cancelled and found lost by itself as any row is, and only the
early row decides whether a submission holds a place under
`submissions.max`. Every read of early rows that looks for failures looks
at their submissions' latest late rows too (`notice_late`), since no
entrant reads those. A `save` row checks a publication rather than a
submission; no list here reads it, and its organisers' reads look for its
failures (`pipelines.checks`).

An organiser of a contest reads its gradings together:

- `feed` lists every grading of the contest's tasks the organiser observes,
  newest first by when each was queued (the package's clock, never the
  database's), filtered by task, by the member who submitted, by entrant,
  and by status, each with whose it is, the entrant by the name it is shown
  under and the member who submitted it by username, as `list_gradings` gives a
  task's; every attempt is a row of its own, or, with `latest`, only each
  submission's latest attempt, so `status=system_error` with `latest` lists
  the failed gradings that wait for staff;
- `queue_depth` counts the ones waiting for a machine by status, `queued`
  and `dispatched`, one that is overdue or lost not among them, since it
  reads as a system error; and the ones that failed and wait for staff,
  `failed`: the latest attempts that read as a system error, the ones
  `feed` lists with `status=system_error` and `latest`.

Both see the tasks the organiser observes: every task for an observer of
the contest or above, and otherwise the tasks they hold a role at, the rest
left out. Someone holding no role in the contest is refused.

A grading is named by its id, which is no access control: an organiser who
does not observe the task is told there is no such grading. `task_of` gives
the task a grading is of, so the host's guard can check the organiser there
before a control whose route names only the grading.
"""

from forge.services.gradings.attempt_history import _next_attempt, last_goods, latest_of, record
from forge.services.gradings.controls import cancel, free_user, rejudge, retry
from forge.services.gradings.following import behind, follow, stop
from forge.services.gradings.listing import (
    _is_latest,
    _log_failure,
    _settings,
    _submitters,
    _workspace,
    entries,
    feed,
    in_contest,
    list_gradings,
    narrowed,
    observed_tasks,
    queue_depth,
    run_log,
)
from forge.services.gradings.lookups import (
    _machine_url,
    callback_token_of,
    callback_url,
    changed,
    envelope_key_of,
    envelope_url,
    find,
    finish,
    org_of,
    plan_of,
    publication_of,
    run_of,
    spec_of,
    staff_cancelled,
    told_late,
)
from forge.services.gradings.records import (
    ACCOUNT_NOT_READY,
    CALLBACK_PATH,
    CI_CONFIG_PATH,
    ENVELOPE_PATH,
    LIST_LIMIT,
    LOG_STORE_UNAVAILABLE,
    NO_ANSWER,
    NO_LOG,
    NO_RUN,
    NO_SUCH_GRADING,
    NOT_ACTIVATED,
    NOTICED_KEPT,
    PUBLICATION_GONE,
    REFUSED,
    RUN_STATE_KEPT,
    START_RETRY_WAIT,
    FeedEntry,
    GradingRecord,
    Listed,
    QueueDepth,
    Rejudged,
    Submitter,
)
from forge.services.gradings.regrading import regrade, to_regrade
from forge.services.gradings.room import (
    _over_limit,
    _places,
    _without_room,
    place_of,
    retry_limits,
    task_limit,
    waits_for_room,
)
from forge.services.gradings.row_state import (
    HAS_RESULT,
    _may_read_as,
    _overdue,
    _run_state,
    _worth_asking,
    has_result,
    latest_with_result,
    lost,
    overdue_of,
    previous_with_result,
    status_of,
    task_of,
)
from forge.services.gradings.row_steps import (
    _cancel_after_commit,
    _line,
    _managed,
    _regraded,
    _settle,
)
from forge.services.gradings.starting import (
    _cancel_quietly,
    _not_started,
    _on_line,
    _start_failure,
    _start_on_dispatch,
    _start_pipeline,
    _start_run,
    _start_signed_in,
    _superseded,
    new_row,
    queue_submission,
    start,
)
from forge.services.gradings.watchdog import (
    _first_to_notice,
    _follows_an_earlier_attempt,
    notice,
    notice_late,
    retry_by_itself,
    start_waits,
)

__all__ = [
    "ACCOUNT_NOT_READY",
    "CALLBACK_PATH",
    "CI_CONFIG_PATH",
    "ENVELOPE_PATH",
    "HAS_RESULT",
    "LIST_LIMIT",
    "LOG_STORE_UNAVAILABLE",
    "NOTICED_KEPT",
    "NOT_ACTIVATED",
    "NO_ANSWER",
    "NO_LOG",
    "NO_RUN",
    "NO_SUCH_GRADING",
    "PUBLICATION_GONE",
    "REFUSED",
    "RUN_STATE_KEPT",
    "START_RETRY_WAIT",
    "FeedEntry",
    "GradingRecord",
    "Listed",
    "QueueDepth",
    "Rejudged",
    "Submitter",
    "_cancel_after_commit",
    "_cancel_quietly",
    "_first_to_notice",
    "_follows_an_earlier_attempt",
    "_is_latest",
    "_line",
    "_log_failure",
    "_machine_url",
    "_managed",
    "_may_read_as",
    "_next_attempt",
    "_not_started",
    "_on_line",
    "_over_limit",
    "_overdue",
    "_places",
    "_regraded",
    "_run_state",
    "_settings",
    "_settle",
    "_start_failure",
    "_start_on_dispatch",
    "_start_pipeline",
    "_start_run",
    "_start_signed_in",
    "_submitters",
    "_superseded",
    "_without_room",
    "_workspace",
    "_worth_asking",
    "behind",
    "callback_token_of",
    "callback_url",
    "cancel",
    "changed",
    "entries",
    "envelope_key_of",
    "envelope_url",
    "feed",
    "find",
    "finish",
    "follow",
    "free_user",
    "has_result",
    "in_contest",
    "last_goods",
    "latest_of",
    "latest_with_result",
    "list_gradings",
    "lost",
    "narrowed",
    "new_row",
    "notice",
    "notice_late",
    "observed_tasks",
    "org_of",
    "overdue_of",
    "place_of",
    "plan_of",
    "previous_with_result",
    "publication_of",
    "queue_depth",
    "queue_submission",
    "record",
    "regrade",
    "rejudge",
    "retry",
    "retry_by_itself",
    "retry_limits",
    "run_log",
    "run_of",
    "spec_of",
    "staff_cancelled",
    "start",
    "start_waits",
    "status_of",
    "stop",
    "task_limit",
    "task_of",
    "to_regrade",
    "told_late",
    "waits_for_room",
]
