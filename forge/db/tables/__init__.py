"""The nine tables the platform owns: what a forge cannot hold. Importing this
package registers every table on the metadata, which is what the migrations
and the tests need.
"""

from forge.db.tables.contestants import Contestant
from forge.db.tables.gradings import Grading
from forge.db.tables.invites import Invite
from forge.db.tables.jupyter_sessions import JupyterSession
from forge.db.tables.provisioning import Provisioning
from forge.db.tables.sessions import Session
from forge.db.tables.teams import Team, TeamMember
from forge.db.tables.uploads import Upload

__all__ = [
    "Contestant",
    "Grading",
    "Invite",
    "JupyterSession",
    "Provisioning",
    "Session",
    "Team",
    "TeamMember",
    "Upload",
    "register",
]


def register() -> None:
    """Make sure every table is on the metadata."""
