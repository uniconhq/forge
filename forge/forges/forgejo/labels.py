"""The labels threads are marked with at Forgejo, with their colours: one per
kind of thread, named as the kind, and one for an answered clarification. The
org area makes them when an org is provisioned, and the thread area finds
them by these names.
"""

from forge.domain.threads import ThreadKind

ANSWERED = "answered"

LABELS = {
    ThreadKind.ANNOUNCEMENT.value: "1d76db",
    ThreadKind.CLARIFICATION.value: "fbca04",
    ANSWERED: "0e8a16",
}
