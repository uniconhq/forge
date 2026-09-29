"""The steps of making each kind of thing at the forge, in the order they
run. The service that makes a kind builds its steps from these names, and
every provisioning record carries its kind's list, so whoever follows a
record reads the steps from the record itself.

An org takes ten: its account row, the org, its roles, the labels its
threads are marked with, its signed event push, its first admin, its service
account at the forge, that account's forge credential, its user at the CI,
and its sign-in at the CI. A contest and a task each take two, the place
with its starter files and its roles and protection. A task's registration
for grading takes one.
"""

from collections.abc import Mapping
from types import MappingProxyType

STEPS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "org": (
            "account_row",
            "org",
            "roles",
            "labels",
            "event_push",
            "first_admin",
            "service_account",
            "service_token",
            "ci_user",
            "ci_login",
        ),
        "contest": ("repo", "roles"),
        "task": ("repo", "roles"),
        "registration": ("register",),
    }
)


def steps_of(kind: str) -> tuple[str, ...]:
    """The steps of making a thing of this kind, in order, or none for a kind
    no service makes in steps.
    """
    return STEPS.get(kind, ())
