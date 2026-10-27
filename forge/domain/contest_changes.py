"""What a contest save changes that can move results or standings (#459)."""

from datetime import datetime

from forge.domain.definitions import ContestDefinition, Hold
from forge.domain.release import in_words


def _time(value: datetime | None) -> str:
    return in_words(value) if value is not None else "no due time"


def _hold(value: Hold | None) -> str:
    if value is None:
        return "no hold"
    until = "staff end it" if value.until == "staff" else in_words(value.until)
    return f"a hold from {in_words(value.from_)} until {until}"


def contest_changes(before: ContestDefinition, after: ContestDefinition) -> tuple[str, ...]:
    """Compare parsed settings, ignoring spelling, key order and contest text."""
    changes: list[str] = []
    old_tasks = {entry.id: entry for entry in before.tasks}
    new_tasks = {entry.id: entry for entry in after.tasks}
    for name in sorted(old_tasks.keys() | new_tasks.keys()):
        old, new = old_tasks.get(name), new_tasks.get(name)
        if old is None or new is None:
            changes.append(
                f"The task {name} is {'added to' if old is None else 'removed from'} the contest."
            )
            continue
        for label, earlier, later in (
            ("release", before.release_of(old), after.release_of(new)),
            ("due", old.due, new.due),
            ("close", before.closes_of(old), after.closes_of(new)),
        ):
            if earlier != later:
                consequence = (
                    f", so lateness is counted again for every {name} submission"
                    if label == "due"
                    else ""
                )
                changes.append(
                    f"{name}'s {label} moves from {_time(earlier)} to {_time(later)}{consequence}."
                )
        if old.withdrawn != new.withdrawn:
            changes.append(f"The task {name} is {'withdrawn' if new.withdrawn else 'put back'}.")
        if old.marks != new.marks:
            earlier_marks = str(old.marks) if old.marks is not None else "the default"
            later_marks = str(new.marks) if new.marks is not None else "the default"
            changes.append(
                f"{name}'s number of marked submissions changes from "
                f"{earlier_marks} to {later_marks}."
            )
    for name in sorted(before.leaderboards.keys() | after.leaderboards.keys()):
        old_board, new_board = before.leaderboards.get(name), after.leaderboards.get(name)
        board = new_board if new_board is not None else old_board
        assert board is not None
        title = board.title_or(name)
        if old_board is None or new_board is None:
            changes.append(
                f"The {title} leaderboard is {'added' if old_board is None else 'removed'}."
            )
            continue
        for key, label in (
            ("audience", "audience changes"),
            ("columns", "columns change"),
            ("sort", "ranking changes"),
            ("display", "displayed columns change"),
        ):
            if getattr(old_board, key) != getattr(new_board, key):
                changes.append(f"The {title} leaderboard's {label}.")
    if before.hold != after.hold:
        changes.append(f"The contest changes from {_hold(before.hold)} to {_hold(after.hold)}.")
    return tuple(changes)
