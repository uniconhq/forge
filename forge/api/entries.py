"""Entering a mark for a submission's `entered` value, for the task's
managers and admins, and the grading queue, for its observers: the
submissions each leaderboard's cells counted, row by row once the task has
closed for it, with their marks; and the types they come back as.
"""

from forge.services.entries import Entered, Queued, QueueRow, enter, queue

__all__ = ["Entered", "QueueRow", "Queued", "enter", "queue"]
