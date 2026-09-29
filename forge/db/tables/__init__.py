"""The ten tables the platform owns: what a forge cannot hold. `metadata`
is the metadata with every one of them on it, which is what the migrations
and the tests compare against; reading it from here is what puts them there.
"""

from forge.db.base import Base
from forge.db.tables.contestants import Contestant
from forge.db.tables.gradings import Grading
from forge.db.tables.invites import Invite
from forge.db.tables.jupyter_sessions import JupyterSession
from forge.db.tables.org_accounts import OrgAccount
from forge.db.tables.provisioning import Provisioning
from forge.db.tables.sessions import Session
from forge.db.tables.teams import Team, TeamMember
from forge.db.tables.uploads import Upload

__all__ = [
    "Contestant",
    "Grading",
    "Invite",
    "JupyterSession",
    "OrgAccount",
    "Provisioning",
    "Session",
    "Team",
    "TeamMember",
    "Upload",
    "metadata",
]

metadata = Base.metadata
