from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from forge.domain import exact_json
from forge.domain.blocks.catalogue import ALL, SHOWN_FACTS
from forge.domain.blocks.check import ValuesCheck, same_value
from forge.domain.content import named_by, names_path
from forge.domain.definitions import Scoring, ShowTimes
from forge.domain.port_values import scalar_to_text
from forge.domain.run_task.records import OPENNESS, RELEASED_OPENNESS, SHOWN_WORDS
from forge.domain.run_task.task_helpers import key_json
from forge.domain.yaml_models import Path, Problem, is_number, path_text


def canonical(key: Mapping[str, object]) -> str:
    """A run's key as one text, the same for the same key in any version: a
    number by its amount, so `1` and `1.0` are one key, as a group's pick
    reads them (`same_value`).
    """
    amounts = {
        name: (
            Decimal(scalar_to_text(value))
            if is_number(value) and not isinstance(value, bool)
            else value
        )
        for name, value in key.items()
    }
    return exact_json.dumps(key_json(amounts), sort_keys=True)


def show_names(scoring: Scoring) -> set[str]:
    """The entries of `scoring`'s `show` that name a group, a value or a
    fact a grading gives, `all` and `compiled` among them, rather than a
    task file: every result a board reads of the task, beside the facts
    that go with the row (`ROW_FACTS`, stage 5 section 6 item 124)."""
    return {ALL, *scoring.groups, *scoring.values, *SHOWN_FACTS}


def file_time(scoring: Scoring, path: str) -> str | None:
    """When a task file is first shown: the earliest time of `scoring`'s
    `show` whose list names it or a folder it is in, or none when no entry
    does. An entry naming a group or a value names no file.
    """
    names = show_names(scoring)
    for time, entries in scoring.show.lists().items():
        if any(entry not in names and names_path(entry, path) for entry in entries):
            return time
    return None


def read_later(scoring: Scoring, values: ValuesCheck) -> list[tuple[Path, str]]:
    """Each value `show` lists that reads, directly or through other
    values, a value or `compiled` listed later than itself, or a group
    listed under `on_release` while it is listed sooner, at its entry, since
    showing it would give away what is shown later (stage 5 section 6 items
    39 and 124). A
    value shown before the reveal may sum up runs a group shows at the
    reveal, as `reward` does over `all` (SIMPLER-CORE.md 9), which is why
    its runs run early. A name `show` does not list is free to read.
    """
    listed = shown_times(scoring.show)
    found: list[tuple[Path, str]] = []
    for time, entries in scoring.show.lists().items():
        for index, entry in enumerate(entries):
            if entry not in values.reads:
                continue
            reads = values.through(entry)
            later = sorted(
                name
                for name in (reads.values - {entry})
                | reads.groups
                | (reads.facts & set(SHOWN_FACTS))
                if name in listed
                and OPENNESS[listed[name]] < OPENNESS[time]
                and (name not in reads.groups or listed[name] == "on_release")
            )
            for name in later:
                found.append(
                    (
                        ("scoring", "show", time, index),
                        f"{entry} reads {name}, which is shown {SHOWN_WORDS[listed[name]]}, so "
                        f"showing {entry} {SHOWN_WORDS[time]} would give {name} away sooner. "
                        f"List {entry} no sooner than {name}.",
                    )
                )
    return found


def _under_sooner(entry: str, time: str, show: ShowTimes) -> str | None:
    """Why `entry`, listed under `time`, cannot be: a folder it is in is
    listed under a sooner time, which shows it then, before `time` says.
    A file listed sooner than its folder is shown sooner, as written.
    """
    for folder_time, entries in show.lists().items():
        if OPENNESS[folder_time] <= OPENNESS[time]:
            continue
        for folder in entries:
            if folder.endswith("/") and folder != entry and entry.startswith(folder):
                return (
                    f"{entry} is in {folder}, which is shown {SHOWN_WORDS[folder_time]}, so "
                    f"{entry} is shown then too. Remove it here, or list {folder} later."
                )
    return None


def _compiled_later(entry: str, time: str, scoring: Scoring) -> str | None:
    """Why `compiled`, listed under `time`, cannot be: a group listed
    under a sooner time shows it then, since its runs' verdicts tell it.
    """
    for group_time, entries in scoring.show.lists().items():
        if OPENNESS[group_time] <= OPENNESS[time]:
            continue
        for group in entries:
            if group == ALL or group in scoring.groups:
                return (
                    f"{group}, shown {SHOWN_WORDS[group_time]}, shows {entry} with its runs, "
                    f"since their verdicts tell it. Remove {entry} here, or list {group} later."
                )
    return None


def holds(picked: Mapping[str, object], key: Mapping[str, object]) -> bool:
    """Whether a group picking runs by `picked`, each repeated input's value
    or list of values, holds the run with `key`: one of each input's values
    is the run's, as a number by its amount (`same_value`).
    """
    return all(
        key.get(name) is not None
        and any(
            same_value(key[name], value)
            for value in (given if isinstance(given, list) else [given])
        )
        for name, given in picked.items()
    )


def shown_times(show: ShowTimes) -> dict[str, str]:
    """Each entry of `show` with its time."""
    return {entry: time for time, entries in show.lists().items() for entry in entries}


@dataclass(frozen=True, slots=True)
class Shown:
    """What a `show` lists, each with its time: the groups, values and
    reports by name, and each task file by path, with the entry that lists
    it, a folder's entry standing for every file under it.
    """

    names: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, tuple[str, str]] = field(default_factory=dict)


def shown_under(scoring: Scoring, paths: Collection[str]) -> Shown:
    """What `scoring`'s `show` lists, its files those of `paths`, the task's
    files as the same save left them. An entry naming none of them is a
    name: a group, a value or a report, which the save checked.
    """
    names = show_names(scoring)
    files: dict[str, tuple[str, str]] = {}
    for path in paths:
        for time, entries in scoring.show.lists().items():
            entry = next(
                (entry for entry in entries if entry not in names and names_path(entry, path)),
                None,
            )
            if entry is not None:
                files[path] = (time, entry)
                break
    listed = shown_times(scoring.show)
    return Shown(
        {
            entry: time
            for entry, time in listed.items()
            if entry in names or not named_by(paths, entry)
        },
        files,
    )


def openness(*, released: bool, revealed: bool) -> Mapping[str, int]:
    """How soon each time of `show` shows an entry now: `RELEASED_OPENNESS`
    while the task's results are released and its reveal has not passed,
    and `OPENNESS` otherwise, a release not counting as sooner than the
    reveal once that has passed, since a take-back hides it again.
    """
    return RELEASED_OPENNESS if released and not revealed else OPENNESS


def most_shown(shown: Iterable[Shown], rank: Mapping[str, int] = OPENNESS) -> Shown:
    """Each name and file any of `shown` lists, with the soonest time one
    shows it, by `rank`.
    """
    names: dict[str, str] = {}
    files: dict[str, tuple[str, str]] = {}
    for each in shown:
        for name, time in each.names.items():
            was = names.get(name)
            if was is None or rank[time] > rank[was]:
                names[name] = time
        for path, listed in each.files.items():
            had = files.get(path)
            if had is None or rank[listed[0]] > rank[had[0]]:
                files[path] = listed
    return Shown(names, files)


def hidden_again(
    before: Shown, scoring: Scoring, paths: Collection[str], rank: Mapping[str, int] = OPENNESS
) -> list[Problem]:
    """A problem at `scoring`'s `show` for each name or file `before` holds,
    with the soonest time it was shown under a publication that graded a
    submission, that `show` drops or moves later by `rank` (`openness`,
    SIMPLER-CORE.md 9.4 and 16); `paths` are the task's files as the save
    leaves them, and a file no longer among them has nothing left to show.
    A file is compared by path, whichever entry lists it, and reported by
    the entry that showed it.
    """
    now = shown_under(scoring, paths)
    problems: list[Problem] = []
    for name, then in before.names.items():
        time = now.names.get(name)
        if time is None or rank[time] < rank[then]:
            problems.append(_hidden(name, then, time))
    reported: set[str] = set()
    for path, (then, entry) in sorted(before.files.items()):
        if path not in paths or entry in reported:
            continue
        time = now.files[path][0] if path in now.files else None
        if time is None or rank[time] < rank[then]:
            reported.add(entry)
            problems.append(_hidden(entry, then, time))
    return problems


def unreleased(shown: Shown) -> Shown:
    """`shown` without what it lists under `on_release`: what a task whose
    results are not released has shown, a take-back included (stage 5
    section 6 item 10).
    """
    return Shown(
        {name: time for name, time in shown.names.items() if time != "on_release"},
        {path: listed for path, listed in shown.files.items() if listed[0] != "on_release"},
    )


def _hidden(entry: str, then: str, time: str | None) -> Problem:
    return Problem(
        path=path_text(("scoring", "show", time)) if time else "scoring.show",
        message=f"Entrants have been shown {entry} {SHOWN_WORDS[then]}, and what they have "
        "been shown cannot be hidden again. List it as soon as before.",
    )
