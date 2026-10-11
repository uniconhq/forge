"""Making a contest and listing an org's contests, each with its state and
dates, for an organiser the host's guard produced, and the id a contest in
an org is known by.
"""

from forge.domain.roles import contest_id_of
from forge.services.contests import Listed, create, list

__all__ = ["Listed", "contest_id_of", "create", "list"]
