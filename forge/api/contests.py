"""Making a contest, listing an org's contests and following how far making
one has got, for an organiser the host's guard produced, and the id a
contest in an org is known by.
"""

from forge.domain.roles import contest_id_of
from forge.services.contests import create, list, status
from forge.services.provisioning import Record

__all__ = ["Record", "contest_id_of", "create", "list", "status"]
