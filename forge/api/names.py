"""The names people give orgs, contests and tasks, and the ids they are
filed under. `scope_at` turns the names in an address into the scope a route
acts on, `NotFound` naming the first part that is not there. The records
the other modules hand back carry the names a page shows, so a host never
reads a name out of an id.
"""

from forge.domain.names import Named, ScopeNames
from forge.services.names import scope_at

__all__ = ["Named", "ScopeNames", "scope_at"]
