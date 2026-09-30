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
  cli.py             unicon-forge migrate
  testing.py         the pytest plugin: a migrated database, the fake, a setup
  runtime/           how a call runs in a process
    context.py       what a building block runs with, and what an action takes
                     from a setup
    actions.py       the @action mark
    held.py          the one setup the process holds
    memo.py          answers the setup keeps for a few seconds
    setup.py         the package set up for one process; start, ready, stop and now
    background.py    the pollers and timed passes that run with nobody clicking
  domain/            the types and their rules, the definition files, the plans,
                     a publication and its note, the release rules, the steps
                     of making each kind of thing and the clock; imports
                     nothing else in the package
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
  runtime/           the setup, the actions over it, and the background loops
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
  access.py     organiser, and the Organiser it returns
  sign_in.py    start, complete, sign_up_url, SignInAttempt, and the SignInStart
                start returns
  orgs.py       create, create_by_operator, status, update, and the Record the
                first three return
  contests.py   create, list, status, the Record, and contest_id_of, the id of a
                contest in an org
  tasks.py      create, list, state, status, the Record, the TaskState state
                returns, and task_id_of, the id of a task in a contest
  files.py      read, tree, history, write, rollback, and the File, TreeEntry,
                EntryKind and Change they return
  publications.py  save, list, and the Published, Draft, Activation and
                Publication they return
  release.py    of_task, and the TaskRelease it returns with its Closed reason
  contestants.py  register, mine, list, approve, reject, remove, extend, and the
                Registration they return with its Status and WorkspaceState
  contest_home.py  contests, home, task, and the ContestSummary, ContestHome,
                TaskEntry and TaskPage they return, with the Limits and Rate a
                page carries and the State and ContestVisibility of a contest
  landing.py    contests, contest, statement, and the PublicContest, PublicTask
                and PublicStatement they return
  events.py     check, EVENTS_PATH, where the door is, and SIGNATURE_HEADERS, the
                headers the signature comes in
  roles.py      holders, grant, revoke, and the Holder holders returns
  cookies.py    what goes into the two cookies and what comes out, and the policy
  log.py        setup, get_logger, and the Logger it returns
  errors.py     every error the package raises to its callers
  types.py      Session, User, Role, Scope, ScopeKind, RoleGrant, OrgName,
                ContestId, TaskId, VersionId, PublicationId, ConflictToken, Edit,
                Problem, and scope_of_place, which reads the names back out of a
                contest or task id
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
and `starter_contest` and `starter_task` are the files a new contest or task
is created with. `primitives.py` reads a primitive's `primitive.yaml`, its
image by digest, its entrypoint, whether it batches, its limits and the
limits it raises from an input, and its typed inputs and outputs. `plans.py`
is the compiler (below) and names what changed how a task grades between two
publications.
`release.py` works out from the settings and the clock whether a task is
released, visible and open to one contestant, and who sees a contest at all.

## The port

`forge/port/` declares every operation the platform needs from a git host,
in the platform's words, as one `Protocol` per area: `identity`, `orgs`,
`content`, `workspaces`, `threads`, `workflows`, `primitives`, `grading` and
`computes`. `Forge` composes the areas, so a service reaches a host as
`ctx.forge.orgs.grant_role(...)` and an implementation is a set of area
classes over one HTTP client. Every reference the package stores is an id
the port hands out. Above the port only `domain/roles.py` reads one, turning
a contest's or a task's id into the scope its roles are held at and back;
every other id is opaque. Every failure is one of five
typed errors: `NotFound`, `Forbidden`, `Conflict`, `Rejected` and
`Unavailable`. Retries with backoff live inside the implementation, so a forge
that is busy reaches the services only as `Unavailable` once the retries are
used up. A retry never makes something twice: a request that sets a state is
retried on a busy server or a lost answer, and a request that creates
something, a POST, only when it never reached the server; otherwise it is
`Unavailable` at once, and the step that sent it finds on its next run
whether the thing was made, since every step checks before it creates.

Operations done for a person take the identity the call is made under, so the
host records the change as theirs and enforces their permissions underneath
the platform's own: a submission is committed by the contestant, a person's
roles are read with their own credential, and the platform's token never makes
a change in anyone's name. The host's permission for a role is `write` for
admin and manager alike and `read` for observer, never a repository admin,
since a repository admin may delete a tag's protection; the package holds the
difference between admin and manager. Provisioning, which includes creating
every repository since the host lets no one else create one, and protected
versions are done as the platform account, the account
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
first and says how many it had to put back, so the nightly pass runs it over
everything. `content.save_files` writes several files as one change as the
person saving, each carrying the token it was read with, and refuses the
whole change as `Conflict` when any has moved; `content.list_files` gives
every file of a place at a version with its token, the same for the same
content, which is how a save compares data files without reading them.
`primitives.read_declaration` reads a primitive's declaration at a version
as the organiser whose save compiles it.
`workspaces.publish` names a version a save already wrote as the next
publication, with a note, and `workspaces.list_publications` reads each back
as a `Publication` with what its note says. A contestant's workspace is made
one part at a time, each of which can be run again and keeps what is
already right: `workspaces.open_workspace` makes the desk and gives the
members write access to it, `workspaces.open_submission_place` makes the
place to submit one task, reserves its submissions for the platform and only
then gives them write access there, and `workspaces.close_workspace` takes
the access away and keeps everything in it. `workspaces.workspace_of` names
a workspace without a call, for closing one whose name never reached the
contestant's row. `identity.verified_emails` reads the addresses the host
has confirmed are a person's.

The org account is made by the platform too, through five more operations
the port declares: `identity.create_user` and `mint_token` make the account
at the host and its credential there, `orgs.ensure_account_membership` puts
it in its place in the org, and `grading.create_ci_user` and
`mint_ci_token` make its user at the CI and sign it in there. The CI admits
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

Both implementations name repositories and build and read ids with the one
grammar in `forges/ids.py`, so an id from elsewhere is `NotFound` whichever
is behind the port.

`UNICON_FORGE=forgejo` runs against Forgejo and Woodpecker; `UNICON_FORGE=fake`
runs the whole stack against the in-memory forge, which records every call
with its identity and refuses what a real forge refuses. `CachedForge` wraps
either and caches the reads that repeat within a request: user lookups
always, org reads when `UNICON_FORGE_CACHE` is on.

## The setup

`forge/runtime/setup.py` sets the package up for one process: the forge
behind the port, which `forges.build` picks and configures from the settings,
the pool of database connections, the clock and the background loops. The
process that hosts the package calls `forge.api.start(callback_path=...)`
once at start. It reads the `UNICON_*` settings, builds the one setup the
process holds, and starts the loops. `callback_path` is the host's own
sign-in callback route, the one thing the package cannot know on its own;
the package joins it to `UNICON_PUBLIC_URL`. A one-off command passes
`background=False`, and no poller or timed pass runs in it.
`forge.api.public_url()` gives that URL back, for a host that checks where a
request came from. `await forge.api.ready()` raises `NotReady` unless the
database answers within two seconds, on a connection outside the pool; the
cause goes to the log as `setup.not_ready` and not into the error.
`await forge.api.stop()` stops the loops and closes every connection. An
action called before `start` raises an error naming `forge.api.start`.

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
variables and required only when `UNICON_FORGE=forgejo`.
`UNICON_HARNESS_IMAGE` is the harness every plan names, by digest, the one of
the runner release the package pins unless given. A missing or malformed
variable stops the process at start with the variable named.

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
| `access` | `organiser` |
| `account` | `create`, `deactivate`, `delete` |
| `sign_in` | `complete` |
| `orgs` | `create`, `create_by_operator`, `status`, `update` |
| `contests` | `create`, `list`, `status` |
| `tasks` | `create`, `list`, `state`, `status` |
| `files` | `read`, `tree`, `history`, `write`, `rollback` |
| `publications` | `save`, `list` |
| `release` | `of_task` |
| `contestants` | `register`, `mine`, `list`, `approve`, `reject`, `remove`, `extend` |
| `contest_home` | `contests`, `home`, `task` |
| `landing` | `contests`, `contest`, `statement` |
| `events` | `check` |
| `roles` | `holders`, `grant`, `revoke` |

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
commits `ctx.db`. Session bookkeeping runs in short transactions of its
own, opened with `ctx.own_transaction()`, which commits when its block ends
and rolls back when it raises, so a refused request still records what it
learned whatever the action does next. The setup holds one lock per session
for refreshing its credential, taken as `ctx.refresh_lock(session_id)`, so
two requests in one process refresh it once.

Making something at the forge is several calls that can fail halfway, so it
is a job. `orgs.create` writes a `pending` row in `provisioning` carrying
the request and answers at once with the `Record`; the `provisioning`
poller takes the row and `provisioning.run` walks the steps of making the
thing, records each as it completes, and leaves a failure on the row naming
the step in `failed_step` and the reason in `error`, in the platform's own
words, while what the forge said goes to the log. The row stays waiting, so a later tick tries again
from the step after the last one that completed: at once after the first
failure, then after a wait in `retry_at` that doubles from two seconds up to
an hour, so a failure that will not heal by itself costs the forge a call
an hour; work that fails before it reaches a step waits the same way. The
progress is written on the poller's unit of work, which holds the row, and
lands when the tick commits. Every step is written to be run twice. There is
one row per thing, so two requests for it at once leave one row and the
second is `Conflict`. `orgs.status` reads the row back for the person who
asked for the org. The steps of each kind are named once, in order, in
`STEPS` in `forge/domain/provisioning.py`, and each service builds its list
from there with `provisioning.steps`. The `Record` a status answers carries
everything a follower needs: `steps`, its kind's steps in order;
`last_step`, the last one completed; and on a `failed` row `failed_step`,
the step it stopped at, which is none when the work failed outside any
step, `error`, the reason, and `retry_at`, when the row is next tried. An
org takes ten steps: its account row, the org, its roles, its labels, its
signed event push, its first admin, its service account at the forge, that
account's place in the org and its forge credential, its user at the CI,
and its sign-in at the CI. The service account's password is
made in the step that creates it and held for that attempt only; a rerun
that starts after the account was made sets a fresh one first, which is why
the password is never in the database. A service account name someone
already took at the forge is refused rather than adopted, since anyone may
sign up there, and so is the org itself: an org of that name already at the
forge is taken as made by an earlier try only when the platform account owns
it (`orgs.platform_owns`). An org's name is at most 30 characters, so that its service
account's, `unicon-ci-<org>`, keeps within the 40 every name has. People
and orgs share one namespace at the forge, so both creates refuse, before
anything is written, a name a person or an org already has there
(`orgs.name_taken` through the port). With
`UNICON_ORG_CREATION_OPEN` off, `orgs.create` refuses and the operator runs
`orgs.create_by_operator`, the same steps inline, naming the first admin.

A contest and a task are made the same way. `contests.create` needs the
manager role at the org and `tasks.create` the manager role at the contest,
which must be there; each checks the name, writes the request and answers
at once, and `status` reads the row for anyone observing the scope above. A
contest or task asked for with no title, or a blank one, is titled by its
name. A contest takes two steps, its place with a starter `contest.yaml` that is
valid as written and its roles and protection; a task takes the same two,
with `task.yaml`, `statement.md` and a placeholder in each of
`data/testcases/` and `checker/`, its publications reserved in the second.
A contest or task the first try made before its record caught up is taken as
made. A contest's tasks are the tasks there are at the forge, which
`tasks.list` reads as the organiser; the `tasks` list in `contest.yaml`
orders, labels and scores them, and creating a task does not touch it.

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
`setup=`: `sign_in.start(next)`, `sign_in.sign_up_url()`, the cookie
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
   of that state. A state that fails is a `Draft`: the organiser's
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
   note saying whether it changed how the task grades and what. A save that
   changes nothing since the latest publication publishes nothing new. When
   another save landed between the check and the write, the change holds
   files this save never checked, so it comes back as a `Draft` saying so,
   and the next save checks and publishes the task as it then stands.
6. The task is activated at the CI as the org's account, once: taken for
   grading, trusted for `volumes` and nothing else, and left with no
   webhook. Its `activation` row in `provisioning` is the record, so the
   first publication activates it, and a later one does only when that
   record never landed. An activation that fails does not undo the
   publication: the row is left waiting for the `provisioning` poller, and
   the result says it is pending until the poller has made it.
7. Every approved contestant of the contest with no place to submit the task
   yet is given one, by the poller (below).

A valid save comes back as `Published`, with the publication, its number,
whether it changed how the task grades and what, and the activation:
`done`, `pending` or `not_needed`. `publications.list` gives every
publication with its flag and its changes, for the task's history.

## The compiler

`forge/domain/plans.py` compiles each stage into the plan the harness runs,
the runner's `plan.schema.json` version 3, flat and fully resolved, so
nothing is read at grade time: the harness image, the stage, the test list,
the steps in order and the verdict block. Each `use:` is the primitive whose
declaration the save read; a `use:` that is someone else's private workflow,
or not there, is an error naming it, and a workflow used as a step waits for
feature 10. Each step carries its primitive's image by digest, entrypoint and
limits, and each `with` value becomes one of the plan's values: a literal, a
file or list of files in the task, a contestant input or the language chosen
for it, or an earlier step's output. A `foreach` over a setter's `file[]`
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
advisory lock on the contest, so the last place goes once. The row is
written pending with what let it through; with `approval: auto` it is
approved in the same call. The rules themselves are in
`forge/domain/registration.py`.

An organiser managing the contest decides: `approve` a pending registration,
`reject` a pending one with a reason the person reads (`invalid_reason`
without one), `remove` an approved one, and `extend` a pending or approved
one, which gives that person more time past the contest's end
(`invalid_extension` below nothing or past a year). A decision from any
other status is `wrong_status`, naming the status. `list` gives every
registration of the contest to anyone observing it, oldest first, with
where each approved contestant's workspace stands; `mine` gives a person
their own.

Approval asks for the contestant's workspace, which the `provisioning`
poller makes in parts, each recorded as a row of its own and tried again
alone when it fails: the `workspace` row opens the desk, names the
workspace on the contestant's row, and asks for a `submission_place` row
for every task published by then, released or not; each `submission_place`
row makes that one place, once the desk is open, and waits for it
otherwise. A save that publishes a task, and the nightly pass, ask for a
place for every approved contestant with none yet, so a task published
later reaches them. The workspace is `ready` once its desk and every place asked for it
are made, and `preparing` until then, with the reason while a part is
failing. Removing a contestant takes their access to every part away and
keeps what is in it; a part made for someone no longer approved does
nothing. The poller takes its rows in the order of what they make, so every
tick locks contestants in the same order.

What a signed-in person reads of a contest is `contest_home`: `contests`,
every contest they see with their own status; `home`, a contest's dates,
their registration, what the register form needs, their own deadline, which
is the end plus their extension, the server's clock and the tasks released
to them in the contest's order; and `task`, a visible task's statement and
limits and nothing else of what it holds. A visitor with no session reads
`landing`: the public contests, one with its released tasks, and a released
task's statement. The list of public contests is kept by each process for
five seconds (`ctx.memo`, `forge/runtime/memo.py`), since anyone may ask for
it and it reads every org's contests; visitors arriving while it is read wait
for that one read. All of it is read live as the platform from the latest
publication of each task, with the contest's visibility and the release
rules applied first, and a contest or task the reader may not see is no
such contest or task, the same answer as one that is not there.

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
| `WrongStatus` | `wrong_status` | `current`, the registration's status |

The rest, `invalid_name`, `unauthenticated`, `session_expired`,
`fresh_sign_in_required`, `sign_in_invalid`, `sign_in_denied`,
`not_ready`, the registration refusals `registration_closed`, `is_staff`,
`already_registered`, `invite_required`, `wrong_invite_code`,
`domain_not_allowed` and `contest_full`, which share the base class
`RegistrationRefused`, and `invalid_reason` and `invalid_extension`, carry
nothing beyond the detail.

## The tables

Ten tables, keyed by UUID v7, with every enumeration as `text` under a
`CHECK`: `sessions`, `contestants`, `teams`, `team_members`, `invites`,
`provisioning`, `org_accounts`, `gradings`, `uploads` and `jupyter_sessions`.
They hold what a forge cannot: nothing about users, orgs, contests or tasks,
which are read live. `org_accounts` is one row per org, its service account's
forge credential, CI credential and event secret each as AES-256-GCM
ciphertext under `UNICON_TOKEN_ENCRYPTION_KEY`, the way a session's
credential is, so a copy of the table hands out no access;
`services/credentials.py` is the one place either is sealed or opened.
`contestants` names each contestant's workspace in `workspace_id` once it is
opened, so it keeps the name it was opened under.
`unicon-forge migrate` reads `UNICON_DATABASE_URL`, applies the migrations
under `forge/db/alembic/` and exits. A deployment runs it before the host
starts, from the host's image, which has the package and its command
installed; the host has no migrate command of its own.

There is no jobs table. Row work is a `Poller` over a table that carries a
status, each row taken under `FOR UPDATE SKIP LOCKED`; timed work is a
`TimedPass` under a Postgres advisory lock, so it runs once however many
processes are up. Both are in `forge/runtime/background.py`. Each tick is
one unit of work, and its work runs with the `Context` over it, the forge
included. The setup runs three loops: the `provisioning` poller every two
seconds over the `pending` and `failed` rows whose `retry_at` has come,
handing each by its `kind` to the service that makes it, as `MAKERS` in
`forge/runtime/setup.py` lists: `org`, `contest` and `task` to the service
of that name, `workspace` and `submission_place` to `workspaces`, and
`activation` to `activations`;
`sessions.sweep` hourly; and `drift.nightly` daily, which puts back any org
account missing from its place in its org, makes one call to the CI as each
account, since the CI refreshes the account's forge credential only when
that account calls it, signing in again any account the CI no longer
answers, makes every org's roles again, giving back any team whose
permission changed at the forge, and secures every contest and task of every
org the platform made again, putting back any role, protection or
reservation that went missing, and asks for any place to submit a published
task that an approved contestant still lacks.

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
client's own loggers, `httpx` and `httpcore`, log from a warning up whatever
the level, since the line they write for every request holds its whole URL,
and a URL of the CI's sign-in holds a one-time code.

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
`OrgName`, `AsUser` and `Visibility`, `Settings` for an override, and `Setup`
to type the setup a fixture hands over. `logged(caplog, event)` gives back the
records a test caused as the JSON objects they are written as. `tick(setup,
name)` runs one tick of the poller or timed pass of that name, such as
`provisioning` or `drift.nightly`, as the setup would, and
`register_contestant(setup, contest, user_id)` writes the row that makes
someone a contestant, with a status and an extension, and asks for no
workspace. `seed_classic(fake)`
puts the built-in workflow `unicon/classic@v1` at the fake from `CLASSIC`, a
copy of the file deploy's bootstrap seeds, and with `seed_primitives` the
three primitives it uses from `PRIMITIVES`, each primitive repo's own
`primitive.yaml` with an image of `PLACEHOLDER_DIGEST`, so a task's first
save finds a workflow and every step's image; the package's tests check the
copy against deploy's file when that repo is checked out beside this one.
The fake refuses a user id it already has, since the accounts the package makes take the next free ids.

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
are skipped without all four. `tests/live/test_organiser_path.py` walks the
whole path over a real setup, the test Postgres included, and the check that
history cannot be rewritten pushes with `git` to a repository it made.

## Releasing

Push a tag `v1.2.3` on `main`. The release workflow refuses a tag whose commit
is not on `main`, checks that the tag, the version in `pyproject.toml` and
`forge.__version__` are the same number, runs the CI workflow itself on the
tagged commit, Postgres included, builds the wheel and the sdist, and
attaches both to a GitHub release. A dependant pins that release.

## Licence

MIT. See `LICENSE`.
