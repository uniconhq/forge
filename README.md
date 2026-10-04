# forge

The Unicon contest API as a Python package, `unicon-forge`. It knows what a
contest is and nothing about HTTP: the domain types, the services, the
database tables and their migrations, the forge port, which is the interface
this package calls a git host through, the implementations behind that port,
and the pytest plugin a dependant tests with. The backend is an HTTP shell
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
    setup.py         the package set up for one process; start, ready, stop and now
  domain/            the types and their rules, the definition files, the plans,
                     a publication and its note, the release rules,
                     grading's rules and clock,
                     the runner's contract files in schemas/ and the clock;
                     imports nothing else in the package
  port/              the interface a git host is called through, one area per module
  services/          the actions and their building blocks, the cookies and the
                     credential at rest; the only layer that writes to the database
  db/                the tables, the engine and the migrations
  forges/__init__.py build, which picks the implementation the settings name
  forges/ids.py      how both implementations name repositories and build and
                     read ids
  forges/forgejo/    the port over Forgejo and Woodpecker, including the sign-in
                     dance that mints an org account's CI token
  forges/fake/       the port in memory, one module per area over one state
  forges/cached.py   the port wrapped in a small per-read cache
  forges/README.md   what any git host must provide to sit behind the port
tests/
  test_*.py          the domain, the front door, the port's shape, the settings,
                     the log, the command and the plugin
  runtime/           the setup, the actions over it, and the memo
  forges/            each implementation against the port's contract
  services/          the services over a real Postgres and the fake
  db/                the migrations, up and down
  live/              the Forgejo implementation against a running Forgejo, and the
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
  orgs.py       create, create_by_operator, update
  contests.py   create, list, and contest_id_of, the id of the contest a scope
                is in
  tasks.py      create, list, state, the TaskState state returns, and
                task_id_of, the id of the task a scope names
  workflows.py  create, and the NewWorkflow it returns
  files.py      read, tree, history, write, rollback, and the File, TreeEntry,
                EntryKind and Change they return
  publications.py  save, list, and the Published, Draft and Publication they
                return
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
  events.py     check, EVENTS_PATH, where the door is, and SIGNATURE_HEADERS, the
                headers the signature comes in
  uploads.py    slot, task_file_slot, complete, door, the Slot a slot is, the
                Upload complete returns with its UploadStatus, and the Door
                the proxy is answered with
  submissions.py  submit, mine, one, files, file, run_log, the SubmittedInput
                submit takes, and the Submission, Result, SubmittedFiles,
                GradingStatus and Show they return
  gradings.py   cancel, retry, rejudge, list, task_of, and the GradingRecord,
                Rejudged and GradingStatus they return
  runs.py       config, envelope, callback, the CiRequest config takes and the
                CiAnswer it returns, GradingStatus, and CI_CONFIG_PATH,
                ENVELOPE_PATH and CALLBACK_PATH, where each is served
  roles.py      holders, grant, revoke, and the Holder holders returns
  cookies.py    what goes into the two cookies and what comes out, and the policy
  log.py        setup, get_logger, and the Logger it returns
  errors.py     every error the package raises to its callers
  types.py      Session, User, Role, Scope, ScopeKind, RoleGrant, HeldRole,
                Named, ScopeNames, OrgId, ContestId, TaskId, VersionId,
                PublicationId, ConflictToken, Edit and Problem
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

`forge/domain/definitions.py` reads `contest.yaml` and `task.yaml` into
models, and `workflow_definition.py` reads a `workflow.yaml`, with the YAML
loading and the error paths they share in `yaml_models.py`. Each refuses a key
the format does not know and reports every problem as `InvalidDefinition`,
whose `errors` pair a YAML path such as `leaderboards[0].order[1].direction`
with a sentence a form shows beside that field. `admin_only_changes` names
the admin-only keys a save changes, `missing_files` names each file input
whose path is not in the state being saved, at the YAML path of its value,
`starter_contest` and `starter_task` are the files a new contest or task
is created with, and `starter_workflow` the `workflow.yaml` a new workflow
is. `primitives.py` reads a primitive's `primitive.yaml`, its
image by digest, whether it batches, its limits and the limits it raises
from an input, and its typed inputs and outputs. `plans.py`
is the compiler (below) and names what changed how a task grades between two
publications. `submissions.py` lays out what a contestant gives as the files
and `submission.json` of one commit, and `uploads.py` holds the rules of an
upload's slot and parts. `grading.py` holds what a grading run is, its two
secrets and its clock, `reports.py` what a run reports back and which
verdicts are kept, and `contracts.py` checks a document against `schemas/`,
a copy of the five contract files of the runner release the package pins,
which the tests check against the runner's own when that repo is checked
out beside this one.
`release.py` works out from the settings and the clock whether a task is
released, visible and open to one contestant, and who sees a contest at all.

## The port

`forge/port/` declares every operation the platform needs from a git host,
in the platform's words, as one `Protocol` per area: `identity`, `orgs`,
`content`, `workspaces`, `threads`, `workflows`, `primitives`, `grading` and
`computes`. `Forge` composes the areas, so a service reaches a host as
`ctx.forge.orgs.grant_role(...)` and an implementation is a set of area
classes over one HTTP client. Every reference the package stores is an id
the port hands out, built from keys (below). Above the port only
`domain/roles.py` reads one, turning a contest's or a task's id into the
scope its roles are held at and back; every other id is opaque. Every failure is one of five
typed errors: `NotFound`, `Forbidden`, `Conflict`, `Rejected` and
`Unavailable`. Retries with backoff live inside the implementation, so a forge
that is busy reaches the services only as `Unavailable` once the retries are
used up. A retry never makes something twice: a request that sets a state is
retried on a busy server or a lost answer, and a request that creates
something, a POST, only when it never reached the server; otherwise it is
`Unavailable` at once and the request that sent it fails.

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
account's two credentials.

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
`Submitted` with its number, version and key, and `read_submission_file` one
of its files at its version, big ones included.
`grading.activate` takes a task for grading at the CI, as the org account.
`grading.start_run` starts a `GradingRun`, what one run of one grading is
in the platform's words, as the org account, with the variables
`grading.run_variables` writes for it, and `grading.cancel_run` stops one as
the CI's administrator.
`grading.read_config_request` checks the CI's signed question of what a run
is and reads it as a `ConfigAsk`, and `grading.config_answer` answers it with
the run's three steps; `grading.run_places` says what the envelope names of
the run's places at the forge. The Forgejo implementation writes the
Woodpecker side of each (below).
`workspaces.publish` names a version a save already wrote as the next
publication, with a note, and `workspaces.list_publications` reads each back
as a `Publication` with what its note says. A contestant's workspace is made
one part at a time, each of which can be run again and keeps what is
already right: `workspaces.open_workspace` makes the desk and gives the
members write access to it, `workspaces.open_submission_place` makes the
place to submit one task, reserves its submissions for the platform and only
then gives them write access there, and `workspaces.close_workspace` takes
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
to the person's own credential, that they may write it. `copy_workflow`
reads the source as the person and makes the copy the same way. Who may
create a workflow under an owner, the person themself or a manager at the
org, is the services' rule (`workflows.create`, below); the rest of these
operations are called by nothing yet, since the workflow pages are feature
10.

The org account is made by the platform too, through six more operations
the port declares: `identity.create_user` and `mint_token` make the account
at the host and its credential there, `orgs.ensure_account_membership` puts
it in its place in the org, `grading.create_ci_user` and `mint_ci_token`
make its user at the CI and sign it in there, and `identity.set_password`
gives it a fresh password whenever it has to sign in again. The CI admits
nobody it was not told about and mints a token only through its web UI, so
`mint_ci_token` in the Forgejo implementation is a real sign-in: it signs
into Forgejo with the account's password, walks Woodpecker's OAuth round
trip approving consent, reads the CSRF token Woodpecker hands its own page,
and asks for a token. It runs inside the platform's process, where the two
public URLs do not resolve, so it follows every redirect by hand and asks
each URL at the internal host instead.

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
whether a place holds an object. The Forgejo implementation addresses its
git-lfs endpoints and presents the person's own access token in the password
half of a Basic header, which is the only form those routes take; Forgejo
hashes what arrives and keeps nothing that is not the digest and length the
address names. The fake holds its objects in memory, refuses the same
things, and resolves a pointer the way Forgejo's media endpoint does.

The `objects` area (`port/objects.py`) is the grading run log store alone: a
URL a machine writes a result with, and reading one up to a size given. The
Forgejo implementation reaches Garage over S3 with boto3 at
`UNICON_S3_ENDPOINT` and signs what a machine is handed for
`UNICON_MACHINE_URL`, path-style; the proxy passes `/unicon-results/` to
Garage with the Host header unchanged.

Both implementations name repositories and build and read ids with the one
grammar in `forges/ids.py`, so an id from elsewhere is `NotFound` whichever
is behind the port.

`UNICON_FORGE=forgejo` runs against Forgejo and Woodpecker; `UNICON_FORGE=fake`
runs the whole stack against the in-memory forge, which records every call
with its identity and refuses what a real forge refuses, a repository made
by anyone but the platform among it. `CachedForge` wraps
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
role team's. The names people give them are labels in the `names` table,
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
behind the port, which `forges.build` picks and configures from the settings,
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
`settings.forgejo`, read from the `UNICON_FORGE_*` and `UNICON_WOODPECKER_*`
variables, and the object store's as `settings.s3`, read from
`UNICON_S3_ENDPOINT`, `UNICON_S3_REGION` (`garage` unless given),
`UNICON_S3_ACCESS_KEY`, `UNICON_S3_SECRET_KEY` and
`UNICON_S3_RESULTS_BUCKET` (`unicon-results`); both are
required only when `UNICON_FORGE=forgejo`. `UNICON_MACHINE_URL` is where
grading machines reach the platform, `UNICON_PUBLIC_URL` unless given.
`UNICON_HARNESS_IMAGE` is the harness every plan names, by digest, and
`UNICON_CLONE_IMAGE` the image the CI checks a task and a submission out
with, by digest, each the one of the runner release the package pins unless
given. A missing or malformed variable stops the process at start with the
variable named.

## Cookies

`forge.api.cookies` makes and reads what the host puts in its two cookies.
`session_value(session)` is the session id signed under
`UNICON_SESSION_SIGNING_KEY`; `session_id(value)` gives the id back, or none
when the value is empty, forged, signed under another key or older than the
session's hard lifetime, so a forged id is refused without a database read.
`sign_in_value(attempt)` and `sign_in_attempt(value)` do the same for what
checks a sign-in's answer, for `UNICON_SIGN_IN_TTL`. `policy()` says whether
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
| `orgs` | `create`, `create_by_operator`, `update` |
| `contests` | `create`, `list` |
| `tasks` | `create`, `list`, `state` |
| `files` | `read`, `tree`, `history`, `write`, `rollback` |
| `publications` | `save`, `list` |
| `release` | `of_task` |
| `contestants` | `register`, `mine`, `list`, `approve`, `reject`, `reopen`, `remove`, `extend` |
| `contest_home` | `contests`, `home`, `task` |
| `landing` | `contests`, `contest`, `statement` |
| `events` | `check` |
| `roles` | `holders`, `grant`, `revoke` |
| `uploads` | `slot`, `complete` |
| `submissions` | `submit`, `mine`, `one`, `files`, `file`, `run_log` |
| `gradings` | `cancel`, `retry`, `rejudge`, `list`, `task_of` |
| `runs` | `config`, `envelope`, `callback` |
| `workflows` | `create` |

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
leaves the removal of what it made there (below).

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
as its CI user, its service account's place in the org, the account, and
the org, which takes its roles, labels, event push and first admin's role
with it; the account must leave its place before Forgejo deletes it. A
contest is undone as its place with its own roles. A task is undone as its
entry in `contest.yaml`, taken out only while the file still says exactly
what the create wrote, its activation at the CI, and its place with its own
roles. The port operations the undo uses are `orgs.delete_org`,
`orgs.remove_account_membership`, `identity.delete_user`,
`content.delete_place`, `grading.deactivate` and `grading.delete_ci_user`.
A name is one row in `names`, so two requests for one name at once make one
thing and the second is `Conflict`. Each create answers with the `Named`
thing, its id and its name. An org takes ten steps: its account row, the
org, its roles, its labels, its signed event push, its first admin, its
service account at the forge, that account's place in the org and its forge
credential, its user at the CI, and its sign-in at the CI. The service
account's password is made for the request, mints the account's two
credentials and is thrown away, so it is never in the database. A service
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
takes four: its place with `task.yaml`, `statement.md` and an example
testcase in `data/testcases/`; its roles and protection, its publications
reserved for the platform; its activation at the CI as the org's own
account, which takes it for grading, trusts it for `volumes` and nothing
else, and leaves it with no webhook, since the platform starts every run
itself; and its entry at the end of the `tasks` list in `contest.yaml`.
Nothing is published until the first save. A contest's tasks are the tasks
there are at the forge, which `tasks.list` reads as the organiser; the
`tasks` list in `contest.yaml` orders, labels and scores them. The last step
writes the new task's entry as the platform, with the next free letter as
its label and 100 points, into the file's text so its comments and layout
stay (`domain/contest_entries.py`); a `contest.yaml` that does not read, or
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
commit, a `workflow.yaml` named `<owner>/<name>` at version `v1` with the
steps of `unicon/classic@v1`, valid as written; the workflow is private. A
workflow's name is not reserved in `names`: the forge holds one place per
owner and name, and a name the owner has already is `Conflict`. Any other
failure at the forge is told in fixed words and logged, like a create's. It
answers with a `NewWorkflow`: its id, and the owner and name a person calls
it by, since an org's id is built from its key.

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
valid, and a manager's change to one of its admin-only keys is refused as
`AdminOnly`, naming each; nothing is written either way. A write to a task
is a save of the task, below. A rollback reads the file at the chosen
version and writes it back as a new change through `write`, so a task's
rollback is a save too and the history stays whole.

## The save

There is no publish button. `publications.save(organiser, task, changes)`
takes each path with its new content and the token it was read with, as an
`Edit`, and runs these steps in order, refusing before anything is written:

1. A path inside `plans/` is refused with `ReservedPath`: the compiler is
   the only writer there. A manager's save that changes the name or the
   `limits` of `task.yaml`, or touches `statement.md`, is refused with
   `AdminOnly`, naming each. An admin passes.
2. The state being saved, the files at the head with the save's over them,
   is checked: `task.yaml` validates, every file it names is there, every
   workflow it names and every primitive their steps use is read at its
   version as the organiser, and one plan per stage compiles over the files
   of that state. When the latest publication used a workflow of the same
   `<owner>/<name>` and that name is now another workflow, by the forge's own
   id for it, the state is refused at that line: the owner may have been
   renamed and the name taken by someone else. A state that fails is a `Draft`: the organiser's
   files are written as one change, nothing is published, and the last
   publication keeps grading. The errors come back with their YAML paths
   and are not stored; `tasks.state` checks the head again whenever it is
   not what the latest publication froze.
3. What the save changes about how the task grades is worked out against
   the latest publication: its plans, the data files its settings name and
   its limits. While the contest runs, a save that changes any of them is
   refused with `ConfirmationRequired`, listing the changes, and nothing is
   written; the same save with `confirm=True` publishes. A save with
   `keep_as_draft=True` is written as a draft that says what it held back
   and publishes nothing, on any save, valid or not, running contest or not.
4. The organiser's files and every `plans/<stage>.json` are written as one
   change, as the organiser; a plan of a stage the task no longer has is
   removed in the same change.
5. That change is named as the next publication, as the platform, with a
   note saying whether it changed how the task grades and what, and which
   workflow each workflow name was, by the forge's own id for it. A save that
   changes nothing since the latest publication publishes nothing new. When
   another save landed between the check and the write, the change holds
   files this save never checked, so it comes back as a `Draft` saying so,
   and the next save checks and publishes the task as it then stands.

A valid save comes back as `Published`, with the publication, its number,
and whether it changed how the task grades and what. `publications.list`
gives every publication with its flag and its changes, for the task's
history.

## The compiler

`forge/domain/plans.py` compiles each stage into the plan the harness runs,
the runner's `plan.schema.json` version 4, flat and fully resolved, so
nothing is read at grade time: the harness image, the stage, the test list,
the steps in order and the verdict block. Each `use:` is the primitive whose
declaration the save read; a `use:` that is someone else's private workflow,
or not there, is an error naming it, and a workflow used as a step waits for
feature 10. Each step carries its primitive's image by digest and its
limits, and its container runs the image's own entrypoint. Each `with`
value becomes one of the plan's values: a literal, a file or list of files
in the task, a contestant input or the language chosen for it, or an earlier
step's output. A `foreach` over a setter's `file[]`
input runs over the tests in its folder at the version being saved: the
files directly in it, hidden ones left out, grouped by stem, `1.in` and
`1.ans` the test `1` with the fields `input` and `answer`, ordered with
numbers compared as numbers; an empty folder is an error at the input's YAML
path. A step whose primitive declares `batch: true` takes every test in one
container, and one that does not is one step per test. Inside a `foreach`, a
step of the same list is read for the same test. Limits are the
declaration's, raised by `limits_from` from values known at the save, and a
batch's time and CPU are summed over its tests. It checks what grading needs:
every input given is declared, every required one is given, every output read
is declared, and each value has its input's type, a language list included
against the enum the compile step takes. The workflow's `outputs` become the
verdict block: `outcome` required, `metrics`, each test's `time_ms` and
`memory_kb`, and `summary`. Every problem is reported at the YAML path in
`task.yaml` of the workflow it is in. The plan is the same bytes every time
for the same state. A new task's starter carries one example test, so its
first save publishes.

`release.of_task(session, task)` says whether the signed-in person sees the
task and may submit to it now: released, visible and open, and why not. It
reads `contest.yaml`, the `task.yaml` of the latest publication and the
person's own time extension on their `contestants` row, all as the
platform, and a task with no publication is not released. Nothing at the
forge changes when a task becomes released.

## Contestants

A person asks to join a contest with `contestants.register(session,
contest)`, with the code the contest asks for when it asks for one. A
request nobody has approved has nothing at the forge, so all of it is a row
in `contestants`. The contest has to be one they see: published, and
`public` or `signed-in`. Then, stopping at the first refusal, each with a
code of its own: the registration window is open (`registration_closed`);
they hold no role at the contest, its tasks or its org (`is_staff`), read
under the org's lock on role changes, the one `roles.grant` takes, so a
grant and a registration never pass each other; they have no registration
there already (`already_registered`); the invite, the code and the email
address the contest asks for (`invite_required`, `wrong_invite_code`,
`domain_not_allowed`), where a pattern must match the whole of one of the
addresses the forge has confirmed are theirs, whatever its case, within a
time limit, since the pattern is an organiser's; an address counts only as
far as the forge confirms it, so with Forgejo that needs
`REGISTER_EMAIL_CONFIRM` on wherever people sign themselves up; and a place is free (`contest_full`), counted under an
advisory lock on the contest, so the last place goes once. Nothing makes
invites yet, so a contest that asks for one refuses everyone
(`invite_required`). The row is written pending with what let it through;
with `approval: auto` it is approved in the same call. The rules themselves
are in `forge/domain/registration.py`.

An organiser managing the contest decides: `approve` a pending registration,
`reject` a pending one with a reason the person reads (`invalid_reason`
without one), `reopen` a rejected one, which leaves it pending again and is
refused like a new registration when the person holds a role there by now
(`is_staff`) or every place is taken (`contest_full`), `remove` an approved
one, and `extend` a pending or approved one, which gives that person more
time past the contest's end (`invalid_extension` below nothing or past a
year). A decision from any other status is `wrong_status`, naming the
status. `list` gives every registration of the contest to anyone observing
it, oldest first; `mine` gives a person their own.

Approval makes nothing at the forge. A contestant's workspace is made a part
at a time when it is first needed: their place to submit a task is made at
their first submit to it (below). Its id comes from the contest and the
person's user id (`workspaces.workspace_of`), so the contestant's row holds
no workspace. Removing a contestant takes their access to whichever parts
were made away and keeps what is in them.

What a signed-in person reads of a contest is `contest_home`: `contests`,
every contest they see with their own status; `home`, a contest's dates,
their registration, what the register form needs, their own deadline, which
is the end plus their extension, the server's clock and the tasks released
to them in the contest's order; and `task`, a visible task's statement,
limits and the inputs a contestant gives, with the labels, languages, file
types and sizes a submit panel shows, and nothing else of what it holds. A visitor with no session reads
`landing`: the public contests, one with its released tasks, and a released
task's statement. The list of public contests is kept by each process for
five seconds (`ctx.memo`, `forge/runtime/memo.py`), since anyone may ask for
it and it reads every org's contests; visitors arriving while it is read wait
for that one read. All of it is read live as the platform from the latest
publication of each task, with the contest's visibility and the release
rules applied first, and a contest or task the reader may not see is no
such contest or task, the same answer as one that is not there.

## Uploads

A person's files go from the browser into the forge's own large-file store
through the upload door, and never through the platform.
`uploads.slot(session, task, input=, filename=, size=, sha256=,
content_type=)` needs the person to be able to submit to the task now, the
same checks a submit starts with, the input to be one of the task's code,
file or file[] inputs, the name to be one plain name the input's `accept`
takes, the digest to be a SHA-256 in lowercase hex, and the size to be within
the input's `max_size` and the task's `limits.max_size`, and never above the
platform's ceiling (`too_large`, naming the limit and the input whose it is);
a save that sets a larger limit is a draft, with the problem at that limit's
path. A person holds at most 200 uploads for a task that no submit has used,
declaring at most twice the task's submission limit in bytes together, the
one asked for included (`upload_limit`, with `limit` and `bytes`). The count
is taken under an advisory lock on the person and the task, held until the
unit of work ends, so two slots asked at once cannot both pass.

It then makes the person's place to submit the task, if no slot of theirs for
it has been kept before, since an object belongs to a place and there has to
be one to put it in; asks the forge whether the place already holds that
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

An upload no submit used is removed, object and row, once its two days are
over, the next time its owner asks for a slot: before the count is taken,
`slot` removes the person's own lapsed, unused uploads, and leaves one whose
object the store failed on for the next time. The object of an upload a
submit used is removed once the submit commits (`uploads.forget`), since its
bytes are in the submission's commit, and its row stays `consumed` as the
record of what was submitted.

## Submissions

`submissions.submit(session, task, inputs, idempotency_key=)` runs in this
order and stops at the first refusal, before anything is written, each with a
code of its own: the task is open by the server's clock plus the
contestant's extension (`task_closed` with its `reason`, or `archived`); they
are approved (`not_approved`); they have submissions left (`submission_limit`),
counted from the submissions at the forge; the task's rate holds
(`rate_limited`, with `retry_at`), counted from the grading rows within its
window; every upload named is theirs for this task (`upload_not_yours`), a
checked file no submission used (`upload_not_ready`), and each and all of
them within the sizes allowed (`too_large`); and what is given fits the
task's contestant inputs (`invalid_inputs`, each problem at its input). The
bytes are read back and checked against the digest the upload was verified
with. At a contestant's first submit to the task, their place to submit it
is made, as the platform, once they are found approved and before their
submissions are counted, which is the one thing a later refusal leaves at
the forge. It is made under a lock on their `contestants` row, so a removal
waits for it and then takes the access away again, and one removed by then
is `not_approved`. Then the files go in as one commit as the
contestant, `files/<input id>/<file name>` beside `submission.json`, named
`submission/<n>` as the platform; one `queued` grading row is inserted per
stage graded on submit, against the task's current publication, attempt 1,
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
are found by the key, unique for a workspace, task and stage, and when the
forge's writes landed but the rows did not, the submission is found at the
forge by the key its note carries and only its rows are inserted. A
submission named at the forge whose rows never landed is one the contestant
saw fail, and submitting again with the same key finishes it.

`mine` lists the signed-in person's own submissions of a task, newest first,
`one` gives one by its number, each with the latest attempt of its grading at
every stage as that stage's `show` allows: `full` the outcome, metrics,
summary, each test's row and whether there is a log, `metrics` the outcome
and metrics, `hidden` the status alone. A `system_error`'s summary is written
for staff and is never shown, whatever the stage's `show`. `files` gives the inputs one was made
with, as its `submission.json` names them, and `file` one of those files.
`run_log` gives the bytes of the run log of the latest attempt at a stage,
the first stage in the task's order with one unless a stage is named, only
where that stage's `show` is `full`, and only when it is at most 9 MiB
(`log_too_large`, with `limit`): the harness cuts its log to 8 MiB and a
line, and the URL it writes with takes any length, since a presigned PUT
cannot cap one, so the read is bounded instead and never takes more than
the limit and a byte. Anyone else's submission is no such submission.

## Grading

A grading is one row of `gradings` per submission, stage and attempt, and
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

**Starting a run.** Every new grading row hands `gradings.start` to
`ctx.after_commit`, so its run is started right after the unit of work that
made it commits, on a unit of work of its own: the CI asks the platform
about the grading while the start is under way, and must find the row
committed. Up to eight such starts run at once. `start` passes over a
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
request as it arrived, a `CiRequest` of method, target, headers and body. The
Forgejo implementation checks its RFC 9421 signature, over the request target
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
`/woodpecker/submission`) set, `lfs` on for the task alone, the machine's
store of large files for the task's org as `unicon-lfs-<org>:/lfs-cache`, one
per org, so no org's task is served a large file another org's task brought
to the machine by naming its object id, and no `environment`, since a clone
step with one is lent no credential; and one step, `grade`, running the
harness image the stage's plan names in the publication, with the socket
filter's socket mounted read-only as `unicon-filter:/run/unicon:ro`, which
the harness connects to and cannot replace, `DOCKER_HOST` naming it, and no
credential. Anything else is `CiRequestRefused`, never an
empty answer, with the reason in the log. The action writes nothing, so it
never waits on the start holding the row.

**The envelope.** `runs.envelope(grading, key)` is the runner's
`envelope.schema.json` version 4, served once: only with the envelope key of
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
the grading, stage and attempt, the submission as the forge names it, the
two checkouts, the callback URL and token, a URL the harness writes its log with
into `unicon-results` at `logs/<grading id>/<attempt>.log`, signed for
`UNICON_MACHINE_URL` until the deadline, the deadline, and
`limits.wall_seconds`: the plan's step time limits summed with fifteen
seconds for each container and a minute for the run, never more than 25
minutes. The times agree with the CI: a run is given the 30 minutes of
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
the row as `{"step", "done", "total"}`, and `finished` carries the verdict: one
that matches the runner's `verdict.schema.json` version 4 is kept on the
row with its log key, and the grading is `done`, or `system_error` when the verdict's outcome says so. Any
other verdict, one of more than 1 MiB as JSON included, leaves the grading in
`system_error` with the reason in `error`, and is taken, since sending it
again would not mend it. A kept verdict sent again after its answer was lost
is answered the same. The
action answers the grading's status after the report.

**Reconcile.** A submission is named at the forge before its rows are
inserted, and a database restored from a backup lacks the gradings of every
submission made since, so `unicon-forge reconcile`, which the operator runs
once after a restore, reads them at the forge. Over every contest of every
org the platform made, for every published task and every person with a row
in `contestants` for the contest, whatever became of their registration, it
lists that person's submissions of the task and inserts, for any with no
grading, one queued grading per stage graded on submit against the current
publication, carrying the idempotency key its tag's note carries. Their runs
start once it commits. It logs what it did as `reconcile.done`.

**The organiser's controls.** Each takes the `Organiser` from
`access.organiser` and needs manager at the grading's task; a grading whose
task they do not observe is no such grading. `gradings.cancel` stops a
grading that is not finished, at the CI too when a run of it is there
(`WrongStatus` for a finished one). `gradings.retry` makes a new attempt of a
finished one against the publication it graded against, while no other
attempt of it is being graded (`Conflict`). `gradings.rejudge(task)` makes a
new attempt of every submission's latest attempt at every stage the current
publication has, against it, cancelling first one still being graded against
an older publication and leaving one being graded against the current one,
and answers a `Rejudged` with its counts. `gradings.list(task)` gives the
task's gradings, newest first, at most 500, to anyone observing the task, as
`GradingRecord`s with the verdict whole. A route that names only the grading
checks the organiser at the task `gradings.task_of(grading)` gives.

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
| `TaskClosed` | `task_closed` | `reason`, `ended` or `submissions_closed` |
| `SubmissionLimit` | `submission_limit` | `limit`, the submissions allowed |
| `RateLimited` | `rate_limited` | `rate`, such as `1 per 30s`, and `retry_at` |
| `TooLarge` | `too_large` | `limit` in bytes, and `input`, or none for the task's |
| `UploadNotYours` | `upload_not_yours` | `uploads`, each id refused |
| `UploadNotReady` | `upload_not_ready` | `uploads`, each id refused |
| `UploadLimit` | `upload_limit` | `limit`, the open uploads one person may hold for a task, and `bytes`, what they may declare together |
| `LogTooLarge` | `log_too_large` | `limit`, the largest run log shown, in bytes |
| `InvalidInputs` | `invalid_inputs` | `errors`, each `{"input", "message"}` |

The rest, `invalid_name`, `unauthenticated`, `session_expired`,
`fresh_sign_in_required`, `sign_in_invalid`, `sign_in_denied`,
`not_ready`, the registration refusals `registration_closed`, `is_staff`,
`already_registered`, `invite_required`, `wrong_invite_code`,
`domain_not_allowed` and `contest_full`, which share the base class
`RegistrationRefused`, `invalid_reason` and `invalid_extension`, and
`archived` (the contest is archived), `not_approved` and
`invalid_idempotency_key`, and grading's
`ci_request_refused` (the CI's request does not verify or names no grading
being started), `invalid_token` (a report without its grading's token),
`grading_closed` (the grading takes no envelope or report now) and
`invalid_callback` (a report that is not one), carry nothing beyond the
detail. The refusals of an upload or a submit share the base class
`SubmitRefused`.

## The tables

Six tables, keyed by UUID v7 but for `names`, keyed by the id it names,
with every enumeration as `text` under a `CHECK`: `sessions`,
`contestants`, `gradings`, `uploads`, `org_accounts` and `names`. They
hold what a forge cannot: of users, orgs, contests and tasks only the names
people gave the last three (above), the rest read live. `org_accounts` is
one row per org, by the org's id, its service account's
forge credential, CI credential and event secret each as AES-256-GCM
ciphertext under `UNICON_TOKEN_ENCRYPTION_KEY`, the way a session's
credential is, so a copy of the table hands out no access, and
`ci_signed_in_at`, when the account last signed in at the CI;
`services/credentials.py` is the one place either is sealed or opened.
A `contestants` row is one person's registration for one contest, and
names no workspace, since a workspace's id comes from the contest and the
person. A `gradings` row names the task, the workspace, the submission with
its number and the exact version its files went in with, when it was
submitted, the publication, the stage and attempt, and the idempotency key
of the submit that made it; its status is one of `queued`, `dispatched`,
`running`, `done`, `cancelled` and `system_error`. `queued_at` is when it
was queued, `run_id`, `dispatched_at`, `started_at` and `deadline_at` are
its run, when it was started, when its harness fetched the envelope and its
deadline, `progress` the last progress reported, `verdict` and `log_key`
what came back, and `error` a line for staff. An `uploads` row is one
browser upload: its owner, task and input, name, declared and measured
size, digest, status, the id of its parts while they arrive, the submission
that consumed it, and when its lifetime ends.
`unicon-forge migrate` reads `UNICON_DATABASE_URL`, applies the migrations
under `forge/db/alembic/` and exits. A deployment runs it before the host
starts, from the host's image, which has the package and its command
installed; the host has no migrate command of its own. `unicon-forge
reconcile` gives every submission at the forge without gradings its
gradings (above), and the operator runs it once after a restore.

There is no jobs table, and nothing in the package runs on a timer. Each
piece of upkeep is done by a request that already touches what it keeps:
`sessions.create`, at every sign-in, first deletes the session rows that
ended longer ago than a session's hard lifetime (`sessions.sweep`);
`uploads.slot` removes the person's own lapsed, unused uploads, and a submit
removes the objects it used once it commits; and `org_accounts.identity`
signs an org's account in at the CI again when its `ci_signed_in_at` is
older than `SIGN_IN_SHARE`, two thirds, of the session's hard lifetime (20
days by default), under a lock on its row so two callers sign it in once.
The CI keeps the account's login at the forge fresh only while the account
calls it, and that login lasts as long as the forge's refresh token, which
deploy sets to the session's hard lifetime; so an org that grades every day
signs in again every 20 days and one that was quiet for months signs in on
its first use. A login the CI refuses for any other reason is mended by
`org_accounts.renew`: a grading's start and a task's activation that the CI
refuses as the org's account sign it in again and try once more, and
`renew` hands a caller the credential another caller already renewed, so a
burst of refusals signs in once. Signing in again sets a fresh password at
the forge, signs in with it and throws it away.

## Layer rules

Five import-linter contracts in `pyproject.toml`, run by `lint-imports` in
CI, so a cross-layer import fails the build:

- `domain`, `services` and `db` never import anything under `forges`.
- Only `runtime` and `testing` import `forges` themselves; `api` reaches an
  implementation through `runtime`.
- `forges.forgejo` never imports `services` or `db`. It may import `port` and
  `domain`.
- `forges.fake` never imports `services`, `db` or `runtime`.
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
puts the built-in workflow `unicon/classic@v1` at the fake from `CLASSIC`, a
copy of the file deploy's bootstrap seeds, and with `seed_primitives` the
three primitives it uses from `PRIMITIVES`, each primitive repo's own
`primitive.yaml` with an image of `PLACEHOLDER_DIGEST`, so a task's first
save finds a workflow and every step's image; the package's tests check the
copy against deploy's file when that repo is checked out beside this one.
The fake's store is `fake.objects`: a test plays the browser with
`post(slot.fields, content)` and `put_part(url, content)`, which answers the
value `complete` takes for the part, and the grading machine with
`put(url, content)`. `fake.racing_submissions = n` makes the next submission
collide with `n` others for its number, and `fake.lose_submission_answer`
names it and then fails as if the answer were lost. The fake CI signs the
question it asks the extension with a key of its own:
`fake.grading.config_request(task, variables, now=)` is that question;
`fake.state.refuse_starts = n` answers the next `n` starts without a run,
and `fake.state.lose_start_answer` starts the next run and fails as if its
answer were lost. The fake refuses a user id
it already has, since the accounts the package makes take the next free ids.

## Checks

What CI runs, in the same order:

```sh
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run lint-imports
uv run mypy
UNICON_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres uv run pytest
uv build
```

The service and migration tests need a real Postgres; they create and drop a
database of their own on the server the URL names. Without the variable those
tests are skipped. The tests under `tests/live/` drive the Forgejo
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
verdict on a grading machine: the test process serves the three machine
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

## Releasing

Push a tag `v1.2.3` on `main`. The release workflow refuses a tag whose commit
is not on `main`, checks that the tag, the version in `pyproject.toml` and
`forge.__version__` are the same number, runs the CI workflow itself on the
tagged commit, Postgres included, builds the wheel and the sdist, and
attaches both to a GitHub release. A dependant pins that release.

## Licence

MIT. See `LICENSE`.
