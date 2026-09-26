# forge

The Unicon contest API as a Python package, `unicon-forge`. It knows what a
contest is and nothing about HTTP: the domain types, the services, the
database tables and their migrations, the forge port, which is the interface
this package calls a git host through, and the implementations behind that
port. The backend is an HTTP shell over it and pins one release of it.

## Layout

```
forge/
  settings.py      every UNICON_* setting, read once at start
  runtime.py       the package assembled for one process: forge, database, loops
  port.py          the interface a git host is called through, a Protocol
  crypto.py        encryption of credentials at rest
  log.py           the one structured logger; every line is a JSON record
  cli.py           unicon-forge migrate
  domain/          the types and their rules; imports nothing else in the package
  services/        the actions; the only layer that writes to the database
  db/              the tables, the engine and the migrations
  forges/forgejo/  the port over Forgejo and Woodpecker
  forges/fake/     the port in memory, for tests
  forges/cached.py the port wrapped in a small per-read cache
  forges/README.md what any git host must provide to sit behind the port
tests/
```

## The port

`forge/port.py` declares every operation the platform needs from a git host,
in the platform's words: identity, orgs and roles, contests and tasks,
workspaces, submissions, publications, threads, workflows, primitives, grading
runs and computes. Every reference the package stores is an opaque id the port
hands out; nothing reads inside one. Every failure is one of five typed errors:
`NotFound`, `Forbidden`, `Conflict`, `Rejected` and `Unavailable`. Retries with
backoff live inside the implementation, so a forge that is busy reaches the
services only as `Unavailable` once the retries are used up.

Operations done for a person take the identity the call is made under, so the
host records the change as theirs and enforces their permissions underneath
the platform's own. Provisioning and protected versions are done as the
platform account.

`UNICON_FORGE=forgejo` runs against Forgejo and Woodpecker; `UNICON_FORGE=fake`
runs the whole stack against the in-memory forge, which records every call
with its identity and refuses what a real forge refuses.

## The tables

Nine tables, keyed by UUID v7, with every enumeration as `text` under a
`CHECK`: `sessions`, `contestants`, `teams`, `team_members`, `invites`,
`provisioning`, `gradings`, `uploads` and `jupyter_sessions`. They hold what a
forge cannot: nothing about users, orgs, contests or tasks, which are read live.
`unicon-forge migrate` applies the migrations under `forge/db/alembic/`.

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
- `domain` imports nothing else in the package.

## Logging

Every module logs through `forge.log.get_logger` with an event name and named
fields, never a formatted sentence, and nothing prints. Each record is one
JSON object: `time`, `level`, `logger`, `event`, then the fields. A
`SecretStr` given as a field is written as `**********`. The process that
hosts the package calls `forge.log.configure(level)` once at start.

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
tests are skipped.

## Releasing

Push a tag `v1.2.3` on `main`. The release workflow refuses a tag whose commit
is not on `main`, checks that the tag, the version in `pyproject.toml` and
`forge.__version__` are the same number, runs the same checks as CI, builds
the wheel and the sdist, and attaches both to a GitHub release. A dependant
pins that release.

## Licence

MIT. See `LICENSE`.
