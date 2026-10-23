"""What a contest's leaderboards ask of `contest.yaml` itself, checked at the
contest's save (SIMPLER-CORE.md 10.1): each board against the catalogue
(`forge.domain.blocks.board_check`), and a task's `marks` given only while
a board reads that task's `marked`, since a mark changes nothing else. A
row may mark the entry's `marks` submissions of a task, or one when a
board reads its `marked` and the entry gives none (`marks_of`).

What a board asks of a task's own file, its values and their kinds, is
checked across files (stage 5, phase 5.7).
"""

from forge.domain.blocks.board_check import BoardsCheck, check_boards
from forge.domain.blocks.check import Found
from forge.domain.definitions import ContestDefinition, ContestTask

MARKED = "marked"


def boards_of(settings: ContestDefinition) -> BoardsCheck:
    """The contest's boards checked, as they are read."""
    return check_boards(settings.leaderboards, [task.id for task in settings.tasks])


def check_contest(settings: ContestDefinition) -> tuple[Found, BoardsCheck]:
    """Every problem with the contest's boards, each at its path, and a
    task's `marks` that no board reads; and the boards as checked."""
    checked = boards_of(settings)
    found = list(checked.problems)
    for index, entry in enumerate(settings.tasks):
        if entry.marks is not None and not checked.reads(entry.id, MARKED):
            found.append(
                (
                    ("tasks", index, "marks"),
                    "This applies only when a leaderboard reads which of this task's "
                    f"submissions an entrant marks, as {{value: [{entry.id}, marked]}}.",
                )
            )
    return found, checked


def marks_of(settings: ContestDefinition, entry: ContestTask) -> int | None:
    """How many submissions a row may mark: the entry's, or 1 when a board
    reads the task's `marked`, or none."""
    if entry.marks is not None:
        return entry.marks
    return 1 if boards_of(settings).reads(entry.id, MARKED) else None


__all__ = ["MARKED", "boards_of", "check_contest", "marks_of"]
