from typing import Any

from forge.domain.yaml_models import ANY, ANY_KEY, Retired


def _is_public(value: object) -> bool:
    return value == "public"


def _is_text(value: object) -> bool:
    return isinstance(value, str)


def _is_list(value: object) -> bool:
    return isinstance(value, list)


_OLD_LEADERBOARDS = (
    "Leaderboards are now keyed by name, each with its columns written as blocks, such as "
    "standings: {columns: {...}, sort: [...], display: [...]}. Write each leaderboard under its "
    "name."
)
_OLD_LEADERBOARD_KEY = (
    "A leaderboard is now its columns, written as blocks, with sort, display and audience. "
    "Write which submission counts and how rows rank as columns, and remove this."
)


_NO_WORTH = (
    "A task no longer gives points: what each task counts for is said by a leaderboard's "
    "columns, such as a total that weighs each task's value. Remove this."
)


WAYS_REPLACED = (
    "This setting is no longer used: who may register, and how, is now set by the contest's "
    "ways in. Add a way in instead, and remove this."
)

CONTEST_RETIRED = (
    Retired(
        ("submissions_closed",),
        "This setting is no longer used: each task takes submissions until its own closing "
        "time. Give each task a closing time, or move the contest's end, and remove this.",
    ),
    Retired(
        ("teams",),
        "This setting is no longer used. Set the most people in a team instead, and remove this.",
    ),
    Retired(
        ("team_size",),
        "This is now the most people in a team. Set that instead, and remove this.",
    ),
    *(
        Retired(("registration", key), WAYS_REPLACED)
        for key in ("invite_only", "approval", "email_pattern", "code", "mode", "eligibility")
    ),
    Retired(("visibility",), 'This is now called "everyone". Choose that instead.', _is_public),
    Retired(
        ("on_system_error",),
        "This setting is no longer used: a grading that fails is tried again once by itself, "
        "and a submission keeps its last result until a newer one replaces it. Remove this.",
    ),
    Retired(
        ("tasks", ANY, "label"),
        "A task's label is now its place in the contest's list of tasks, A, B and so on. "
        "Remove this.",
    ),
    Retired(("tasks", ANY, "points"), _NO_WORTH),
    Retired(("tasks", ANY, "worth"), _NO_WORTH),
    Retired(
        ("tasks", ANY, "late_per_day"),
        "A task no longer takes a share off per late day: a late penalty is a value of the "
        "task that reads seconds_late, or a leaderboard column that reads it. Remove this.",
    ),
    Retired(("leaderboards",), _OLD_LEADERBOARDS, _is_list),
    *(
        Retired(("leaderboards", ANY_KEY, key), _OLD_LEADERBOARD_KEY)
        for key in (
            "name",
            "tasks",
            "over",
            "select",
            "order",
            "who",
            "rows",
            "stage",
            "combine",
            "visibility",
            "freeze_at",
            "team_only",
        )
    ),
    Retired(
        ("freeze_at",),
        "Leaderboards no longer freeze: give the contest a hold, from a time until a time "
        "or until staff end it. Remove this.",
    ),
)

TASK_RETIRED = (
    Retired(
        ("release_at",),
        "A task's release time is now set in the contest, on the task's timeline. Set it "
        "there, and remove this.",
    ),
    Retired(
        ("hidden",),
        "A task is now shown from its release time in the contest. To hide results until "
        'the reveal, set a test group\'s "When entrants see results". Remove this.',
    ),
    Retired(
        ("limits",),
        "This setting is no longer used. Set the most submissions and how often entrants may "
        "submit instead, and the largest size on each file input. Remove this.",
    ),
    Retired(
        ("subtasks",),
        "Subtasks are now test groups. Add a test group for each, and remove this.",
    ),
    Retired(
        ("stages",),
        "This setting is no longer used. To hide results, set a test group's \"When entrants "
        'see results"; which submission counts is set on each leaderboard. Remove this.',
    ),
    Retired(
        ("inputs", "contestant"),
        "Inputs are no longer split into the entrant's and the task's. List each input of the "
        "workflow once, and remove this.",
    ),
    Retired(
        ("inputs", "setter"),
        "Inputs are no longer split into the entrant's and the task's. List each input of the "
        "workflow once, and remove this.",
    ),
    Retired(
        ("submissions", "rate"),
        "Give how often entrants may submit as a count and a number of seconds, such as 1 "
        "every 30 seconds.",
        _is_text,
    ),
)


IGNORED_ON_READ = ("on_system_error", "freeze_at")
"""Retired contest keys that nothing took the place of, so a stored file that
still holds one reads with it ignored, and only a save that has it is
refused, naming it. Leaderboards written in the old list form are ignored
on read the same way (`_old_leaderboards`): a stored contest reads with no leaderboards
until a save writes them as columns."""
ENTRY_IGNORED_ON_READ = ("worth", "late_per_day")
"""Retired keys of a task's entry, ignored on read and refused in a save the
same way: a task gives no points since stage 5 (stage 5 section 6, item
123)."""


def _old_leaderboards(document: dict[str, Any]) -> bool:
    return isinstance(document.get("leaderboards"), list)
