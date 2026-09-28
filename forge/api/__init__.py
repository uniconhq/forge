"""The front door: everything a hosting process may call, and nothing else.
The backend imports from here and from no other part of the package, and an
import-linter rule on its side fails CI when it does. Each module here only
re-exports names written elsewhere in the package and lists them in
`__all__`, so what the backend can reach is read in this one folder, and a
test on this side fails when a building block is put on the list.

This module starts, asks and stops the one setup the process holds, reads
its clock and says where the platform is served. The actions and the types
are in the modules beside it.
"""

from forge.setup import now, public_url, ready, start, stop

__all__ = ["now", "public_url", "ready", "start", "stop"]
