# forge

The Unicon contest API as a Python package, `unicon-forge`. It knows what a
contest is and nothing about HTTP: the domain types, the services, the
database tables and their migrations, the forge port, which is the interface
this package calls a git host through, the implementations behind that port,
and the pytest plugin a dependant tests with. The backend is an HTTP shell
over it and pins one release of it.

## Layout

```
forge/
  settings.py      every UNICON_* setting, read once at start
  setup.py         the package set up for one process; start, ready, stop and now
  actions.py       the @action mark, and the one setup the process holds
  context.py       what a building block runs with, and the clock
  testing.py       the pytest plugin: a migrated database, the fake, a setup
  crypto.py        encryption of credentials at rest
  log.py           the one structured logger; every line is a JSON record
  domain/          the types and their rules; imports nothing else in the package
  port/            the interface a git host is called through, one area per module
  services/        the actions and their building blocks; the only layer that
                   writes to the database
  db/              the tables, the engine and the migrations
  forges/forgejo/  the port over Forgejo and Woodpecker
  forges/fake/     the port in memory, one module per area over one state
  forges/cached.py the port wrapped in a small per-read cache
  forges/README.md what any git host must provide to sit behind the port
tests/
  forges/          each implementation against the port's contract
  services/        the services over a real Postgres and the fake
  db/              the migrations, up and down
  live/            the Forgejo implementation against a running Forgejo
```

## The port

`forge/port/` declares every operation the platform needs from a git host,
in the platform's words, as one `Protocol` per area: `identity`, `orgs`,
`content`, `workspaces`, `threads`, `workflows`, `primitives`, `grading` and
`computes`. `Forge` composes the areas, so a service reaches a host as
`ctx.forge.orgs.grant_role(...)` and an implementation is a set of area
classes over one HTTP client. Every reference the package stores is an opaque
id the port hands out; nothing reads inside one. Every failure is one of five
typed errors: `NotFound`, `Forbidden`, `Conflict`, `Rejected` and
`Unavailable`. Retries with backoff live inside the implementation, so a forge
that is busy reaches the services only as `Unavailable` once the retries are
used up.

Operations done for a person take the identity the call is made under, so the
host records the change as theirs and enforces their permissions underneath
the platform's own: a submission is committed by the contestant, a person's
roles are read with their own credential, and the platform's token never
stands in for anyone. Provisioning, which includes creating every repository
since the host lets no one else create one, and protected versions are done
as the platform account.

`UNICON_FORGE=forgejo` runs against Forgejo and Woodpecker; `UNICON_FORGE=fake`
runs the whole stack against the in-memory forge, which records every call
with its identity and refuses what a real forge refuses. `CachedForge` wraps
either and caches the reads that repeat within a request: user lookups
always, org reads when `UNICON_FORGE_CACHE` is on.

## The setup

`forge/setup.py` sets the package up for one process: the forge behind the
port, the pool of database connections, the clock and the background loops.
The process that hosts the package calls
`forge.start(sign_in_redirect_uri=...)` once at start. It reads the
`UNICON_*` settings, builds the one setup the process holds, and starts the
loops. `sign_in_redirect_uri` is the host's sign-in callback, the one thing
the package cannot know on its own. `await forge.ready()` raises unless the
database answers within two seconds, on a connection outside the pool, and
`await forge.stop()` stops the loops and closes every connection. An action
called before `start` raises an error naming `forge.start`.

## Actions and building blocks

A service is a module of functions under `forge/services/`. The functions a
hosting process calls are actions, marked `@action` from `forge/actions.py`:

| Module | Actions |
|---|---|
| `sessions` | `revoke`, `revoke_all`, `list_for` |
| `identity` | `whoami`, `current` |
| `account` | `deactivate`, `delete` |
| `sign_in` | `complete` |
| `orgs` | `provision` |

An action is one unit of work. Called as `account.delete(session)`, it opens
a transaction on the setup the process holds, runs, commits when it
returns, rolls back when it raises, and closes the transaction either way. A
commit that fails raises out of the call, so the caller never answers a
success for a change that was not saved. Handed a setup first, as
`account.delete(setup, session)`, it opens the transaction on that setup,
which is how a test runs one against a setup of its own. Handed a `Context`
first, as another service calls it, it runs inside the caller's transaction,
so `account.delete` calling `sessions.revoke_all` stays one transaction.
When two writes must succeed or fail together, they are one action with a
name: `sign_in.complete` creates the new session and ends the one the
browser had before, together.

Every other service function is a building block, called only from inside
the package. Its first parameter is a `Context`: the transaction of the
unit of work, a factory for work that needs a transaction of its own, the
forge, the settings and the clock. A building block never commits. Two
things run in short transactions of their own so they land whatever the
action does next: session bookkeeping, so a refused request still records
what it learned, and the `provisioning` record. Making something at the
forge is several calls that can fail halfway, so `provisioning.run` walks
the steps of making one thing, writes the last completed step to its row as
soon as it completes, leaves a failure on the row naming the step and the
error, and on a rerun starts at the step after the last one that completed.
`orgs.provision` is the first user of it.

Three functions need no transaction and are plain functions with an optional
`setup=`: `sign_in.start(next)`, `sign_in.sign_up_url()` and `forge.now()`,
the clock the package enforces deadlines with.

## The tables

Nine tables, keyed by UUID v7, with every enumeration as `text` under a
`CHECK`: `sessions`, `contestants`, `teams`, `team_members`, `invites`,
`provisioning`, `gradings`, `uploads` and `jupyter_sessions`. They hold what a
forge cannot: nothing about users, orgs, contests or tasks, which are read live.
`forge.setup.migrate` applies the migrations under `forge/db/alembic/`; the
backend runs it as `unicon migrate`.

There is no jobs table. Row work is a `Poller` over a table that carries a
status, each row taken under `FOR UPDATE SKIP LOCKED`; timed work is a
`TimedPass` under a Postgres advisory lock, so it runs once however many
processes are up. Both are in `forge/services/background.py`.

## Layer rules

Three import-linter contracts in `pyproject.toml`, run by `lint-imports` in
CI, so a cross-layer import fails the build:

- `domain`, `services` and `db` never import anything under `forges`.
- `forges.forgejo` never imports `services` or `db`. It may import `port` and
  `domain`.
- `domain` imports nothing else in the package, `port` included.

## Logging

Every module logs through `forge.log.get_logger` with an event name and named
fields, never a formatted sentence, and nothing prints. Each record is one
JSON object: `time`, `level`, `logger`, `event`, then the fields. A
`SecretStr` given as a field is written as `**********`. The process that
hosts the package calls `forge.log.configure(level)` once at start.

## Testing

`forge.testing` is a pytest plugin, loaded with `-p forge.testing` or from a
`conftest.py`. It gives a test a migrated Postgres of its own, the in-memory
forge with two users, a clock the test can move, a `setup` over them, and a
`ctx`, one unit of work committed when the test ends. A test calls a
building block with `ctx`, and an action either with `ctx`, to run it inside
that unit of work, or with `setup`, to have it commit one of its own.
`held_setup` makes `setup` the one forge holds for the test, for code that
calls actions with neither, and lets it go when the test ends, passed or
failed. A dependant loads the same plugin, so its tests run the package the
way the backend does, with no fixtures of its own to keep in step.

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
with an administrator token in `UNICON_LIVE_FORGE_ADMIN_TOKEN`; they are
skipped without both, and `-m "not live"` leaves them out.

## Releasing

Push a tag `v1.2.3` on `main`. The release workflow refuses a tag whose commit
is not on `main`, checks that the tag, the version in `pyproject.toml` and
`forge.__version__` are the same number, runs the CI workflow itself on the
tagged commit, Postgres included, builds the wheel and the sdist, and
attaches both to a GitHub release. A dependant pins that release.

## Licence

MIT. See `LICENSE`.
