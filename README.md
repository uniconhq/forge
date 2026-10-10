# forge

The Unicon contest API as a Python package, `unicon-forge`. It knows what a
contest is and nothing about HTTP: the domain types, the services, the
database tables and their migrations, the forge port, which is the interface
this package calls the services outside it through (the git host, the CI,
the object store and the mail server), the adapters behind that port, and
the pytest plugin a dependant tests with. The backend is an HTTP shell
over it, pins one release of it, and imports `forge.api` and nothing else.

## Layout

```
forge/
  api/               the front door: everything a hosting process may call
  settings.py        every UNICON_* setting, read once at start
  log.py             the one structured logger; every line is a JSON record
  cli.py             unicon-forge migrate and unicon-forge reconcile
  testing.py         the pytest plugin: a migrated database, the fake, a setup
  runtime/           how a call runs in a process
    context.py       what a building block runs with, and what an action takes
                     from a setup
    actions.py       the @action mark
    held.py          the one setup the process holds
    memo.py          answers the setup keeps for a few seconds
    broker.py        the process's live-update streams and the one connection
                     that listens for the nudges every process publishes
    setup.py         the package set up for one process; start, ready, stop and now
  domain/            the types and their rules, the definition files, the plans,
                     a publication and its note, the release rules,
                     grading's rules and clock,
                     the runner's contract files in schemas/ and the clock;
                     imports nothing else in the package
  port/              the interface the services outside are called through, one
                     area per module, in four groups
  services/          the actions and their building blocks, the cookies and the
                     credential at rest; the only layer that writes to the database
  db/                the tables, the engine and the migrations
  adapters/          one group per service behind the port, which never
                     import each other:
    __init__.py      build, which picks one of each from the settings and
                     joins them into one forge
    git/forgejo/     the git host's areas over Forgejo, and what a CI needs of it
    git/fake/        the git host in memory
    ci/host.py       CiHost, what a CI needs from the git host
    ci/woodpecker/   the CI's areas over Woodpecker, including the sign-in that
                     mints an org account's CI token
    ci/fake.py       the CI in memory
    objects/         the run log store: s3.py, fake.py
    mail/            the mail server: smtp.py, fake.py
    fakes.py         the fakes joined into the forge tests use, in one world
                     (fake_world.py)
    http.py, browser.py, ids.py, cached.py  the shared HTTP client, the
                     browser a sign-in walks with, the naming grammar every
                     git host uses, and the per-read cache
    README.md        what each service must provide to sit behind the port
tests/
  test_*.py          the domain, the front door, the port's shape, the settings,
                     the log, the command and the plugin
  runtime/           the setup, the actions over it, and the memo
  adapters/          each adapter against the port's contract, by group
  services/          the services over a real Postgres and the fake
  db/                the migrations, up and down
  live/              the Forgejo adapter against a running Forgejo, and the
                     organiser's whole path against Forgejo and Woodpecker
```

## The front door

`forge/api/` lists everything a hosting process may call, one module per
area, and the backend imports from there and nowhere else in the package. An
import-linter rule on the backend's side fails its CI when it reaches deeper.

```
forge/api/
  __init__.py   start, stop, ready, now, public_url
  account.py    create, deactivate, delete
  sessions.py   revoke, revoke_all, list_for, and the SessionInfo they return
  identity.py   whoami, current, and the Me whoami returns
  access.py     organiser, organiser_at, the same by the names in an address,
                and the Organiser they return
  names.py      scope_at, the scope the names in an address name, and the
                Named and ScopeNames the records carry
  sign_in.py    start, complete, sign_up_url, forge_url, SignInAttempt, and the
                SignInStart start returns
  orgs.py       create, create_by_operator, update, read, and the
                OrgProfile read returns
  contests.py   create, list, and contest_id_of, the id of the contest a scope
                is in
  tasks.py      create, list, state, standing, the TaskState state returns,
                the TaskStanding and Timeline standing returns, and
                task_id_of, the id of the task a scope names
  workflows.py  create, listing, view, read_version, save, create_version,
                check, set_visibility, share, unshare, copy, combine,
                primitives, and the NewWorkflow, WorkflowSummary,
                WorkflowView, Draft, PrimitiveVersion and Problem they
                return
  files.py      read, tree, history, write, rollback, write_upload, and the
                File, TreeEntry, UploadInfo, EntryKind and Change they return
  publications.py  save, list, workflow_form, and the Published, Draft,
                Publication, WorkflowForm, DeclaredInput and DeclaredField
                they return
  release.py    of_task, and the TaskRelease it returns with its Closed reason
  contestants.py  register, mine, list, approve, reject, reopen, remove, extend, and the
                Registration they return with its Status
  contest_home.py  contests, home, task, and the ContestSummary, ContestHome,
                TaskEntry and TaskPage they return, with the Limits and Rate a
                page carries, the ContestantInput entries and their InputType
                a submit panel is built from, and the State and
                ContestVisibility of a contest
  landing.py    contests, contest, statement, and the PublicContest, PublicTask
                and PublicStatement they return
  events.py     check and publish, EVENTS_PATH, where the door is, and
                SIGNATURE_HEADERS and KIND_HEADERS, the headers the signature
                and the event's kind come in
  announcements.py  post, edit, close, manage, contest, task, and the
                Announcement and AnsweredQuestion they return
  clarifications.py  ask, mine, follow_up, inbox, of_contest, reply, mark,
                unmark, answer_publicly, and the Clarification and Message
                they return
  live.py       stream, the nudges one session hears, and the Nudge and
                NudgeKind it yields
  uploads.py    slot, task_file_slot, complete, door, the Slot a slot is, the
                Upload complete returns with its UploadStatus, and the Door
                the proxy is answered with
  submissions.py  submit, mine, one, files, download, the SubmittedInput
                submit takes, and the Submission, Result, GroupShown, Points,
                SubmittedFiles, GradingStatus and Show they return
  boards.py     seen, organised, held, mark, unmark, written, and the
                Standings, Key, Column, NotInView, Ranked, Cell,
                OrganisedBoard, Marks, Points, UserOwner and TeamOwner they
                return
  gradings.py   cancel, retry, fall_back, clear_fallback, rejudge, list,
                run_log, task_of, feed, queue_depth, and the GradingRecord,
                Rejudged, FeedEntry, Submitter, QueueDepth, GradingStatus and
                Fallback they return; list
                and feed both give FeedEntry
  runs.py       config, envelope, callback, the InboundRequest config takes
                and the InboundAnswer it returns, GradingStatus, and CI_CONFIG_PATH,
                ENVELOPE_PATH and CALLBACK_PATH, where each is served
  roles.py      holders, grant, revoke, and the Holder holders returns
  invites.py    create, at, send_again, withdraw, mine, by_token, accept,
                decline, and the Invite they return
  teams.py      create, request, cancel, leave, invite, approve, remove, the
                organise_ actions, mine, listed, every, and the Team, Member,
                Listed and Mine they return
  cookies.py    what goes into the two cookies and what comes out, and the policy
  log.py        setup, get_logger, and the Logger it returns
  errors.py     every error the package raises to its callers
  types.py      Session, User, Role, Scope, ScopeKind, RoleGrant, HeldRole,
                Named, ScopeNames, OrgId, ContestId, TaskId, VersionId,
                PublicationId, ConflictToken, Edit, Uploaded, Problem, and an
                invite's Grant, InviteStatus and MailStatus
```

Each module only imports names written elsewhere in the package and lists
them in `__all__`, so `from forge.api import sessions` gives `sessions.revoke`
and no way to reach `sessions.create`. `tests/test_api.py` fails when a name
on the list is a building block, a function whose first parameter is a
`Context` and which is not marked `@action`, when a module writes a name
itself instead of re-exporting it, or when a listed function returns a type
the package writes that is not listed too. A new action reaches the backend
only when it is added here, and that one line is the review point.

## The definition files

The format is the proposal's `TASK-FORMAT.md`. `forge/domain/definitions.py`
reads `contest.yaml` and `task.yaml` into models, `workflow_definition.py` a
`workflow.yaml` and `primitives.py` a `primitive.yaml`, with the YAML loading
and the error paths they share in `yaml_models.py` and the six value types in
`types.py`. Each refuses a key the format does not know, refuses a key it no
longer has at that key with the sentence that says what replaced it (a
`Retired` rule), and reports every problem as `InvalidDefinition`, whose
`errors` pair a YAML path such as `test_groups.main.pass_at` with a sentence a
form shows beside that field. What a file says of itself is checked there: a
contest's times and each task entry's timeline in order (C1), each board's
own rules (C3), a task's test groups, rule weights, test weights and `show`
words (T1, T2, T6), a workflow's report (W1 to W5) and a primitive's `runs`
marks (P1). `admin_only_changes` names the admin-only keys a save changes,
`starter_contest` and `starter_task` are the files a new contest or task is
created with, and `starter_workflow` the `workflow.yaml` a new workflow is.
`plans.py` is the compiler (below) and names what changed how a task grades
between two publications. `submissions.py` lays out what a contestant gives
as the files and `submission.json` of one commit, and `uploads.py` holds the
rules of an upload's slot and parts. `grading.py` holds what a grading run
is, its two secrets, its clock and the machine a plan must fit,
`reports.py` what a run reports back and which results are kept,
`exact_json.py` the JSON that keeps a result's numbers exactly as written,
`showing.py` what a contestant is shown of a result and when,
`scoring.py` a result's credit, points and folded values, exact,
`boards.py` a board ranked from scored results, `board_checks.py` what a
board asks of the tasks it covers (T8, C4), and
`contracts.py` checks a document against `schemas/`, a copy of the five
contract files of the runner release the package pins, which the tests
check against the runner's own when that repo is checked out beside this
one. `release.py` works out from `contest.yaml` and the clock whether a task
is released, visible and open to one row, when a submission is late, when a
task reveals, and who sees a contest at all.

## The port

`forge/port/` declares every operation the platform needs from the
services outside it, in the platform's words, as one `Protocol` per area,
in four groups by the service behind them: the git host's (`GitHost`:
`identity`, `orgs`, `content`, `workspaces`, `threads`, `workflows`,
`primitives` and `uploads`), the CI's (`Ci`: `grading` and `computes`),
`objects` and `mail`. `Forge` composes the areas, so a service reaches one
as `ctx.forge.orgs.grant_role(...)`, and each group is filled by an adapter
of its own (`adapters/README.md`). Every reference the package stores is an id
the port hands out, built from keys (below). Above the port only
`domain/roles.py` reads one, turning a contest's or a task's id into the
scope its roles are held at and back; every other id is opaque. Every failure is one of five
typed errors: `NotFound`, `Forbidden`, `Conflict`, `Rejected` and
`Unavailable`. Retries with backoff live inside the adapter, so a forge
that is busy reaches the services only as `Unavailable` once the retries are
used up. A retry never makes something twice: a request that sets a state is
retried on a busy server or a lost answer, and a request that creates
something, a POST, only when it never reached the server; otherwise it is
`Unavailable` at once and the request that sent it fails. A process keeps at
most eight calls in flight to Forgejo and eight to the CI, one connection
each; a call waits its turn for a free connection for 30 seconds at most and
is then `Unavailable`, not asked again. The adapters put every
name they are handed into a request's path quoted whole (`http.segment`), a
username, an org, a repository or a ref, and a file's path segment by
segment (`http.file_path`), so a `/`, `?`, `#` or `%` in one stays in it and
never reaches another endpoint; an empty name, `.` or `..` is `NotFound`
without a request, since a path would resolve it away.

Operations done for a person take the identity the call is made under, so the
host records the change as theirs and enforces their permissions underneath
the platform's own: a submission is committed by the contestant, a person's
roles are read with their own credential, and the platform's token never makes
a change in anyone's name. The host's permission for a role is `write` for
admin and manager alike and `read` for observer, never a repository admin,
since a repository admin may delete a tag's protection; the package holds the
difference between admin and manager. Making orgs, contests, tasks and
workspaces, which includes creating every repository since the host lets no
one else create one, and naming protected versions are done as the platform
account, the account
`UNICON_FORGE_PLATFORM_ACCOUNT` names and the admin token belongs to.
Activating a task at the CI and starting its runs are done as the org's own
account, which the caller hands in as an `AsOrgAccount` carrying that
account's credential at the forge and its state at the CI.

A contest or a task is made bare, with its starter files, by
`content.create_contest` and `content.create_task`, and `content.secure`
gives it the rest: the roles of its contest and, for a task, of the task
itself reach it (Forgejo: the contest's three teams and the task's three
attached to the repository, so a contest's roles reach its tasks), its
history cannot be rewritten (a protected `main`, which Forgejo keeps from
every force-push), and for a task its publications are reserved for the
platform account (the `published/*` tag protection). `secure` checks each
first and puts back only what is missing, so it can be run again, and says
how many it put back. `content.save_files` writes several files as one
change as the person saving, each carrying the token it was read with, and refuses the
whole change as `Conflict` when any has moved; `content.list_files` gives
every file of a place at a version with its token, the same for the same
content, which is how a save compares data files without reading them.
`primitives.read_declaration` reads a primitive's declaration at a version
as the organiser whose save compiles it. `workspaces.record_submission`
writes a submission's files as one change as the contestant, leaving exactly
those files in the place, and names it the next `submission/<n>` as the
platform with a note carrying the submit's idempotency key, taking the next
number when another took its own; `list_submissions` reads each back as a
`Submitted` with its number, version and key, `read_submission_file` one
of its files at its version, big ones included, and `read_submission_blob`
one as the commit holds it, a large file's pointer rather than its bytes
(Forgejo's `raw` endpoint), which is how a recovered submission finds the
uploads it used.
`grading.set_up_org` sets an org up at the CI for its account and gives
back a `CiState`, what the account holds there, which only the
implementation reads; the platform keeps it encrypted, asks
`grading.needs_refresh` before using it, and has it refreshed with
`grading.refresh`, and `grading.tear_down_org` removes it again.
`grading.activate` takes a task for grading at the CI, as the org account,
and says whether the CI did not know it before.
`grading.start_run` starts a `GradingRun`, what one run of one grading is
in the platform's words, as the org account, with a `RunSpec` of what it
runs, read from the plan as it starts, and `grading.cancel_run` stops one.
A CI that asks the platform what a run is as it starts one is answered by
`grading.answer`, which checks the request is the CI's own, has the service
find the run (`lookup`, a queued grading of that task) and refuses a run
started with any variable but those the adapter starts it with; a
CI that is pushed to never asks, and its `answer` is `NotFound`.
`grading.run_places` says what the envelope names of the run's places at
the forge. The Woodpecker adapter writes its side of each (below), and
the runner's README states what any CI owes the machine.
`workspaces.publish` names a version a save already wrote as the next
publication, with a note, and `workspaces.list_publications` reads each back
as a `Publication` with what its note says. A contestant's workspace is made
one part at a time, each of which can be run again and keeps what is
already right: `workspaces.open_workspace` makes the desk and gives the
members read access to it, which at Forgejo lets them post and comment on
issues there and close their own but not edit anyone else's comments or
labels, `workspaces.open_submission_place` makes the
place to submit one task, reserves its submissions for the platform and only
then gives them write access there, so a place whose members can all write it
already is finished and asks nothing more, and `workspaces.close_workspace` takes
the access away and keeps everything in it. `workspaces.workspace_of` names
a contestant's workspace from the contest and the person without a call,
whether any part of it is made or not, so the package stores no workspace
id. `identity.verified_emails` reads the addresses the host
has confirmed are a person's.

A workflow is made the same way, as the platform in its owner's name.
`workflows.create_workflow` makes a person's own under their name, through
Forgejo's administrator route that creates a repository for a user, so they
own it; an org's is made in the org, where the org's three role teams reach
it as they reach every repository there, admin and manager with `write` and
observer with `read`, and nobody else does. The platform protects its `main`
and sets the workflow mark; the person writes its first commit, names its
versions and reads it as themself. Making it public or private, and sharing
it with a named person or taking that away, need a repository admin, which
no organiser's team is, so the platform does them once the forge has said,
to the person's own credential, that they may write it, and so is reading
who it is shared with. `copy_workflow` reads the source's files at a
version as the person and makes the copy the same way. The draft is read
at the head of `main` with the commit that head is, and written with the
token it was read with, so a write over someone else's is `Conflict`; a
version is a tag at the commit the draft was read at, and a read or a copy
at a version looks the name up among the tags first, since Forgejo's `ref`
also takes a branch or a commit, which move. `describe_workflow` reads a
workflow as the person, and one they may not read is `NotFound`;
`workflows_readable_by` is every repository the person reaches, their own,
their orgs' and those shared with them, page by page, and the platform's
public built-ins; other people's public workflows are the marketplace's. Who may change a workflow under an owner, the person themself
or a manager at the org, is the services' rule (`workflows`, below).

The org account is made by the platform too, through more operations the
port declares: `identity.create_user` and `mint_token` make the account at
the host and its credential there, `orgs.ensure_account_membership` puts it
in its place in the org, and `grading.set_up_org` sets it up at the CI with
the password it was just given. Woodpecker admits nobody it was not told
about and mints a token only through its web UI, so the Woodpecker
adapter makes the account's user there as the CI's administrator and then
signs in for real: into the git host with the account's password, through
the git host's own sign-in page, then Woodpecker's OAuth round trip
approving the git host's consent page, reading the CSRF token Woodpecker
hands its own page, and asking for a token. The git host's two pages are
its own adapter's (`CiHost`, below). It runs inside the platform's
process, where the two public URLs do not resolve, so it follows every
redirect by hand and asks each URL at the internal host instead
(`adapters/browser.py`). Its `refresh` finds the account at the git host by
the id the state keeps, gives it a fresh password there and signs in the
same way.

A CI reaches the git host only through `CiHost` (`adapters/ci/host.py`):
the account by its id and a fresh password for it, a repository's id, the
webhooks the CI left on a repository, the branch runs start on, and the
sign-in and consent pages. Forgejo answers it as `ForgejoForge.ci_host`,
and `build` hands it to the CI it picks.

An org's display name and description are its own fields at the host, which
only an owner may change, so `orgs.update_org` writes them and
`orgs.read_org` reads them back as an `OrgProfile`, both as the platform
account (Forgejo: `PATCH` and `GET /orgs/<org>`, the display name kept as
`full_name`). `orgs.update` is the org admin's change, and `orgs.read`
gives the current values to anyone holding a role at the org or at
anything in it.

`orgs.roles_of_user` reads every role one person holds, as the platform
account, for the rules that ask about someone other than the person signed
in. Forgejo lists a user's teams only to that user, so the platform's
administrator token asks for that one listing on their behalf with `sudo`,
one read however many scopes there are. The port tells no person from a
service account: the package knows an org's service account by its id in
`org_accounts`, never by its name, since anyone may sign up under a name that
looks like one.

Two areas carry files, and neither carries their bytes through the
platform.

The `uploads` area (`port/uploads.py`) is where a person's files go: the
address one object's bytes are sent to for a place, as that person, and
whether a place holds an object. The Forgejo adapter addresses its
git-lfs endpoints and presents the person's own access token in the password
half of a Basic header, which is the only form those routes take; Forgejo
hashes what arrives and keeps nothing that is not the digest and length the
address names. The fake holds its objects in memory, refuses the same
things, and resolves a pointer the way Forgejo's media endpoint does.

The `objects` area (`port/objects.py`) is the grading run log store alone: a
URL a machine writes a result with, and reading one up to a size given.
Its adapter, `objects/s3.py`, reaches Garage over S3 with boto3 at
`UNICON_S3_ENDPOINT` and signs what a machine is handed for
`UNICON_MACHINE_URL`, path-style; the proxy passes `/unicon-results/` to
Garage with the Host header unchanged.

The `mail` area (`port/mail.py`) hands one plain-text message to the mail
server the forge sends its own mail through. Its adapter, `mail/smtp.py`,
speaks SMTP
with Python's `smtplib` on a thread, giving each step 15 seconds, to the
one address the message names; a server that does not answer or answers
busy is `Unavailable`, and one that refuses the address or the login, or
whose encryption is not what the settings ask for, is `Rejected`. Without `settings.mail` the area says it
is not configured and sends nothing. The fake keeps what it was handed in
`fake.mail.sent`; `fake.mail.down`, `fake.mail.refusing` and
`fake.mail.configured` stand for a server that does not answer, one that
refuses and none at all.

Every git host's adapter names repositories and builds and reads ids with
the one grammar in `adapters/ids.py`, since the platform names every
repository itself, so an id from elsewhere is `NotFound` whichever is behind
the port.

`UNICON_FORGE=forgejo` runs against Forgejo, grading with the CI `UNICON_CI`
names, `woodpecker` the one there is and the default, each built by its own
adapter and joined by `adapters.build`; `UNICON_FORGE=fake` runs the whole
stack against the fakes (`adapters/fakes.py`), which record every call with
its identity in one log and refuse what a real forge refuses, a repository
made by anyone but the platform among it. The fake CI asks what a run is,
as Woodpecker does, unless it is made with `ci_asks` off, when it is handed
every run whole, as a CI that is pushed to is; a test reads its records as
`fake.ci`. `CachedForge` wraps
either and keeps a few reads in the process for up to a minute, dropping
them on a write through the same area: user lookups always, the role reads
when `UNICON_FORGE_CACHE` is on.

## Names and keys

An org, a contest and a task are each filed under a key that never changes,
26 characters of lower-case base32 made from a UUID v7
(`forge/domain/keys.py`). The key is made when the thing is asked for, and
everything else is built from it: the ids the tables store, `<org>`,
`<org>/<contest>` and `<org>/<contest>/<task>` with each part a key; the
org's name at the forge and its service account's, `unicon-ci-<org key>`;
every repository's name, such as `<contest key>.<task key>.task`; and every
role team's. The Forgejo adapter lists an org's contests and a
contest's tasks through Forgejo's repository search for names holding
`.contest` or `.task`, checking each name it gets back, since the org also
holds a repository for every contestant at every task and reading them all
grew with each. The names people give them are labels in the `names` table,
one row per thing, unique among the things of its kind in the same parent.
So a rename will change one row and move nothing, and a name freed and taken
again names a new thing that inherits no registration, grading or role of
the old one.

`services/names.py` reserves a name on the unit of work of the create that
asks for it, so a create that fails leaves the name free, and turns names
into keys and back. `scope_at(org,
contest, task)` is the one way in from an address: the scope of keys a role
is checked at, labelled with the names for the messages a person reads, or
`NotFound` naming the first part that is not there.
`access.organiser_at` is `organiser` by the names in an address: a part
that is not there is refused with `Forbidden` like a scope the person may not
reach, and is `NotFound` only to someone holding the role above it, so no
route tells anyone else what exists. Every record that is shown to a person
carries its names: a list of contests or tasks as `Named`, a contest's home
and its public page with `where`, a holder's scope with `at_names`, and the
roles `whoami` gives as `HeldRole`s; a host never reads a name out of an id.
A workflow named `<owner>/<name>` has the owner an org by its name, read
from the org's key, the platform's org `unicon` as it is, or a person by
their username, under which the forge keeps their workflows.

A person is filed by their user id at the forge, which never changes and is
never another's; a workspace's owner is `u<user id>`, never a username.

## The setup

`forge/runtime/setup.py` sets the package up for one process: the forge
behind the port, which `adapters.build` picks and configures from the settings,
the pool of database connections and the clock. Nothing runs in the
background: every piece of work is done by the request that asks for it.
The process that hosts the package calls
`forge.api.start(callback_path=...)` once at start. It reads the `UNICON_*`
settings and builds the one setup the process holds. `callback_path` is the
host's own sign-in callback route, the one thing the package cannot know on
its own; the package joins it to `UNICON_PUBLIC_URL`.
`forge.api.public_url()` gives that URL back, for a host that checks where a
request came from. `await forge.api.ready()` raises `NotReady` unless the
database answers within two seconds, on a connection outside the pool; the
cause goes to the log as `setup.not_ready` and not into the error.
`await forge.api.stop()` closes every connection. An action called before
`start` raises an error naming `forge.api.start`.

The settings are all the package's, the host's included: the host reads no
environment variable. `UNICON_SESSION_SIGNING_KEY` signs the two cookies and
never leaves the package. `UNICON_INTERNAL_URL` is where the forge reaches
the platform inside the deployment, `UNICON_PUBLIC_URL` unless given; an
org's event push points there, since the forge is allowed to call only that
host. `UNICON_ORG_CREATION_OPEN`, on by default, lets any signed-in user
create an org; a deployment that opens sign-up to strangers turns it off,
because every org mints a service account and a login at the CI.
`UNICON_COOKIE_SECURE` defaults to on exactly when `UNICON_PUBLIC_URL` is
https, and the package refuses to start when the URL is https and the flag
is set off. The Forgejo settings travel together as
`settings.forgejo`, read from the `UNICON_FORGE_*` variables, Woodpecker's
as `settings.woodpecker`, read from `UNICON_WOODPECKER_URL`,
`UNICON_WOODPECKER_PUBLIC_URL` (the URL unless given) and
`UNICON_WOODPECKER_TOKEN`, and the object store's as `settings.s3`, read from
`UNICON_S3_ENDPOINT`, `UNICON_S3_REGION` (`garage` unless given),
`UNICON_S3_ACCESS_KEY`, `UNICON_S3_SECRET_KEY` and
`UNICON_S3_RESULTS_BUCKET` (`unicon-results`); all three are
required only when `UNICON_FORGE=forgejo`. The mail server invite mail goes
through travels as `settings.mail`, read from `UNICON_MAIL_SMTP_ADDR`,
`UNICON_MAIL_SMTP_PORT` (587 unless given), `UNICON_MAIL_PROTOCOL`
(`smtp+starttls` unless given, or `smtps`, or plain `smtp` for a server on
the deployment's own network), `UNICON_MAIL_SMTP_USER`,
`UNICON_MAIL_SMTP_PASSWORD` and `UNICON_MAIL_FROM`; it is the server the
forge sends its own mail through, and the whole of it is absent while
`UNICON_MAIL_SMTP_ADDR` is empty, when nothing is mailed. `UNICON_MACHINE_URL` is where
grading machines reach the platform, `UNICON_PUBLIC_URL` unless given.
`UNICON_HARNESS_IMAGE` is the harness every plan names, by digest, and
`UNICON_CLONE_IMAGE` the image the CI checks a task and a submission out
with, by digest. Both are required when `UNICON_FORGE=forgejo`, since which
images a deployment runs is its own choice, from its image manifest; the fake
runs neither and names a placeholder digest unless given. A missing or malformed variable stops the process at start with the
variable named.

## Cookies

`forge.api.cookies` makes and reads what the host puts in its two cookies.
`session_value(session)` is the session id signed under
`UNICON_SESSION_SIGNING_KEY`; `session_id(value)` gives the id back, or none
when the value is empty, forged, signed under another key or older than the
session's hard lifetime, so a forged id is refused without a database read.
`sign_in_value(attempt)` and `sign_in_attempt(value)` do the same for what
checks a sign-in's answer, for `UNICON_SIGN_IN_TTL`. A cookie signed up to
two seconds in the future still reads, since another process's clock may
be a second ahead or the machine's may be stepped back after it signed;
every refusal is logged as `cookies.refused` with its reason and never the
value. `policy()` says whether
the cookies are `Secure` and how long each lives. The host keeps the cookie
names, `HttpOnly`, `SameSite` and the `Set-Cookie` header itself, because
those are HTTP.

## Actions and building blocks

A service is a module of functions under `forge/services/`. The functions a
hosting process calls are actions, marked `@action` from
`forge/runtime/actions.py`:

| Module | Actions |
|---|---|
| `sessions` | `revoke`, `revoke_all`, `list_for` |
| `identity` | `whoami`, `current` |
| `access` | `organiser`, `organiser_at` |
| `names` | `scope_at` |
| `account` | `create`, `deactivate`, `delete` |
| `sign_in` | `complete` |
| `orgs` | `create`, `create_by_operator`, `update`, `read` |
| `contests` | `create`, `list` |
| `tasks` | `create`, `list`, `state`, `standing` |
| `files` | `read`, `tree`, `history`, `write`, `rollback`, `write_upload` |
| `publications` | `save`, `list`, `workflow_form` |
| `release` | `of_task` |
| `contestants` | `register`, `mine`, `list`, `approve`, `reject`, `reopen`, `remove`, `extend` |
| `contest_home` | `contests`, `home`, `task` |
| `landing` | `contests`, `contest`, `statement` |
| `events` | `check`, `publish` |
| `announcements` | `post`, `edit`, `close`, `manage`, `contest`, `task` |
| `clarifications` | `ask`, `mine`, `follow_up`, `inbox`, `of_contest`, `reply`, `mark`, `unmark`, `answer_publicly` |
| `roles` | `holders`, `grant`, `revoke` |
| `uploads` | `slot`, `complete` |
| `submissions` | `submit`, `mine`, `one`, `files`, `download` |
| `boards` | `seen`, `organised` |
| `marks` | `held`, `mark`, `unmark` |
| `gradings` | `cancel`, `retry`, `fall_back`, `clear_fallback`, `rejudge`, `list`, `run_log`, `task_of`, `feed`, `queue_depth` |
| `runs` | `config`, `envelope`, `callback` |
| `workflows` | `create`, `listing`, `view`, `read_version`, `save`, `create_version`, `check`, `set_visibility`, `share`, `unshare`, `copy`, `combine` |
| `primitives` | `listing` |

A hosting process reaches them through `forge.api`. An action is one unit of
work. Called as `account.delete(session)`, it opens a transaction on the setup
the process holds, runs, commits when it returns, rolls back when it raises,
and closes the transaction either way. A commit that fails raises out of the
call, so the caller never answers a success for a change that was not saved.
Handed a setup first, as `account.delete(setup, session)`, it opens the
transaction on that setup, which is how a test runs one against a setup of its
own. Handed a `Context` first, as another service calls it, it runs inside the
caller's transaction, so `account.delete` calling `sessions.revoke_all` stays
one transaction. When two writes must succeed or fail together, they are one
action with a name: `sign_in.complete` creates the new session and ends the
one the browser had before, together.

Every other service function is a building block, called only from inside
the package. Its first parameter is a `Context`: the transaction of the
unit of work, the forge, the settings and the clock. A building block never
commits `ctx.db`.

A unit of work holds one pooled connection from its first query until it
ends, and never asks for a second while it holds it: a request that did
would wait on the pool while keeping a connection from it, and a few dozen
at once lock the whole pool up. So the few writes that must land whatever
the action does go to `ctx.after_end(work)`, which runs each on a unit of
work of its own once this one has ended, committed or rolled back, and its
connection is back: noting that a session was used, and ending one whose
credential the forge refused. Refreshing a session's credential, which asks
the forge and then compare-and-sets the stored value, is done before the
route's action opens its unit of work, by `identity.current`, which the
host's guard calls on every request (`sessions.keep_fresh`): each read and
write there is a transaction of its own and none is open while the forge is
asked, and the action then reads a credential good for minutes yet. The
setup holds one lock per session for that refresh, taken as
`ctx.refresh_lock(session_id)`, so two requests in one process refresh it
once. An action that has only read may hand its connection back before a
slow call to the forge with `await ctx.let_go()`; the next query takes one
again in a transaction of its own. One that has written or holds a lock
keeps its connection, so its all or nothing stands. The pages people open
most do this: `/me`, the organiser's guard, the list of contests, a
contest's home and a task's page. `tests/services/test_one_connection.py`
runs the main request paths on a pool of one connection that waits a
second, so a path that nests a checkout fails there at once.

Each process keeps `UNICON_DATABASE_POOL_SIZE` connections (20),
`UNICON_DATABASE_POOL_OVERFLOW` more in a rush (10), and a request that
finds them all taken waits `UNICON_DATABASE_POOL_WAIT` seconds (10) and
fails. The deployment runs one backend process beside Forgejo and
Woodpecker on a Postgres that allows 100 connections, three of them kept
for its superuser; more backend processes keep their pools' sum under what
the others leave. Work that may start only once
the unit of work has committed is handed to `ctx.after_commit(work)`: when
the unit of work commits, `Setup.unit_of_work` runs each piece on a unit of
work of its own, and one that fails goes to the log as
`setup.after_commit_failed`, since what the request did has landed; nothing
runs when it rolls back. Every new grading row leaves the start of its run
there, since the CI asks the platform about the grading while the start is
under way, and a submit also leaves the removal of the upload objects it
used. Its mirror is `ctx.after_rollback(work)`, for undoing what the unit of
work made outside the database: when it rolls back, whether something in it
raised or its commit failed, each piece runs once the transaction is rolled
back, the latest first, before the error goes on; one that fails goes to the
log as `setup.after_rollback_failed` and the error is raised as it was.
Nothing runs when it commits, or when the request is cancelled. A create
leaves the removal of what it made there (below). Work the person who asked
does not wait on is handed to `ctx.in_background(work)`: it starts on a unit
of work of its own once the unit of work commits, and the request answers
without waiting for it. One that fails goes to the log as
`setup.background_failed`; nothing starts when it rolls back; `stop` cuts
short whatever is running, and `Setup.settle()` waits for it, for a test.
Only work that something else finishes when it never runs goes there: the
places made ahead (below), which a contestant's first upload makes itself
when it finds one missing.

Making something at the forge is several calls, and a create makes every
one of them inside the request, on its unit of work, before it answers. A
step that fails fails the request: the transaction rolls back, the row in
`names` with it, and what the try already made at the forge and the CI is
removed again, the latest first, before the person is told; then the person
asks again. What the forge said goes to the log. The removal is best effort
(`services/making.py`). Each create notes a thing the moment it may be
removed, by the id or key the step answered with or the try made itself,
never by looking up a name, so an undo removes only what that try made. A
removal that fails, often because the forge is down, which may be why the
step failed, is logged as `orgs.undo_left`, `contests.undo_left` or
`tasks.undo_left` with the kind and key of what was left, the other
removals still run, and the person gets the step's own error, in the same
fixed words; an undo with nothing left logs `<subject>.undone` once. The
undo runs from `ctx.after_rollback`, so a commit that fails after every step
worked, the database gone away, undoes them as well. A process that dies
halfway leaves what it made under keys no name points at. An org is undone
as its set-up at the CI, its service account's place in the org, the
account, and the org, which takes its roles, labels, event push and first
admin's role with it; the account must leave its place before Forgejo deletes it. A
contest is undone as its place with its own roles. A task is undone as its
entry in `contest.yaml`, taken out only while the file still says exactly
what the create wrote, its activation at the CI, and its place with its own
roles. The port operations the undo uses are `orgs.delete_org`,
`orgs.remove_account_membership`, `identity.delete_user`,
`content.delete_place`, `grading.deactivate` and `grading.tear_down_org`.
A name is one row in `names`, so two requests for one name at once make one
thing and the second is `Conflict`. Each create answers with the `Named`
thing, its id and its name. An org takes ten steps: its account row, the
org, its roles, its labels, its signed event push, its first admin, its
service account at the forge, that account's place in the org and its forge
credential, and its set-up at the CI. The service account's password is
made for the request, mints its forge credential, signs it in at the CI
and is thrown away, so it is never in the database. A service
account name someone already took at the forge is refused rather than
adopted, since anyone may sign up there. The service account is named
from the org's key, `unicon-ci-<key>`, 36 characters, so an org's name
has the same 40-character limit as any other. People and orgs share one namespace at the forge, so
both creates refuse, before anything is written, a name a person or an org
already has there (`orgs.name_taken` through the port). With
`UNICON_ORG_CREATION_OPEN` off, `orgs.create` refuses and the operator runs
`orgs.create_by_operator`, the same steps, naming the first admin.

A contest and a task are made the same way. `contests.create` needs the
manager role at the org and `tasks.create` the manager role at the contest,
which must be there; each checks the name and makes the thing before it
answers. A contest or task asked for with no title, or a blank one, is
titled by its name. A contest takes two steps, its place with a starter
`contest.yaml` that is valid as written and its roles and protection. A task
takes four: its place with `task.yaml`, `statement.md`, an empty `public/`
and one test in `tests/main/1/`; its roles and protection, its publications
reserved for the platform; its activation at the CI as the org's own
account, which takes it for grading, trusts it for `volumes` and nothing
else, and leaves it with no webhook, since the platform starts every run
itself; and its entry at the end of the `tasks` list in `contest.yaml`.
Nothing is published until the first save. A contest's tasks are the tasks
there are at the forge, which `tasks.list` reads as the organiser; the
`tasks` list in `contest.yaml` orders, labels and scores them. The last step
writes the new task's entry as the platform, `{id: <name>}`, its label
being its place in the list, and in a published contest with its
`release_at` and `closes` at the contest's end, into the file's text so its
comments and layout stay (`domain/contest_entries.py`); a `contest.yaml` that does not read, or
a list written in flow style with items in it, is left alone and the task is
made without an entry.

A workflow is made by `workflows.create`, which takes the session, an owner
and a name, and is open to anyone signed in: under their own username, or
under an org where they hold the manager role or above. An observer of the
org may not, and neither may anyone for another person. An owner that is
neither is refused with `Forbidden` in the same words whether an org of
that name is there or not. The owner is read as an org's name first, the
way a `workflow.yaml` reads one, then as the caller's username in any case;
a person's own workflows are named by their username in lower case, and a
username that breaks the name rules then, one with a dot in it, is
`InvalidName`. The name follows the name rules, at most 40 characters. The
port makes the place as the platform and the person writes its first
commit, a `workflow.yaml` with the inputs, test fields, steps and report of
`unicon/classic@v2`, valid as written; the workflow is private. A
workflow's name is not reserved in `names`: the forge holds one place per
owner and name, and a name the owner has already is `Conflict`. Any other
failure at the forge is told in fixed words and logged, like a create's. It
answers with a `NewWorkflow`: its id, and the owner and name a person calls
it by, since an org's id is built from its key.

The same rule says who may change a workflow once it is made: `save`,
`create_version`, `set_visibility`, `share` and `unshare` are the person
it is named for or a manager or admin of its org, and every change is made
as them, so the forge's own check is underneath. Reading is the forge's to
decide, as the person: `view` and `read_version` answer a workflow they may
not read as `NotFound` in the same words as one that is not there.
`listing` is every workflow the person reaches, each saying whether they
may edit it. `view` gives the editor the draft with its token and the
people it is shared with; anyone else gets the workflow without them. A
`save` writes whatever it is given, problems and all, up to 256 KB, since a
draft may stop half done; `Conflict` when the file moved since it was read.
`create_version` reads the draft, runs every check a version must pass
(`check_workflow`, with each `use:` read as the person: one that cannot be
read, or names a workflow, is a problem at its own `steps[n].use`), and
tags the commit it read only when there is none, so a version is never of
a draft with problems and never changes after; given the token the person
saved with, it is `Conflict` when someone has saved since, so a version is
of the save they made. A port with no value is a problem at its own
`steps[n].with.<port>`. `check` runs the same checks
over a text without writing anything, the editor's validate. A workflow is
private, shared or public: shared is private with a list of readers, and
reads back as shared once the list holds someone; `set_visibility` to
private or public empties the list, and `share` is refused while it is
public. `copy` makes a private workflow of the source's files at a version,
read as the caller, with nothing written about where it came from; `combine`
inlines two or more versions into one new private definition
(`domain/workflow_combine.py`): an input or test field an earlier source
declared alike is one, anything else that clashes with an earlier source
takes the first free `-2`, `-3` (`_2` for a reported name), free of every
id already taken and of the source's own others, every reference follows, and the once steps of
every source come before their per-test steps. `primitives.listing` is every
primitive at every version with its declaration, for the editor's palette;
a version in a format no longer read comes with the reason instead.

Who may do what is decided once per request: `access.organiser` checks the
session, reads the person's roles with their own credential, applies the
inheritance rule from `domain/roles.py`, and returns an `Organiser`, or
refuses naming the scope and the role. The other organiser actions take
that `Organiser` and read none of the organiser's roles again.

`roles.holders` lists everyone holding a role at a scope, directly or from a
broader one, each once at their highest role; `roles.grant` and
`roles.revoke` add and remove holders by team membership at the forge, as
the platform account. Each needs manager at the scope, and granting admin,
removing an admin or demoting one needs admin. A person holds one role
directly at a scope, so granting another moves them, granting the new role
before revoking the old; handing a scope over is granting admin to the new
person and then removing the old one. Every change is checked before
anything is written, and the changes to one org's roles happen one after
another: each takes a Postgres advisory lock on the org, held until its
unit of work ends, so two admins stepping down at once cannot each count
the other. Deleting an account takes the same lock for every org the person
has a role in. A scope is never left without an admin, counting
admins of broader scopes, so a contest's only admin may step down while an
org admin stands (`SoleAdmin`). A role at a contest or its tasks is refused
to someone registered in it, and a role at an org to someone registered in
any of its contests (`ContestantConflict`); `roles.holds_role_in_contest`
answers the other direction for registration. An org's service account is
never listed, granted or removed, and a role is changed only at a contest or
task that is there, which `content.exists` asks as the platform.

The functions that need no transaction are plain functions with an optional
`setup=`: `sign_in.start(next)`, `sign_in.sign_up_url()`,
`sign_in.forge_url()`, where a browser reaches the forge's own pages, the cookie
functions, `forge.api.public_url()` and `forge.api.now()`, the clock the
package enforces deadlines with.

## Files

`files.read`, `tree` and `history` need the observer role at the contest or
task, and `write` and `rollback` the manager role; each goes through the
port as the organiser's own identity, so the forge's own check stays
underneath and the history is theirs. Every path is checked first, and one
that is not a plain path inside the place is `InvalidPath`, unread and
unwritten. A write carries the token the file was read with, and one that has moved is `Conflict`. A write to `contest.yaml`
is validated first and refused whole as `InvalidDefinition` when it is not
valid, when an entry of `tasks` names no task of the contest, when it puts a
`worth` or a `due` on a task whose latest publication gives no points, or
when it moves a task's timeline behind what rows already did (C2,
`services/timelines.py`): the release of a task that has opened moved later,
the entry of a task that has opened or has submissions dropped, a `due`
moved earlier than a submission it would make late, a close moved earlier
than a submission already made, or later once the task has revealed. A task
has opened when the settings saved before released it, the contest
published and past its start and the task's `release_at` passed, so a
contest that was never published moves freely. A task reveals once it has
closed for every row, with the longest extension in force on it: an
approved contestant's who is in no team, or a team's with an approved
member. A save of the contest's settings, like a save of one of its tasks,
holds the contest's rules alone until it commits and a mark change holds
them shared (`timelines.hold_rules`), so the checks one save makes against
the other's file, and against the marks rows hold, read what is there. A
manager's change to one of its admin-only keys, `name`,
`description`, `state`, `visibility` and `registration`, is refused as
`AdminOnly`, naming each; nothing is written either way. A write to a task
is a save of the task, below. A rollback reads the file at the chosen
version and writes it back as a new change through `write`, so a task's
rollback is a save too and the history stays whole.

A file an organiser uploaded is put into a task by `write_upload`, a save
whose commit holds the pointer to it (below, Uploads). `tree` lists such a
file with `upload`, the size and SHA-256 of what it holds, and `read` gives
it with the same `upload` beside its content, the pointer, so an editor
never opens one as text; a typed file has none. Forgejo's contents API
gives a file's blob size and nothing about git-lfs, so only a file whose
size a pointer the platform writes could have, 126 to 144 bytes, is read
to tell, and its content checked as such a pointer
(`uploads.may_be_pointer`, `upload_info`); a folder of large or of
ordinary files costs the listing alone.

## The save

There is no publish button. `publications.save(organiser, task, changes)`
takes each path with its new content and the token it was read with, as an
`Edit`, and runs these steps in order, refusing before anything is written:

1. A path inside `plans/` is refused with `ReservedPath`: the compiler is
   the only writer there. A manager's save that changes the `name` or the
   `submissions` of `task.yaml`, or touches `statement.md`, is refused with
   `AdminOnly`, naming each. An admin passes.
2. The state being saved, the files at the head with the save's over them,
   is checked in the order of `TASK-FORMAT.md` section 2: `task.yaml`
   validates; the workflow it names is read at its version as the
   organiser, with every primitive its steps use, and checks as a version,
   one in an old format refused at the `workflow` line so its owner tags a
   new one; the tests are read from `tests/<group>/<test>/` and checked
   against `test_groups`; once the task has a graded submission, a group
   shown `always` or `verdict` under any publication a graded submission
   ran under is not hidden again and a group the save adds says its `show`
   (T7, T10); a task left giving no points is refused at `test_groups`
   while its contest's entry gives it a `worth` or a `due`, naming that
   line; and the plan compiles over the files of that state. When the latest publication used a workflow of the same
   `<owner>/<name>` and that name is now another workflow, by the forge's own
   id for it, the state is refused at that line: the owner may have been
   renamed and the name taken by someone else. A state that fails is a `Draft`: the organiser's
   files are written as one change, nothing is published, and the last
   publication keeps grading. The errors come back with their paths, in
   `task.yaml` or at a test's folder, and are not stored; `tasks.state`
   checks the head again whenever it is not what the latest publication
   froze.
3. What the save changes about how the task grades is worked out against
   the latest publication: its plan and the digests of the task's files the
   plan names. Groups, rule weights, `show`, `credit` and `submissions` are
   read on every read and change nothing that grades. Once the contest has
   started, until it is archived, a save that changes how the task grades is
   refused with `ConfirmationRequired`, listing the changes, and nothing is
   written; the same save with `confirm=True` publishes. A save with
   `keep_as_draft=True` is written as a draft that says what it held back
   and publishes nothing, on any save, valid or not, started contest or not.
4. The organiser's files and `plans/plan.json` are written as one change,
   as the organiser; any other file under `plans/` is removed in the same
   change.
5. That change is named as the next publication, as the platform, with a
   note saying whether it changed how the task grades and what, which
   workflow each workflow name was, by the forge's own id for it, and what
   the task's sealed steps hold back until its reveal. A save that changes
   nothing since the latest publication publishes nothing new. When another
   save landed between the check and the write, the change holds files this
   save never checked, so it comes back as a `Draft` saying so, and the next
   save checks and publishes the task as it then stands.
6. A publication that changed how the task grades regrades every submission
   to the task, as `gradings.rejudge` does: a new attempt of each one's
   latest attempt against it.

`tasks.standing(contest)` is the list an observer of the contest works
its tasks from: every task the contest's `tasks` lists, in that order,
each a `TaskStanding` with its name, its letter by its place, its
`TaskState` as `tasks.state` gives it, the latest publication with its
number, time and `grading_changed`, and whether a draft with errors sits on
it, and its `Timeline` from its entry, each time at its default where the
entry gives none: `worth` 100, none on a task whose latest publication
gives no points or that has none, `release_at` the contest's start,
`due` none, `late_per_day` 1 with a due and none without, and `closes` the
contest's end. A task of the org the contest does not list is not on it. A
`contest.yaml` that does not read is `InvalidDefinition` naming the file
with every problem and its path, as a save of it is refused, so the page
can show what to mend; a contest that is not there is `NotFound`.
Each task costs what `state` does, its head and its publications, a check
of its head when that is a draft, and the `task.yaml` of its latest
publication once per process, for whether it gives points.

A valid save comes back as `Published`, with the publication, its number,
whether it changed how the task grades and what, its notes, the sealed
steps and a bounded value the task's `credit` does not name, and how many
submissions it queued to be graded again. `publications.list`
gives every publication with its flag and its changes, for the task's
history.

`publications.workflow_form(task)` is what the form over `task.yaml` is
built from, for an observer of the task: the workflow the task's
`task.yaml` names as it is saved now, a draft included, read at its
version as the organiser the way a save reads it, as a `WorkflowForm` with
each input it declares (`DeclaredInput`: id, type, whether the contestant
gives it, its options, `per_test`, `optional`) and each test field
(`DeclaredField`: name, type, options), in the workflow's order. A workflow
declares no defaults; a contestant input's `default` is the task's own.
A `task.yaml` that is not there or does not read as YAML, one that names no
workflow or names it wrongly, and a workflow that cannot be read or is in
an old format are answered with `problem`, the reason, and no inputs, so the
form can still be opened to mend them. Either way `graded` says whether the
task has a graded submission, a grading of it `done`, the condition T7 and
T10 hold from: from then on a save refuses a test group it adds without its
`show`, so the form asks for one there instead of offering a default.
`newer` is the workflow's latest version, in natural order, when it comes
after the one the task names, so the task page can say a newer one exists;
the task keeps grading with the one it names until it is saved naming
another.

## The compiler

`forge/domain/plans.py` compiles a task into `plans/plan.json`, the plan
the harness runs, the runner's `plan.schema.json` version 5: the workflow
with the task's values filled in, flat, so nothing of the task's or the
workflow's is read at grade time: the harness image, every test of the task,
the contestant's inputs as the workflow declares them, the steps in order
and the report. `check_workflow` is what making a version checks, needing
only the primitives the steps use: every port given and every value of the
port's type, with the two widenings, a scalar into a text port and a file
into a folder port; every reference to an earlier step and a declared
output; `test.<field>` and per-test inputs only in per-test steps; optional
outputs and inputs only into optional ports; no contestant input or step
output into a port a limit is raised from; and the report's types. Each
`use:` is a primitive; a workflow there is refused as not one. `read_tests`
reads the tests from `tests/<group>/<test>/`, one entry per field the
workflow's `test` block declares, a file named for the field with or
without an ending, a folder named for a folder field, and the scalars in a
`test.yaml`; a test's id is `<group>/<test>`, and the plan lists every test,
groups in name order and tests with numbers compared as numbers.
`compile_plan` binds the task's values to the workflow's inputs (a value for
every input that is neither the contestant's nor optional, form details for
the contestant's), writes the task's and each test's scalars into strings
and a contestant's as a template the harness fills, raises each limit a
value raises and rounds it up, folds a per-test step over a primitive that
takes a batch into one container and writes one entry per test otherwise,
works out which steps are sealed, running the contestant's code over a task
file the contestant is not served, or reading what such a step wrote, and
refuses a task with a sealed step that shows a group before it closes (T5),
checks the plan fits a machine, its whole time within the 25 minutes a run
may take, each step's memory and GPUs within `PLATFORM_MACHINE` and no step
reaching the network, which no machine gives yet, checks `credit` against
the report (T3, T4), and checks the plan against the runner's contract. A
raised limit past what is allowed is refused where its number came from:
the task's input, the `test.yaml` of the test giving the most, or the
`workflow` line when the workflow writes the number itself, saying what
pushed it over ("150 tests at time_limit 2 give the run 26 minutes; a run
may take 25"); a run too long is blamed on the raised time limit that adds
the most, and on the number of tests when none is raised. A test id longer
than 255 characters is refused at its folder.

Each step carries its primitive's image by digest, whether it may
reach the network, its six limits and its declared outputs, a `?` after an
optional one; its container runs the image's own entrypoint. No org holds a
secret yet, so a value given as `{secret: <name>}` is refused naming it.
The plan is the same bytes every time for the same state. A new task's
starter carries one test, so its first save publishes.

`release.of_task(session, task)` says whether the signed-in person sees the
task and may submit to it now: released, visible and open, and why not. It
reads `contest.yaml`, where the task's entry is its timeline, and the
extension of the person's row, their team's while they are in one and their
own otherwise, all as the platform, and a task with no publication, or one
the contest does not list, is not released. Nothing at the forge changes
when a task becomes released.

## Contestants

A person asks to join a contest with `contestants.register(session,
contest)`, with the code the contest asks for when it asks for one. A
request nobody has approved has nothing at the forge, so all of it is a row
in `contestants`. The contest has to be one they see: published, and
`everyone` or `signed-in`, or `hidden` for someone who has accepted an invite
to it. Then, stopping at the first refusal, each with a
code of its own: the registration window is open (`registration_closed`);
they hold no role at the contest, its tasks or its org (`is_staff`), read
under the org's lock on role changes, the one `roles.grant` takes, so a
grant and a registration never pass each other; they have no registration
there already (`already_registered`); the invite, the code and the email
address the contest asks for (`invite_required`, `wrong_invite_code`,
`domain_not_allowed`), where the invite is one they have accepted (below)
and a pattern must match the whole of one of the
addresses the forge has confirmed are theirs, whatever its case, within a
time limit, since the pattern is an organiser's; an address counts only as
far as the forge confirms it, so with Forgejo that needs
`REGISTER_EMAIL_CONFIRM` on wherever people sign themselves up; and a place is free (`contest_full`), counted under an
advisory lock on the contest, so the last place goes once. The row is written pending with what let it through;
with `approval: auto` it is approved in the same call. The rules themselves
are in `forge/domain/registration.py`.

An organiser managing the contest decides: `approve` a pending registration,
`reject` a pending one with a reason the person reads (`invalid_reason`
without one), `reopen` a rejected one, which leaves it pending again and is
refused like a new registration when the person holds a role there by now
(`is_staff`) or every place is taken (`contest_full`), `remove` an approved
one, and `extend` a pending or approved one, which moves that person's due
and close on the tasks it names, every task when it names none, while they
work alone (`invalid_extension` below nothing, past a year, naming a task
the contest does not list, letting them submit to a task whose reveal has
passed, or leaving a submission after the due or the close it was made
before). `teams.organise_extend` does the same for a team. A decision from any other status is `wrong_status`, naming the
status. `list` gives every registration of the contest to anyone observing
it, oldest first; `mine` gives a person their own.

A contestant's workspace is made a part at a time. With
`UNICON_PLACES_AHEAD` on, the default, approving someone starts making their
place to submit every task the contest has published, and a task's first
publication starts making it for everyone approved, each once the request
has committed and without the request waiting (`services/places.py`). The
work takes two turns in a process, apart from the four the first uploads
take, so a crowd approved at once never holds an upload back; the tasks it
makes places at are read once a minute per contest, and a contest that is
archived or over gets none. A place is made by the same steps as at a first
upload, holding nothing while the forge works, then the contestant's row is
held and read again and someone removed meanwhile has the access taken back.
A forge that fails stops the rest of that piece of work, in the log as
`places.ahead_stopped`. Whatever is not made ahead, by a restart, a forge
that failed or the setting off, is made at the contestant's first upload to
the task (below), which finds a place made ahead finished in one call. Off,
nobody who never submits costs the forge a repository. The workspace's id
comes from the contest and the person's user id (`workspaces.workspace_of`),
so the contestant's row holds no workspace. Removing a contestant takes
their access to whichever parts were made away and keeps what is in them.

What a signed-in person reads of a contest is `contest_home`: `contests`,
every contest they see with their own status; `home`, a contest's dates,
their registration, what the register form needs, the server's clock and
the tasks released to them in the contest's order, each with its label, its
place as a letter, its worth, and when it falls due and closes for their
row; and `task`, a visible task's statement, its submission caps and the
inputs a contestant gives, read from its plan with the task's form details,
the labels, options, bounds and sizes a submit panel shows, and nothing else
of what it holds. A visitor with no session reads
`landing`: the public contests, one with its released tasks, and a released
task's statement. Both lists of contests, a signed-in person's and a
visitor's, start from every published contest as the process read it at
most thirty seconds ago (`published.every_contest_kept`, kept in
`ctx.memo`, `forge/runtime/memo.py`), since reading it costs the forge two
calls an org and it is read as the platform, so one answer serves
everybody and each person's filtering comes after; people arriving while
it is read wait for that one read. A contest's settings saved through the
process drop its copy at once (`published.forget_contests`); another
process's copy lasts its thirty seconds. The operator's reconcile reads the
list live. Everything else is read live as the platform from the latest
publication of each task, with the contest's visibility and the release
rules applied first, and a contest or task the reader may not see is no
such contest or task, the same answer as one that is not there.

## Invites

`invites` lets an organiser ask one person, by username or by email address,
to take a contestant's place in a contest or an organiser's role at an org,
a contest or a task. An invite is a row in `invites`, since it may name an
address with no account behind it. `create` needs what granting it would:
the manager role at the scope, admin to invite an admin, and a contestant's
place only at a contest; a username is looked up at once, someone who holds
the role or a registration already, or could not take it (a role in a
contest they are registered for, a place in one where they hold a role), is
refused (`invalid_invite`), the same pending invite twice is
`already_invited`, and an org makes at most 1000 invites a day
(`invite_limit`). An address is one plain address and nothing else, so one
invite mails one person. Making and changing invites take the org's lock
on role changes, the one `roles.grant` takes. `at` lists a scope's invites
to its observers, newest first, at most 500; `send_again` mails a pending
one again, lapsed or not, with a new token, so the earlier link stops
working, and its whole lifetime again from then, at most once every ten
minutes (`invite_limit`) and only where there is a mail server
(`invalid_invite`); and `withdraw` takes back a pending one, or an accepted
contestant's place until its person registers.

The mail goes out once the request that made the invite has committed, and
the request answers without waiting (`ctx.in_background`), so a mail server
that is slow or down never fails the click. The invite is the record, and
its `mail_status` says what became of the mail: `waiting`, `sent`,
`failed`, whatever stopped it, a username whose account has no confirmed
address included, or `off` on a deployment without a mail server. A mail a
restart cut short stays `waiting`, and an organiser sends it again. At most
four mails are written and sent at a time in a process, and none holds a
database connection while it asks the forge or the mail server. The mail
carries a link to `/invites#<token>` on the app; the token is kept only as
its SHA-256, and sits after the `#` so no server's log ever holds it.

The person acts on their own invites only. `mine` lists their pending ones
that have not lapsed, once every pending invite to an address the forge has
confirmed is theirs has been made theirs; an address counts only as far as
the forge confirms it, so with Forgejo that needs `REGISTER_EMAIL_CONFIRM`
on wherever people sign themselves up. `by_token` opens the invite a link
carries when it is theirs and is `not_found` for anyone else, or
`forge_unavailable` when the forge could not say whose address it is; `accept`
grants what the invite carries and `decline` grants nothing, both closing
it, and a lapsed one does neither (`invite_expired`). An organiser role is
granted by the rules a grant keeps, and never lowers a role the person
holds; a contestant's place is the eligibility an invite-only contest asks
for, and shows a hidden contest to them until they register, when their
registration decides. Either is taken only while whoever sent the invite
may still grant it. Deleting an account withdraws the pending invites to
it. The rules are in `forge/domain/invites.py`.

## Announcements and clarifications

Both are threads at the forge, through the port's `threads` area, and
nothing about either is kept in the database: the thread is the record.

An **announcement** is an organiser's message to a contest or one of its
tasks, a thread labelled `announcement` on the contest's or the task's own
repository. A manager there posts, edits and closes one, each as
themselves with their own credential, so the forge's record says who wrote
it (`announcements.post`, `edit`, `close`); an observer reads every one,
closed ones included (`manage`). There is no delete: a message people have
read is closed and stays readable. A signed-in person reads the open
announcements of a contest they see and of each task released to them
(`contest`), and of one released task (`task`), read live as the platform,
since a contestant reaches no repository at the forge; a task not released
to them contributes nothing.

A **clarification** is a contestant's question, a thread labelled
`clarification` on the desk of their own workspace, which is what keeps it
private to them and the organisers who reach the desk through the
contest's roles. An approved contestant asks as themselves, and their first
question makes the desk, as the platform (`clarifications.ask`), with them
a reader on it, so they cannot change an organiser's reply or a label; the
port puts the label on as the platform when Forgejo drops a reader's. A
question may name a task released to them, kept as a line in its body that
is checked against the question's contest whenever it is read, since the
asker can edit their own question at the forge. They read their own
questions with every message (`mine`), and comment again on one
(`follow_up`), after which, on an answered question, the platform takes the
mark off and opens it, so the follow-up lands back with the organisers in
the same thread. A manager at the contest
replies, which leaves it open (`reply`); marks it answered, which labels it
`answered` and closes it, with or without a reply (`mark`); and takes that
back (`unmark`). Marking or unmarking twice changes nothing, so a retry is
always safe and a reply is never posted twice. The inbox is one search at
the forge for the org's open clarifications, as the organiser, for anyone
holding a role anywhere in the org, each shown only where they observe the
contest (`inbox`); an answered question is closed precisely because the
forge's search cannot ask for a label's absence. An issue labelled
`clarification` anywhere but a desk is passed over. `of_contest` is every
question of one contest, answered ones included. An answer made public is
an ordinary announcement on the question's task, or its contest, posted as
the organiser with a line the platform adds pointing at the question
(`answer_publicly`); readers learn that it answers a question and an
organiser which, and the question stays private. Text a person writes
never carries such a line: `announcements.checked` takes any out, again
until none is left. A title
is at most 200 characters and a text at most 20,000, and an empty or
longer one is `InvalidMessage`, naming which.

## Live updates

A page that shows something that can change while it is open hears of the
change and asks for the thing again; nothing that changed travels on the
stream. A nudge (`domain/live.py`) is a kind, `grading`, `announcement` or
`clarification`, and an id, with who may hear it: the one person it
concerns, the organisers who observe a scope or a broader one, and the
approved contestants of a contest. `resync` tells a page that nudges may
have been missed.

A unit of work nudges with `ctx.nudge(...)`, and the setup publishes every
nudge the unit of work left in one `pg_notify` on its own transaction, just
before the commit, so Postgres delivers them with it and never for a unit
of work that rolls back. Every write of a grading's status or progress
nudges its contestant and its task's organisers (`gradings.changed`, from
`new_row`, the start, `finish`, the envelope and the progress callback),
which is what moves a submissions list while its owner watches. Its scope
is the task, and a role at the contest or the org covers the task, so
everyone who can read a grading in a contest's feed hears it change; the
feed's page asks for the feed again on every grading nudge, since a nudge
says only that some grading moved, and a new attempt is a new id. A thread's
changes come from the forge: the host answers the forge's push as soon as
`events.check` passes, then hands the body to `events.publish`, which reads
it through the port and nudges a clarification's asker and its contest's
organisers, an announcement of a contest its contestants and organisers,
and one of a task its organisers and, only once the task is released, its
contestants. An event about anything else, about an issue that does not
carry the label of the thread its repository holds, about a pull request,
or naming a place in another org than the one whose secret signed it,
nudges nobody.

Each process keeps one broker (`runtime/broker.py`), with one connection
outside the pool that listens on `unicon_live`, opened when the first
stream subscribes and closed when the last leaves, so whichever process
published a nudge, every process's streams hear it. `live.stream(session)`
is what the host serves as one Server-Sent Events connection per open
tab: it checks the session, reads its audience (the person, every role they
hold and the contests where they are an approved contestant), yields `None`
once it has subscribed, so the host can wait for that and have any refusal
raised before it answers, and then each nudge that audience hears, or
`None` every fifteen seconds for a keepalive. It holds no database
connection while it waits, checks the session again every minute without
counting that as the person being there, so an open tab never keeps an idle
session alive, and ends once the session has; it reads the audience again
every five minutes, keeping the roles it had when the forge does not
answer. A stream that falls 256 nudges behind is emptied and told to
resync, and so is every stream when the listening connection drops and is
opened again a second later, and a stream that subscribed while the
connection was still being opened. A session holds at most eight streams
in a process; one more ends the oldest.

## Teams

`teams` lets contestants of a contest whose `contest.yaml` turns `teams` on
enter together and count as one contestant. Everyone in a team registers
and is approved first, so the contest's rules hold for every person. An
approved contestant `create`s a team, leading it, or `request`s to join one,
which accepts the leader's invitation when there is one; the leader
`invite`s an approved contestant, `approve`s a request and `remove`s a
member, or turns a request down; a member `leave`s, and the lead passes to
the member who joined earliest. Organisers of the contest make
(`organise_create`), delete (`organise_delete`, refused as
`team_has_submissions` for a team that has submitted), move people between
(`organise_move`), take people out of (`organise_remove`) and change the
leader of (`organise_lead`) teams, and an observer lists them all (`every`).
`mine` gives a person their team and the teams they asked or are asked
into, and `listed` the teams to choose from. Refusals: `teams_off`,
`invalid_team_name`, `team_name_taken` (names are unique in a contest,
ignoring case), `team_full` (with `limit`, the contest's `max_size`),
`in_team`, `submitted_alone` (someone who submitted on their own joins no
team, since those results are theirs), `not_approved` and `forbidden`. A
contestant's changes stop at the contest's end; organisers can still mend
teams, and a member can leave and organisers remove after teams are turned
off. A team left with nobody that never submitted is deleted, its members'
rows kept `left`.

Once in a team, the team is the person's contestant: their workspace is the
team's (`TeamOwner`, named `team.<id>`), so its gradings, the task's limits,
which are counted per workspace, and its questions are the team's, and
every member sees them. Who reaches the team's workspace changes in the
request that changes the membership, at the forge first: a joiner is given
access to every part already made (`share_workspace`, which also takes off
anyone else), and someone who leaves, is removed, moved or taken out of the
contest loses it; a call that fails fails the request, and asking again
finishes it. A part made later is made with the members then, and checked
against them afterwards (`teams.settle`), since it is made holding no lock;
a team's repository also sheds any former member whenever it is opened. A
submit holds the person's registration so no change of team passes it, and
is asked again (`team_changed`) when their team changed under it. In a
contest with teams, places are made ahead for teams, not people. Live
updates reach every member of a team. Locks: contestant rows first, then
team rows by id.

## Uploads

A person's files go from the browser into the forge's own large-file store
through the upload door, and never through the platform.
`uploads.slot(session, task, input=, filename=, size=, sha256=,
content_type=)` needs the person to be able to submit to the task now, the
same checks a submit starts with, the input to be one of the task's file or
folder inputs, the file's path to be one the input takes (one plain name for
a file input, a path of plain names for a folder input, `<group>/<test>` with
or without an ending for a per-test input), the digest to be a SHA-256 in
lowercase hex, and the size to be within the input's `max_size`, and never
above the platform's ceiling (`too_large`, naming the limit and the input
whose it is); a save that sets a larger limit is a draft, with the problem
at that limit's path. A person holds at most 200 uploads for a task that no submit has used,
declaring at most twice the task's submission limit in bytes together, the
one asked for included (`upload_limit`, with `limit` and `bytes`). The count
is taken under an advisory lock on the person and the task, held until the
unit of work ends, so two slots asked at once cannot both pass.

Before any of that, it makes the person's place to submit the task, if no
slot of theirs for it has been kept before, since an object belongs to a
place and there has to be one to put it in. It asks with no connection held
(below); then it asks the forge whether the place already holds that
object, which answers a `Slot` that is `ready` with nothing to send; and
otherwise records an `uploads` row and answers the address to send the file
to.

`uploads.door(session, upload, length=)` is what the proxy asks before it
reads a byte of the body: it answers where the bytes go and the person's own
credential to present there, for that person's own waiting upload of exactly
that length, and `Forbidden` in the same words for everything else, since the
browser learns where its upload stands by asking for the upload.
`uploads.complete(session, task, upload)` asks the forge whether the place
holds the object: `verified` when it does, `upload_not_ready` when it does
not. There is no rejected upload, because the forge keeps nothing that is not
what its address named. Asked again it answers the same. An upload is its
owner's alone, for one task: anyone else asking for it is told there is no
such upload. When the forge fails, what it said goes to the log and the
caller is told in fixed words that the file store did not answer; a
completion it failed on leaves the upload `waiting`, to be completed again.

`uploads.task_file_slot(organiser, task, path=, size=, sha256=)` is the same
for a file an organiser puts into a task, which the next save writes the
pointer for (`files.write_upload`).

An upload no submit used loses its row once its two days are over, the
next time its owner asks for a slot: before the count is taken, `slot`
removes the person's own lapsed, unused uploads, so they stop counting
against what the person may hold. Their bytes are the forge's to collect
once no commit names them. The row of an upload a submit used stays
`consumed` as the record of what was submitted, and its bytes stay, since
the submission's commit points at them.

## Submissions

`submissions.submit(session, task, inputs, idempotency_key=)` runs in this
order and stops at the first refusal, before anything is written, each with a
code of its own: the task is open by the server's clock plus the
contestant's extension (`task_closed` with its `reason`, or `archived`); they
are approved (`not_approved`); they have submissions left (`submission_limit`),
counted from the submissions at the forge but for the ones staff
cancelled; the task's rate holds
(`rate_limited`, with `retry_at`), counted from the grading rows within its
window; every upload named is theirs for this task (`upload_not_yours`), a
checked file no submission used (`upload_not_ready`), and each and all of
them within the sizes allowed (`too_large`); and what is given fits the
task's contestant inputs (`invalid_inputs`, each problem at its input). The
forge is asked once more that the place still holds each upload's object,
so a commit never points at bytes that are not there (`upload_not_ready`).
A file upload made the contestant's place to submit the task already; a
first submission of nothing but typed values makes it, as the platform,
once they are found approved and before the submit's hold, which is the one
thing a later refusal leaves at the forge. The place is made first, holding nothing, since it takes the
forge seconds; then their `contestants` row is held and read again, and
someone removed meanwhile has the access just given taken away again and is
`not_approved`. A removal that comes after takes away a place already
there, so either way the access is gone once both are done. Then the files go in as one commit as the
contestant, `files/<input id>/<path>` beside `submission.json`, named
`submission/<n>` as the platform; one `queued` grading row is inserted,
against the task's current publication, attempt 1,
with the SHA-256 of its callback token, `base64url(HMAC-SHA256(k,
"callback:" || grading id))` with `k` derived from
`UNICON_TOKEN_ENCRYPTION_KEY` by HKDF, so the token itself is never stored,
and its run is started once the submit commits; and the uploads are marked
consumed, their objects removed once it commits. The bytes of an upload
whose object is gone, or grew, are refused like ones that changed (`upload_not_ready`), and no more of an
object is read than the size the upload was checked at. A forge that fails
the commit, or a store that fails the read, is told in fixed words: the
forge or the store did not answer (`forge_unavailable`), refused the
platform's own registration (`forge_misconfigured`), or the forge refused
the submission (`rejected`), with what it said in the log.

Submits of one workspace to one task happen one after another, under an
advisory lock held until the unit of work ends. The same idempotency key
sent again answers with the submission it made and creates nothing: its rows
are found by the key, unique for a workspace and task, and when the
forge's writes landed but the rows did not, the submission is found at the
forge by the key its note carries and only its rows are inserted. A
submission named at the forge whose rows never landed is one the contestant
saw fail, and submitting again with the same key finishes it.

`mine` lists the signed-in person's own submissions of a task, newest first,
`one` gives one by its number, each with how many started days after their
row's due it was and the latest attempt of its grading, as the task's test
groups show it (`domain/showing.py`): a group shown `always` with its
outcome and its tests, one shown as a `verdict` with its outcome and its
tests at the task's reveal, one shown `after_close` with its name and when
it is shown; the outcome over the groups shown; the values reported once,
and each value folded over the tests shown (`folded`); and what stopped the
run. A run that a sealed step stopped, as the result's `stopped_by` names
it, is held whole until the reveal: no stop, every group hidden as if it
ran, its points all pending and no folds, so nothing tells it from a run
that has not stopped. A grading is shown
with the publication it ran under: that publication's sealed facts always,
and the latest publication's `test_groups` when the two plans list the
same tests, its own otherwise, so its rows are folded with the tests they
ran on; a group with no rows did not run on it (`ran` false) and adds
nothing to the outcome. A past publication's `task.yaml` and plan are read
once per process. A run in `system_error` is told to its contestant as
still running, with nothing of it shown, and one staff then cancelled as
`cancelled` with the sentence they gave (`Result.reason`); while a fallback
is in force for either (below), the submission is told by its last good
result instead, the attempt the boards count. `files` gives
the inputs one was made with, as its `submission.json` names them, and
`download` a door to one of those files, which the proxy streams from the
forge. A run's log
names every test, the hidden ones too, so it is the organisers'
(`gradings.run_log`). Anyone else's submission is no such submission.

## Grading

A grading is one row of `gradings` per submission and attempt, and
nothing about one is ever edited into another: a retry and a rejudge make
new attempts, each a new row with a new id and so new secrets, and the old
rows stay as they were. A grading has one run, which proves itself with
two secrets derived from `UNICON_TOKEN_ENCRYPTION_KEY` and the grading's id:
the envelope key its envelope's URL carries and the callback token the
envelope hands it, whose SHA-256 the row keeps. Its status runs `queued`
(its run is not started yet), `dispatched` (the CI holds the run, waiting
for a machine or checking out), `running` (the harness fetched its
envelope), and ends `done`, `cancelled` or `system_error`, a failure of the
platform's, never a grade. A grading past what its state may take reads
as `system_error`, with the reason as its error, wherever it is read
(`overdue` in `domain/grading.py`, `gradings.status_of`), and is refused its
envelope and reports; its row keeps its status, and an organiser retries
it: `queued` five minutes after it was made (its start was lost),
`dispatched` two hours after its run was started (no machine took it, or it
never reached the harness), and `running` past its deadline.

A `dispatched` grading can also have a run the CI lost: Woodpecker 3.18.1
drops a run from its queue for good when the machine it handed the run to
does not renew its claim within a minute, after a network drop or a machine
that died during the checkout, and the pipeline goes on saying `pending`
(woodpecker-ci/woodpecker#7063). Once a grading has been `dispatched` two
minutes, reading it asks the CI where its run is (`grading.run_state`,
`gradings.lost`): queued for a machine, taken by one, finished, or lost,
told by the queue the run is in, not by its status, which is `pending`
either way. A lost one reads as `system_error` with `LOST` as its reason;
one still queued is left alone however long it waits, since at a
contest's start a run waits minutes behind others. The CI is asked about a
run at most once every fifteen seconds by a process, through the memo,
and the Woodpecker adapter reads the queue at most once every ten
seconds for all runs, so contestants' pages polling every few seconds
cost it little. Nothing is written when a grading is read; the reader's
connection is let go of before the CI is asked, and a CI that does not
answer loses nothing.

**Starting a run.** Every new grading row hands `gradings.start` to
`ctx.after_commit`, so its run is started right after the unit of work that
made it commits, on a unit of work of its own: the CI asks the platform
about the grading while the start is under way, and must find the row
committed. Up to eight of one request's starts run at once. `start` passes over a
grading that is not `queued`, reads what the start needs and commits that
much, so the call to the CI holds no connection and no lock, and as the
org's own account (`org_accounts.identity`)
starts a run on `main`, the one thing the CI starts a run on, with the
variables `UNICON_GRADING_ID`, `UNICON_ENVELOPE_URL`,
`UNICON_PUBLICATION_COMMIT`, `UNICON_SUBMISSION_REPO`,
`UNICON_SUBMISSION_COMMIT` and `UNICON_COMPUTE`, which is `pool:platform`, so
only a machine the platform controls takes it. Then it takes the row under
`FOR UPDATE`: the run's id and when it was started go on it, and it is
`dispatched`, or, when the grading moved on meanwhile, the run is cancelled. A start that fails ends
the grading in `system_error`, with a reason in `error` in the platform's
words and what the CI said in the log: the org's account is not ready
(`ACCOUNT_NOT_READY`), the publication it grades against is gone
(`PUBLICATION_GONE`), the CI did not answer (`NO_ANSWER`), refused the org's
account (`REFUSED`), answered without a run (`NO_RUN`), which is how
Woodpecker answers a start, with an empty 204, when the extension refused
it, or does not take the task for grading (`NOT_ACTIVATED`). Whoever reads
it sees that at once and tries again, a contestant by submitting, an
organiser with `retry`.

**The configuration extension.** `runs.config(request)` takes the CI's
request as it arrived, an `InboundRequest` of method, target, headers and
body, and hands it to `grading.answer` with the service's `lookup`. What a
run runs is decided by the platform, never by the request or the
repository, and that takes two checks kept apart: `lookup` answers only for
a grading of the task the request names that is `queued`, and the
CI's adapter refuses a run started with any variable but those it starts
that run with, since anyone who may start a manual pipeline on the task's
repository may pass variables of their own, a harness image among them. The
Woodpecker adapter checks its RFC 9421 signature, over the request target
and the body's `Content-Digest`, made within five minutes, against the CI's
ed25519 key, read from `GET /api/signature/public-key` with the CI
administrator's token, kept, and read again once when a request does not
verify; a signature that says it expires is refused after that. The key is
asked for at most once a minute, whether the last read worked or not. The request names the task's repository and the
run's variables; the grading must be the one `UNICON_GRADING_ID` names, of
that task, and `queued`, since the CI asks while the start is under way,
and every other variable must be the one the platform starts that
grading's run with. The answer, `{"configs": [{"name": "grading", "data":
...}]}`, is the same every time for the same run: `labels` from
`UNICON_COMPUTE`; under `clone:` two full steps, `task` and `submission`,
each running `UNICON_CLONE_IMAGE` with `remote`, `sha`, `ref` (the
`published/<n>` or `submission/<n>` tag) and `path` (`/woodpecker/task`,
`/woodpecker/submission`) set, `lfs` on for both, the machine's
store of large files for the task's org as `unicon-lfs-<org>:/lfs-cache`, one
per org, so no org's task is served a large file another org's task brought
to the machine by naming its object id, and no `environment`, since a clone
step with one is lent no credential; and one step, `grade`, running the
harness image the plan names in the publication, with the socket
filter's socket mounted read-only as `unicon-filter:/run/unicon:ro`, which
the harness connects to and cannot replace, `DOCKER_HOST` naming it, and no
credential. Anything else is `CiRequestRefused`, never an
empty answer, with the reason in the log. The action writes nothing, so it
never waits on the start holding the row.

**The envelope.** `runs.envelope(grading, key)` is the runner's
`envelope.schema.json` version 5, served once: only with the envelope key of
the grading's run (`NotFound` otherwise), and only while the grading
is `dispatched` (`GradingClosed` otherwise). That fetch is the run
beginning: the grading is `running` and its deadline is written, the wall
clock and a minute for reporting from now, so a run that waited for a
machine loses none of its time. Any later fetch is `GradingClosed`, since
the envelope's URL is one of the run's variables, which anyone who can read
the task's runs at the CI sees, and the envelope hands out the callback
token. The harness fetches it once and does not try again: one whose fetch
lost its answer ends its run without a report, and the grading reads as
`system_error` once its deadline passes, for an organiser to retry. The
envelope carries
the grading and attempt, the submission as the forge names it, the
two checkouts, the callback URL and token, a URL the harness writes its log with
into `unicon-results` at `logs/<grading id>/<attempt>.log`, signed for
`UNICON_MACHINE_URL` until the deadline, the deadline,
`limits.wall_seconds`: the plan's step time limits summed with fifteen
seconds for each container and a minute for the run, which a save refuses
above 25 minutes, and `secrets`, the value of every secret the plan names,
empty while no org holds one. The times agree with the CI: a run is given the 30 minutes of
Woodpecker's pipeline timeout (`WOODPECKER_DEFAULT_PIPELINE_TIMEOUT`), four of
them for the checkouts, 25 for the harness and one for reporting.

**Reports.** `runs.callback(grading, authorization, body)` takes one report
under `Authorization: Bearer <token>`, the token's SHA-256 compared with the
row's in constant time, so another grading's token is refused like a wrong
one (`InvalidToken`): the token is what says which grading a report is
for. The envelope's key and a report's
token are checked on the row as read and again once it is locked, so a
caller that proves nothing holds the row up for no one. A report comes only from a `running` grading before
its deadline (`GradingClosed`), and a body that is no report is
`InvalidCallback`. `started` confirms the run began, `progress` is kept on
the row as `{"step", "done", "total"}`, and `finished` carries the result: one
that matches the runner's `result.schema.json` version 5 is kept on the
row with its log key, its numbers exactly as written, and the grading is
`done`, or `system_error` with the result's `error` when the run stopped on
one. Any other result, one of more than 1 MiB as JSON included, leaves the
grading in `system_error` with the reason in `error`, and is taken, since
sending it again would not mend it. A kept result sent again after its
answer was lost is answered the same. The
action answers the grading's status after the report.

**Reconcile.** A submission is named at the forge before its rows are
inserted, and a database restored from a backup lacks the gradings of every
submission made since, so `unicon-forge reconcile`, which the operator runs
once after a restore, reads them at the forge. Over every contest of every
org the platform made, for every published task and every person with a row
in `contestants` for the contest, whatever became of their registration, it
lists that person's submissions of the task and inserts, for any with no
grading, one queued grading against the current
publication, carrying the idempotency key its tag's note carries. Their runs
start once it commits. Before a task's submissions it activates the task at
the CI as the org's account, signing it in again once if the CI refuses it,
since the CI's database restored from a dump taken at another moment, or
alone, may not know a task made since; a task the CI knows is unchanged,
and one it cannot activate is logged as `reconcile.activation_failed` and
its gradings are still inserted. It logs what it did as `reconcile.done`,
with how many tasks it activated that the CI did not know.

**The organiser's controls.** Each takes the `Organiser` from
`access.organiser` and needs manager at the grading's task; a grading whose
task they do not observe is no such grading. `gradings.cancel(grading,
reason)` is staff ending a submission in `system_error` when a regrade
would only repeat the fault: it cancels a latest attempt that reads as
`system_error`, stored or because it is overdue or lost while its row still
waits, with `reason`, a sentence its contestant reads, trimmed, from 1 to
500 characters (`invalid_reason` otherwise). The row keeps `error`, the
line for staff, the overdue or lost reason written there for one that only
read so, and the sentence in `cancel_reason`; a run of it still at the CI is
cancelled once the cancel has committed. Anything else is `WrongStatus`
with the status it reads as, and an earlier attempt of a submission
attempted again is `Conflict`, since the latest is the one to cancel. The
cancel is final: a submission whose latest attempt staff cancelled is
served to its contestant as `cancelled` with the sentence, does not count
against the task's `submissions.max`, and is graded again by nothing: a
`retry` of it is `WrongStatus` saying staff cancelled it, and a rejudge and
a save's regrade leave it as it is. Staff who want it graded after all tell
the contestant, who submits again. Migration 0014 gives each submission
cancelled before cancels carried a sentence a stock one, "The organisers
cancelled this grading.", so it reads and counts the same.

**Falling back.** A submission whose latest attempt is a `system_error`, or
staff cancelled, counts as still grading, or void once cancelled, while the
contest's `on_system_error` is `grading`, the default. A **fallback** has it
count as its last good result instead, the latest earlier attempt that
finished with a result, while there is one: on the boards, to its
contestant, and under `submissions.max`, a cancel included. It is in force
by the contest's `on_system_error: last_result`, or by staff on the one
attempt: `gradings.fall_back(grading)` sets `falls_back` on a latest
attempt that reads as `system_error` or staff cancelled (`WrongStatus`
otherwise, `Conflict` for an earlier attempt or a submission with no
earlier result), and `gradings.clear_fallback(grading)` takes it back, so
the contest's word holds; both answer the grading as it then stands and
need what a cancel needs. Migration 0016 adds the column, false on every
grading so far. Every `GradingRecord` of a broken latest attempt carries
`last_good`, the attempt a fallback counts, and `fallback`, `staff` or
`contest` while one is in force and none otherwise; a contest whose
settings do not read counts as `grading`.
`gradings.retry` makes a new attempt of a submission's latest attempt once
it is finished, against the publication it graded against, while no other
attempt of it is being graded (`Conflict`). An earlier attempt is
`Conflict` too, since retrying it would grade the submission again against
the publication a later attempt replaced. One that reads as finished only
because it is overdue or lost is ended first with that reason written on
its row, and its old run is cancelled at the CI once the retry has
committed, so it does not keep a machine's containers going.
`gradings.rejudge(task)` makes a new attempt of every submission's latest
attempt, against the current publication, cancelling first one still being
graded against an older publication and leaving one being graded against
the current one, and answers a `Rejudged` with its counts.
`gradings.list(task)` gives the task's gradings, newest first, at most 500,
to anyone observing the task, each a `FeedEntry` as the feed gives it
(below), so it says who submitted each. Every `GradingRecord` an organiser
reads, from `list`, the feed, a cancel or a retry, has the result whole and
`latest`, whether it is its submission's latest attempt, worked out over
every attempt of the submission whatever the page holds: the latest is the
one to cancel or retry. `gradings.run_log(grading)` gives
the grading's run log to anyone observing its task, read from the store up
to `RUN_LOG_MAX`, 9 MiB, and refused above as `LogTooLarge`; a grading with
no log is `NotFound`, and a store that fails is `Unavailable` in fixed
words, with what it said in the log. A route that names only the grading
checks the organiser at the task `gradings.task_of(grading)` gives.

**A contest's gradings.** `gradings.feed(contest, task=, user=, team=,
status=, limit=)` lists the gradings of the contest's tasks as one feed,
newest first, at most `limit` (100 unless given) and never more than 500,
each a `FeedEntry`: the `GradingRecord`, with the overdue and lost reading
and its reason and `latest`; `by`, a `Submitter`, the contestant by user id
and username or the team by id and name, the name none once the account or
the team is gone or the forge does not say; and `task_name` and `label`,
the task's name and the letter of its place in the contest's `tasks`, the
label none once the contest no longer lists the task or its settings do not
read, so a page needs no second read for them. Every attempt is a row of
its own, so the attempts of one submission group by its workspace and
number. `task` narrows to one task; `user` to what a contestant submitted,
by username: on their own, and in each team of the contest while they were
in it, from when they joined until they left, by when the submission was
taken; `team` to a team's; and `status` to the gradings that read as it. A
`user` that breaks the forge's username rule (`names.is_username`:
letters, digits, `-`, `_` and `.`, beginning and ending with a letter or a
digit, none of the three twice in a row, at most 40 characters) matches
nothing and is never sent to the forge. A filter by status is told in the
query by the deadlines `overdue` keeps: `system_error` reads the rows
stored so, the ones past their deadline and the `dispatched` ones the CI
is to be asked about, and an unfinished status the rows stored so and not
overdue. Only whether the CI lost a run is asked once a page is read, so a
filter reads another page only when a lost-run check left one short. An
observer of the contest, or of its
org, sees every task's gradings; someone holding a role at some of its
tasks alone sees theirs, a task they do not observe left out rather than
refused, and someone holding none of either is `Forbidden`. The usernames
cost one read of the forge per contestant on the page, kept a minute by
`CachedForge`, and a team's name one query. `gradings.queue_depth(contest)`
counts, in one read over the same gradings, the ones waiting for a machine,
as a `QueueDepth` of `queued` and `dispatched`, by the status they read as:
one overdue or lost reads as `system_error` and is not counted. Nothing
stores the count.

## Scores and boards

Nothing scored is stored. A result keeps the run's raw facts, and every read
scores it again (`services/scores.py`, `domain/scoring.py`, TASK-FORMAT.md
sections 3.1 to 3.5): each test's credit, each group's points
`worth * late * E_g / W` and most `worth * R_g / W`, and each reported value
folded over the tests, all as exact rationals, a value written `0.1` read
as one tenth. A grading is scored with the latest publication's groups,
credit and value meanings while no grading change came between them, and
with its own otherwise. Relative credit's best is taken over the
candidates the boards count, each row's of the task graded under the same
plan. A submission is read with its points shown and those still decided
at the reveal.

`boards.seen(session, contest)` gives every board the reader's audience
sees, a visitor reading with no session, and an archived contest's to its
approved contestants alone; `boards.organised(organiser, contest, row=)`
gives every board as `now` and `final`, every row, or, for a row of the
contest, `now` of each board shown to contestants as that row sees it,
beside what the boards ask of their tasks that does not hold. A board is
ranked on read (`domain/boards.py`) over the contest's rows, each approved
contestant in no team and each team with an approved member, read once a
request: which submission counts per row and task (`best`,
`best_per_group`, `marked`), its keys in turn, ties sharing a rank. A
submission still grading, stopped by a step that is not sealed, or
cancelled is no attempt; a regrade in progress leaves the earlier attempt
counting. `marks.mark`, `unmark` and `held` keep a row's marks for the
`marked` boards in `marks`, at most the task's `marks`, frozen at the row's
close; the task page says how many (`TaskPage.marks`) to an approved
contestant alone, and to anyone else an open task reads closed as
`not_approved`. A task's save is refused where a board covering it asks what it does
not give (T8) and reports the boards it moves (T9); a contest's save is
refused where a board asks what its tasks do not give (C4), or lowers a
task's `marks` below what a row holds (C1).

## Errors

Every error has a stable `code` and a `detail` for a person, and some carry
structured members in `extra`:

| Error | `code` | `extra` |
|---|---|---|
| `NotFound`, `Forbidden`, `Conflict`, `Rejected`, `Unavailable` | `not_found`, `forbidden`, `conflict`, `rejected`, `forge_unavailable` | none |
| `Misconfigured` | `forge_misconfigured` | none |
| `InvalidDefinition` | `invalid_definition` | `errors`, each `{"path", "message"}` |
| `InvalidPath` | `invalid_path` | `path`, the path refused |
| `AdminOnly` | `admin_only` | `keys`, each admin-only key or file |
| `ReservedPath` | `reserved_path` | `paths` inside `plans/` |
| `ConfirmationRequired` | `confirmation_required` | `changes`, what would change |
| `SoleAdmin` | `sole_admin` | `scopes`, each `{"kind", "name"}` |
| `ContestantConflict` | `contestant_conflict` | `contests` |
| `SharedWorkflowOwner` | `shared_workflow_owner` | `workflows` |
| `WrongStatus` | `wrong_status` | `current`, the registration's or the grading's status |
| `InvalidMessage` | `invalid_message` | `field`, `title` or `body` |
| `TaskClosed` | `task_closed` | `reason`, `closed` |
| `SubmissionLimit` | `submission_limit` | `limit`, the submissions allowed |
| `RateLimited` | `rate_limited` | `rate`, such as `1 per 30s`, and `retry_at` |
| `TooLarge` | `too_large` | `limit` in bytes, and `input`, or none for the task's |
| `UploadNotYours` | `upload_not_yours` | `uploads`, each id refused |
| `UploadNotReady` | `upload_not_ready` | `uploads`, each id refused |
| `UploadLimit` | `upload_limit` | `limit`, the open uploads one person may hold for a task, and `bytes`, what they may declare together |
| `InvalidInputs` | `invalid_inputs` | `errors`, each `{"input", "message"}` |
| `LogTooLarge` | `log_too_large` | `limit`, the most of a run log read, in bytes |
| `MarkLimit` | `mark_limit` | `limit`, the marks a row may hold on the task |

The rest, `invalid_name`, `unauthenticated`, `session_expired`,
`fresh_sign_in_required`, `sign_in_invalid`, `sign_in_denied`,
`not_ready`, the registration refusals `registration_closed`, `is_staff`,
`already_registered`, `invite_required`, `wrong_invite_code`,
`domain_not_allowed` and `contest_full`, which share the base class
`RegistrationRefused`, `invalid_reason` and `invalid_extension`, the
invites' `invalid_invite`, `already_invited` (with `invite`, the one held
already), `invite_limit` (with `retry_at`) and `invite_expired`, the
teams' refusals (above) and `team_changed`, and
`archived` (the contest is archived), `not_approved` and
`invalid_idempotency_key`, and grading's
`ci_request_refused` (the CI's request does not verify or names no grading
being started), `invalid_token` (a report without its grading's token),
`grading_closed` (the grading takes no envelope or report now) and
`invalid_callback` (a report that is not one), and the marks' `marks_off`
(no marked board covers the task) and `marks_frozen` (the row's close has
passed), carry nothing beyond the detail. The refusals of an upload or a submit share the base class
`SubmitRefused`.

## The tables

Ten tables, keyed by UUID v7 but for `names`, keyed by the id it names,
with every enumeration as `text` under a `CHECK`: `sessions`,
`contestants`, `gradings`, `uploads`, `org_accounts`, `names`,
`invites`, `teams`, `team_members` and `marks`. They
hold what a forge cannot: of users, orgs, contests and tasks only the names
people gave the last three (above), the rest read live. `org_accounts` is
one row per org, by the org's id, its service account's
forge credential, its `ci_state` and its event secret each as AES-256-GCM
ciphertext under `UNICON_TOKEN_ENCRYPTION_KEY`, the way a session's
credential is, so a copy of the table hands out no access;
`services/credentials.py` is the one place either is sealed or opened,
but for revision `0017`, which keeps a frozen copy of the sealing so it
reads the same whatever the package does later. Revision `0017` moved the three columns Woodpecker's sign-in filled into
`ci_state`, which reads `UNICON_TOKEN_ENCRYPTION_KEY` whenever there is a
token to move and stops before changing anything without it.
A `contestants` row is one person's registration for one contest, and
names no workspace, since a workspace's id comes from the contest and the
person. A `gradings` row names the task, the workspace, the submission with
its number and the exact version its files went in with, when it was
submitted, the publication, the attempt, and the idempotency key
of the submit that made it; its status is one of `queued`, `dispatched`,
`running`, `done`, `cancelled` and `system_error`. `queued_at` is when it
was queued, `run_id`, `dispatched_at`, `started_at` and `deadline_at` are
its run, when it was started, when its harness fetched the envelope and its
deadline, `progress` the last progress reported, `result` and `log_key`
what came back, `error` a line for staff, and `cancel_reason` the sentence
staff cancelled it with, which its contestant reads, on every submission
staff cancelled (a stock one on those cancelled before 0013) and on no
attempt a rejudge replaced. An `uploads` row is one
browser upload: its owner, task and input, name, declared and measured
size, digest, status, the id of its parts while they arrive, the submission
that consumed it, and when its lifetime ends. An `invites` row is one
invite: the scope's keys, what it grants, the username or the address it
names, who it is for once known, who sent it, its token's SHA-256, its
status, its expiry, when it was decided, and what became of its mail. A
`marks` row is one submission a row marked: the workspace, the task, the
submission's number, and who marked it.
`unicon-forge migrate` reads `UNICON_DATABASE_URL`, applies the migrations
under `forge/db/alembic/` and exits. A deployment runs it before the host
starts, from the host's image, which has the package and its command
installed; the host has no migrate command of its own. `unicon-forge
reconcile` activates every published task at the CI and gives every
submission at the forge without gradings its gradings (above), and the
operator runs it once after a restore.

There is no jobs table, and nothing in the package runs on a timer. Each
piece of upkeep is done by a request that already touches what it keeps:
`sessions.create`, at every sign-in, first deletes the session rows that
ended longer ago than a session's hard lifetime (`sessions.sweep`);
`uploads.slot` removes the rows of the person's own lapsed, unused uploads;
and `org_accounts.identity`
refreshes an org's account at the CI when the CI's implementation says its
state needs it, under a lock on its row so two callers refresh it once.
Woodpecker keeps the account's login at the forge fresh only while the
account calls it, and that login lasts as long as the forge's refresh
token, which deploy sets to the session's hard lifetime; so its
implementation says a sign-in older than two thirds of that lifetime (20
days by default) needs refreshing, and an org that grades every day signs
in again every 20 days and one that was quiet for months on its first use.
A state the CI refuses for any other reason is mended by
`org_accounts.renew`: a grading's start and a task's activation that the CI
refuses as the org's account refresh it and try once more, and `renew`
hands a caller the state another caller already refreshed, so a burst of
refusals refreshes once. Woodpecker's refresh sets a fresh password at the
forge, signs in with it and throws it away.

## Layer rules

Seven import-linter contracts in `pyproject.toml`, run by `lint-imports` in
CI, so a cross-layer import fails the build:

- `domain`, `services` and `db` never import anything under `adapters`.
- Only `runtime` and `testing` import `adapters` themselves; `api` reaches an
  adapter through `runtime`.
- `adapters.git`, `adapters.ci`, `adapters.objects` and `adapters.mail` never
  import each other, so a CI reaches the git host only through `CiHost`.
- The shared adapter modules (`browser`, `cached`, `fake_world`, `http`,
  `ids`) import no group, so only `adapters/__init__.py` and
  `adapters/fakes.py` join them.
- No adapter imports `services` or `db`.
- The fakes never import `services`, `db` or `runtime`.
- `domain` imports nothing else in the package, `port` included.

## Logging

Every module logs through `forge.log.get_logger` with an event name and named
fields, never a formatted sentence, and nothing prints. Each record is one
JSON object: `time`, `level`, `logger`, `event`, then the fields. A
`SecretStr` given as a field is written as `**********`. The process that
hosts the package calls `forge.api.log.setup()` once at start, before
anything logs. It reads `UNICON_LOG_LEVEL` and sends every logger in the
process through the one JSON handler, the web server's included, and the
host writes its own records through `forge.api.log.get_logger`. The HTTP
clients' own loggers, `httpx` and `httpcore` and the object store's `boto3`,
`botocore`, `s3transfer` and `urllib3`, log from a warning up whatever the
level, since the lines they write for every request hold its whole URL or
its signed headers, and a URL of the CI's sign-in holds a one-time code.

## Testing

`forge.testing` is a pytest plugin, loaded with `-p forge.testing` or from a
`conftest.py`. It gives a test a migrated Postgres of its own, the in-memory
forge with two users, a clock the test can move, a `setup` over them, and a
`ctx`, one unit of work committed when the test ends. A test calls a building
block with `ctx`, and an action either with `ctx`, to run it inside that unit
of work, or with `setup`, to have it commit one of its own. `held_setup` makes
`setup` the one forge holds for the test, for code that calls actions with
neither, and lets it go when the test ends, passed or failed. A dependant
loads the same plugin, so its tests run the package the way the backend does,
with no fixtures of its own to keep in step. Since a dependant imports nothing
but `forge.api` and `forge.testing`, the plugin also re-exports what its tests
arrange the fake with: `FakeForge`, `FakeClock`, `APP_URL`, `FORGE_URL`,
`OrgId`, `AsUser` and `Visibility`, `Settings` for an override, and `Setup`
to type the setup a fixture hands over. The setup files what a test names
under its name, so a test writes `acme/spring/sum` for the task it made as
`sum` in `spring` in `acme`; `setup_with_random_keys`, and
`held_setup_with_random_keys` for a dependant, make random keys as a
deployment does, for the tests that tell a name from its key, and
`name_places(setup, *ids)` names ids a test made straight at the fake. `logged(caplog, event)` gives back the
records a test caused as the JSON objects they are written as, and
`register_contestant(setup, contest, user_id)` writes the row that makes
someone a contestant, with a status and an extension. `seed_classic(fake)`
puts the built-in workflow `unicon/classic@v2` at the fake from `CLASSIC`, a
copy of the file deploy's bootstrap seeds, and with `seed_primitives` the
three primitives it uses from `PRIMITIVES`, each primitive repo's own
`primitive.yaml` with an image of `PLACEHOLDER_DIGEST`, so a task's first
save finds a workflow and every step's image; the package's tests check the
copies against deploy's file and the primitives' own when those repos are
checked out beside this one.
The fake's large-file store is `fake.uploads`: a test plays the browser
through the door with `send`, handing back the address the door gave out,
or puts an object straight into a place with `put`, and `forget` drops one
the way the forge's collector would. The grading machine's results go to
`fake.objects` with `put(url, content)`. `fake.racing_submissions = n` makes the next submission
collide with `n` others for its number, and `fake.lose_submission_answer`
names it and then fails as if the answer were lost. The fake CI signs the
question it asks the extension with a key of its own:
`fake.grading.config_request(task, variables, now=)` is that question;
`fake.ci.refuse_starts = n` answers the next `n` starts without a run,
and `fake.ci.lose_start_answer` starts the next run and fails as if its
answer were lost. A run of a task not in `fake.ci.activated` is
`NotFound`, as at a CI that does not know the task. The fake refuses a user id
it already has, since the accounts the package makes take the next free ids.

## Checks

What CI runs, in the same order:

```sh
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run lint-imports
uv run mypy
UNICON_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres uv run pytest -n 8
uv build
```

The service and migration tests need a real Postgres, on the server the URL
names; without the variable those tests are skipped. `-n` runs the tests in
that many processes side by side (`pytest-xdist`); a run without it is one
process. Each process migrates one template database and copies from it one
database that all its tests share, emptied after each test, so a test starts
with every table empty without a database being made for it. The tests that
move the schema up and down take a copy of their own instead. The template
and the shared database are kept on the server between runs, under names
carrying a hash of the migrations (`unicon_template_<process>_<hash>`,
`unicon_shared_<process>_<hash>`): a run that finds them starts at once, and
one after a migration changed builds new ones and drops the old. On this
repo's own laptop the whole suite took 800 seconds as one process making a
database per test, and takes about 70 on eight. The tests under `tests/live/` drive the Forgejo
implementation against a running Forgejo, named by `UNICON_LIVE_FORGE_URL`
with an administrator token in `UNICON_LIVE_FORGE_ADMIN_TOKEN`, whose own
account is the platform account; they are skipped without both, and
`-m "not live"` leaves them out. The tests that sign an org account in at
the CI need a Woodpecker signed in through that Forgejo as well:
`UNICON_LIVE_CI_URL`, `UNICON_LIVE_CI_PUBLIC_URL`, `UNICON_LIVE_CI_ADMIN_TOKEN`
and `UNICON_LIVE_FORGE_PUBLIC_URL`, the URL the CI sends a browser to; they
are skipped without all four. `tests/live/test_objects.py` drives the S3
store against a running Garage named by `UNICON_LIVE_S3_ENDPOINT` with the
platform's key in `UNICON_LIVE_S3_ACCESS_KEY` and `UNICON_LIVE_S3_SECRET_KEY`,
and is skipped without them. `tests/live/test_organiser_path.py` walks the
whole path over a real setup, the test Postgres included, and the check that
history cannot be rewritten pushes with `git` to a repository it made.
`tests/live/test_grading.py` reads the CI's signing key and starts a
grading's run as the org account once its row commits.
`tests/live/test_grading_run.py` takes one grading from its queued row to its
result on a grading machine: the test process serves the three machine
routes through the package's actions and points its task repository's
configuration extension at itself. It needs `UNICON_LIVE_MACHINE_HOST`, the
name the CI and a step container reach the test's machine by
(`host.docker.internal` on Docker Desktop), the images in
`UNICON_LIVE_HARNESS_IMAGE` and `UNICON_LIVE_CLONE_IMAGE`, and the object
store's variables, and stops, saying why, when the run's checkout cannot
reach the forge's public URL. `tests/live/test_undo.py` drives the removals
a failed create uses and then an org and a task whose commit fails, and
checks that nothing they made is left at Forgejo or Woodpecker; its first
two tests need only the forge.

A few tests compare the package's copies with their originals in the repos
checked out beside this one: `schemas/` with `runner`'s, `CLASSIC` with the
workflow `deploy` seeds, and `PRIMITIVES` with each `primitive-<name>`'s
`primitive.yaml`. Without a repo they are skipped, unless `CI` is set, when
they fail. CI checks the forge out beside them: the runner at the release
whose contracts `schemas/` copies, a `ref:` in `.github/workflows/ci.yaml`
that moves with the copy, deploy's `main`, and each primitive at the release
whose declaration `PRIMITIVES` copies, which moves the same way.

## Releasing

Push a tag `v1.2.3` on `main`. The release workflow refuses a tag whose commit
is not on `main`, checks that the tag, the version in `pyproject.toml` and
`forge.__version__` are the same number, runs the CI workflow itself on the
tagged commit, Postgres included, builds the wheel and the sdist, and
attaches both to a GitHub release. A dependant pins that release.

## Licence

MIT. See `LICENSE`.
