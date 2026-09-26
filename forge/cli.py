"""`unicon-forge migrate`: bring a database up to the package's latest
migration.
"""

import argparse
import sys

from forge.db.migrations import upgrade_to_head
from forge.settings import load_database_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unicon-forge", description="The Unicon contest API")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="bring the database up to the latest migration")
    parser.parse_args(argv)
    upgrade_to_head(str(load_database_settings().database_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
