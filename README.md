# forge

The core of Unicon, as the Python package `unicon-forge`. It holds every
rule, every service, the Postgres tables and their migrations, and the
adapters for Forgejo, Woodpecker, S3, package registries and mail. It knows
nothing about HTTP.

The backend is a thin FastAPI app over this package. It pins one release and
imports only `forge.api`. The other repos are frontend, runner,
primitive-exec and deploy. Gradings run as Woodpecker pipelines.

## Layout

```
forge/
  api/        what a hosting process may call, and nothing else
  runtime/    how a call runs in a process: setup, actions, live updates
  domain/     types and rules; imports nothing else in the package
  port/       the interface to the git host, CI, object store, mail and registries
  services/   actions and their building blocks; the only layer that writes the database
  db/         tables, engine and Alembic migrations
  adapters/   one implementation per service behind the port, plus in-memory fakes
  seeds/      the exec primitive's declaration, used by the test fakes
```

`forge/testing.py` is a pytest plugin that dependants load too. `forge/cli.py`
is the `unicon-forge` command.

## Local setup

You need Python 3.14 and uv.

```sh
uv sync
```

Run the same checks as CI:

```sh
uv run ruff format --check .
uv run ruff check .
uv run lint-imports
uv run mypy
UNICON_TEST_DATABASE_URL=postgresql+psycopg://postgres:test@localhost:5432/postgres uv run pytest -n 4
uv build
```

The service and migration tests need a Postgres server they may create
databases on. Without `UNICON_TEST_DATABASE_URL` they skip.

Some tests compare copies in this package with their originals in the
runner, deploy and primitive-exec repos. Clone those beside this repo to run
them. Locally they skip when a repo is missing. In CI (`CI` set) they fail.

To migrate a database:

```sh
UNICON_DATABASE_URL=postgresql+psycopg://... uv run unicon-forge migrate
```

The live tests in `tests/live/` need a real Forgejo, Woodpecker and Garage.
Deploy's CI runs them against its dev stack:

```sh
uv run pytest -m live -rs tests/live
```

Each test skips unless its variables are set. The main ones are
`UNICON_LIVE_FORGE_URL`, `UNICON_LIVE_FORGE_ADMIN_TOKEN`,
`UNICON_LIVE_FORGE_PUBLIC_URL`, `UNICON_LIVE_CI_URL`,
`UNICON_LIVE_CI_PUBLIC_URL`, `UNICON_LIVE_CI_ADMIN_TOKEN`,
`UNICON_LIVE_S3_ENDPOINT`, `UNICON_LIVE_S3_ACCESS_KEY`,
`UNICON_LIVE_S3_SECRET_KEY`, and `UNICON_LIVE_REGISTRIES` for the registry
tests. See `tests/live/` for the rest.

## Releasing

Bump the version in both `pyproject.toml` and `forge/__init__.py`, then push
a tag `vX.Y.Z` on `main`. The `release` workflow runs all of CI on the tagged
commit, checks the tag is on `main` and matches both versions, then
attaches the wheel and sdist to a GitHub release. There is no image.

## Do not break

- Layer rules live in `pyproject.toml` and `lint-imports` enforces them. For
  example, domain, services and db never import an adapter.
- `forge.api` is the public surface. Anything the backend needs goes there.
- Log through `forge.log.get_logger` with an event name and fields. Never
  print.
- `forge/domain/schemas/`, `forge/domain/classic/` and `forge/seeds/` are
  copies from runner, deploy and exec. Change them with their originals.
- What each adapter must provide is in
  [forge/adapters/README.md](forge/adapters/README.md).

Licence: MIT, see `LICENSE`.
