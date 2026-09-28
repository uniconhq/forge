"""`unicon-forge migrate`: bring the database `UNICON_DATABASE_URL` names up
to the package's latest migration, then exit. Migrating is the package's job
and not the hosting process's, so the command is the package's own; a
deployment runs it once, before the host starts.
"""

import argparse
import sys

from forge.settings import load_database_settings
from forge.setup import migrate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unicon-forge", description="The Unicon contest API")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="bring the database up to the latest migration")
    parser.parse_args(argv)
    migrate(str(load_database_settings().database_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
