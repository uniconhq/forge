"""The tables the platform owns: what a forge cannot hold. `metadata` is the
metadata with every one of them on it, which is what the migrations and the
tests compare against; reading it from here is what puts them there.
"""

from forge.db.base import Base
from forge.db.tables.entrants import Entrant, EntrantMember
from forge.db.tables.entries import Entry
from forge.db.tables.gradings import Grading
from forge.db.tables.hold_ends import HoldEnd
from forge.db.tables.invites import Invite
from forge.db.tables.marks import Mark
from forge.db.tables.names import Name
from forge.db.tables.org_accounts import OrgAccount
from forge.db.tables.regrades import Regrade
from forge.db.tables.releases import Release
from forge.db.tables.sessions import Session
from forge.db.tables.steps import GradingReuse, GradingStep, PublicationObjects, StepOutput
from forge.db.tables.uploads import Upload

__all__ = [
    "Entrant",
    "EntrantMember",
    "Entry",
    "Grading",
    "GradingReuse",
    "GradingStep",
    "HoldEnd",
    "Invite",
    "Mark",
    "Name",
    "OrgAccount",
    "PublicationObjects",
    "Regrade",
    "Release",
    "Session",
    "StepOutput",
    "Upload",
    "metadata",
]

metadata = Base.metadata
