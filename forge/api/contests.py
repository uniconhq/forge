"""Making a contest and listing an org's contests, for an organiser the
host's guard produced, and the id a contest in an org is known by.
"""

from forge.domain.roles import contest_id_of
from forge.services.contests import create, list

__all__ = ["contest_id_of", "create", "list"]
