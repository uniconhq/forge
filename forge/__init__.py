"""The Unicon contest API as a Python package. It knows what a contest is and
nothing about HTTP: the domain types, the services, the database tables and
their migrations, the forge port, which is the interface it calls the
services outside it through, and the adapters behind that port.

Layers, each a package here, and what each may import:

    domain      the types and the clock; imports nothing else in this package
    services    the actions and their building blocks; composes domain, db and the port
    db          the tables and migrations
    port        the interface the git host, the CI, the store and mail are called through
    adapters/   the adapters behind the port, a group per service: git/, ci/,
                objects/ and mail/, joined by build
    runtime     how a call runs: the context, @action, the setup and its loops

`domain`, `services` and `db` never import `adapters`, and only `runtime` and
`forge.testing` build them; the adapter groups never import each other,
`services` or `db`, and the fakes never import `runtime`. import-linter
contracts in `pyproject.toml` fail the build on a leak.

A hosting process imports `forge.api` and nothing else of the package; that
folder lists everything it may call. `unicon-forge migrate` migrates the
database.
"""

__version__ = "0.21.0"
