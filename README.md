# forge

The Unicon contest API as a Python package, `unicon-forge`. It knows what a
contest is and nothing about HTTP: the domain types, the services, the
database tables and their migrations, the forge port, which is the interface
this package calls a git host through, and the implementations behind that
port. The backend is an HTTP shell over it, and pins one release of it.

Today the package is its layout, its rules and its logger. The tables, the
port's operations and the two implementations land feature by feature.

## Layout

```
forge/
  domain/          the types; imports nothing else in the package
  services/        the actions; the only layer that writes to the database
  db/              the tables and their migrations
  port.py          the interface a git host is called through, a Protocol
  forges/forgejo/  the port over Forgejo and Woodpecker
  forges/fake/     the port in memory, for tests
  log.py           the one structured logger; every line is a JSON record
tests/
```

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
hosts the package calls `forge.log.configure(level)` once at start; the
backend logs in the same shape, so the two can be read together.

## Checks

What CI runs, in the same order:

```sh
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run lint-imports
uv run mypy
uv run pytest
uv build
```

## Releasing

Push a tag `v1.2.3` on `main`. The release workflow refuses a tag whose commit
is not on `main`, checks that the tag, the version in `pyproject.toml` and
`forge.__version__` are the same number, runs the same checks as CI, builds
the wheel and the sdist, and attaches both to a GitHub release. A dependant
pins that release.

## Licence

MIT. See `LICENSE`.
