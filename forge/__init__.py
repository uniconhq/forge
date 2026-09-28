"""The Unicon contest API as a Python package. It knows what a contest is and
nothing about HTTP: the domain types, the services, the database tables and
their migrations, the forge port, which is the interface it calls a git host
through, and the implementations behind that port.

Layers, each a package here, and what each may import:

    domain      the types; imports nothing else in this package
    services    the actions and their building blocks; composes domain, db and the port
    db          the tables and migrations
    port        the interface a git host is called through
    forges/     the implementations of the port: forgejo/, fake/ and cached

`domain`, `services` and `db` never import `forges`; `forges.forgejo` never
imports `services` or `db`. import-linter contracts in `pyproject.toml` fail
the build on a leak.

A hosting process calls `start` once, then actions, then `stop`; `ready`
asks the database and `now` reads the package's clock. `setup` holds these.
"""

from forge.setup import now, ready, start, stop

__all__ = ["now", "ready", "start", "stop"]

__version__ = "0.2.0"
