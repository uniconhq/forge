"""Running the package's Alembic migrations from code, so `unicon migrate`, CI
and the tests move a database to the latest revision without an `alembic.ini`.
"""

from alembic import command
from alembic.config import Config

SCRIPT_LOCATION = "forge:db/alembic"


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_to_head(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")


def downgrade_to_base(database_url: str) -> None:
    command.downgrade(alembic_config(database_url), "base")
