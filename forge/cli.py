"""`unicon-forge`, the package's own commands, which a deployment runs from
the host's image:

- `migrate` brings the database `UNICON_DATABASE_URL` names up to the
  package's latest migration, then exits. Migrating is the package's job and
  not the hosting process's, so the command is the package's own; a
  deployment runs it once, before the host starts.
- `reconcile` gives every submission at the forge that has no grading its
  gradings, over the running contests, or with `--all` over every contest of
  every org the platform made, which is what a restore runs once the
  database is back. It reads every `UNICON_*` setting, as the host does,
  runs no background loop, and writes what it did as the log record
  `reconcile.done`.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable

from forge import log
from forge.db.migrations import upgrade_to_head
from forge.runtime.setup import Setup
from forge.services import reconcile
from forge.settings import load_database_settings, load_settings

CALLBACK_PATH = "/"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unicon-forge", description="The Unicon contest API")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="bring the database up to the latest migration")
    reconciling = commands.add_parser(
        "reconcile", help="give every submission at the forge without gradings its gradings"
    )
    reconciling.add_argument(
        "--all", action="store_true", help="every contest, not only the running ones"
    )
    arguments = parser.parse_args(argv)
    if arguments.command == "reconcile":
        log.setup()
        asyncio.run(_reconcile(every_contest=arguments.all), loop_factory=_loop_factory())
        return 0
    upgrade_to_head(str(load_database_settings().database_url))
    return 0


def _loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """The event loop psycopg runs on: Windows' default loop is one it
    cannot use asynchronously.
    """
    return asyncio.SelectorEventLoop if sys.platform == "win32" else None


async def _reconcile(*, every_contest: bool) -> reconcile.Reconciled:
    setup = Setup.build(load_settings(), callback_path=CALLBACK_PATH)
    try:
        async with setup.unit_of_work() as ctx:
            return await reconcile.reconcile(ctx, every_contest=every_contest)
    finally:
        await setup.stop()


if __name__ == "__main__":
    sys.exit(main())
