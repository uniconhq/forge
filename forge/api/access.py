"""Who may do what: the one action that reads a person's roles, and the
`Organiser` it returns for the other organiser actions to take.
"""

from forge.services.access import Organiser, organiser

__all__ = ["Organiser", "organiser"]
