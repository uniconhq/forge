"""The Unicon contest API as a Python package. It knows what a contest is and
nothing about HTTP: the domain types, the services, the database tables and
their migrations, the forge port, which is the interface it calls a git host
through, and the implementations behind that port.

Layers, each a package here, and what each may import:

    domain      the types and the clock; imports nothing else in this package
    services    the actions and their building blocks; composes domain, db and the port
    db          the tables and migrations
    port        the interface a git host is called through
    forges/     the implementations of the port: forgejo/, fake/ and cached
    runtime     how a call runs: the context, @action, the setup and its loops

`domain`, `services` and `db` never import `forges`, and only `runtime` and
`forge.testing` build an implementation; `forges.forgejo` never imports
`services` or `db`, and `forges.fake` never imports `services`, `db` or
`runtime`. import-linter contracts in `pyproject.toml` fail the build on a
leak.

A hosting process imports `forge.api` and nothing else of the package; that
folder lists everything it may call. `unicon-forge migrate` migrates the
database.
"""

__version__ = "0.17.0"
