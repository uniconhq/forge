from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from forge.domain import exact_json
from forge.domain.blocks.catalogue import ALL, FIRST_NOT_PASSED, SHOWN_FACTS
from forge.domain.blocks.check import ValuesCheck, same_value
from forge.domain.definitions import AFTER_RELEASE, ALWAYS, Scoring, Shown, ShowTimes
from forge.domain.port_values import scalar_to_text
from forge.domain.run_task.records import OPENNESS, RELEASED_OPENNESS, SHOWN_WORDS
from forge.domain.run_task.task_helpers import key_json
from forge.domain.workflow_definition import RUN_NAMES, TEXT_OUTPUTS
from forge.domain.yaml_models import Path, Problem, is_number, joined

KINDS = ("values", "inputs", "outputs")
"""The lists of a moment of `show` that name things by name alone."""
RUN_FIELDS = (*RUN_NAMES, *TEXT_OUTPUTS)
"""What every run gives, beside its own inputs and outputs: its verdict,
`passed`, and the `label` and `message` shown with its verdict."""
_LISTED = "{name} is already listed above. Remove this one."
_ALWAYS_SHOWN = (
    "{name} is already shown always, which shows it after the deadline and once results "
    "are released too. Remove this one."
)


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


def listed(
    show: ShowTimes, kind: str, moments: Collection[str] | None = None
) -> dict[str, frozenset[str]]:
    """Each name `show` lists under `kind`, `values`, `inputs` or `outputs`,
    with the moments that list it, of `moments` when given, in the order
    first listed.
    """
    found: dict[str, set[str]] = {}
    for moment, shown in show.moments().items():
        if moments is None or moment in moments:
            for name in getattr(shown, kind):
                found.setdefault(name, set()).add(moment)
    return {name: frozenset(each) for name, each in found.items()}


def picks(
    show: ShowTimes, moments: Collection[str] | None = None
) -> dict[str, dict[str, frozenset[str]]]:
    """Each run entry's `of` that `show` lists, of `moments` when given, with
    each field it shows and the moments that show it, both in the order
    first listed: one `of` listed at two moments shows the fields of both.
    """
    found: dict[str, dict[str, set[str]]] = {}
    for moment, shown in show.moments().items():
        if moments is None or moment in moments:
            for entry in shown.runs:
                fields = found.setdefault(entry.of, {})
                for name in entry.fields:
                    fields.setdefault(name, set()).add(moment)
    return {
        of: {name: frozenset(each) for name, each in fields.items()} for of, fields in found.items()
    }


def results(of: str, fields: Iterable[str], columns: Collection[str]) -> bool:
    """Whether a run entry picking runs `of` and showing `fields` shows
    anything of its runs' results: always for `first_not_passed`, whose
    pick tells which run failed first, and otherwise a field that is not one
    of the run's own inputs, `columns` the task's repeated inputs, which are
    known before any grading.
    """
    return of == FIRST_NOT_PASSED or any(name not in columns for name in fields)


def covers(moments: Collection[str], moment: str) -> bool:
    """Whether something shown at `moments` is shown by `moment`: at it, or
    `always`, which comes first. The two later moments are not ordered
    against each other, since a release may come before the deadline.
    """
    return ALWAYS in moments or moment in moments


def shown_when(moments: Collection[str]) -> str:
    """When something shown at `moments` is first shown, in words: at
    whichever of them comes first."""
    return " or ".join(SHOWN_WORDS[moment] for moment in SHOWN_WORDS if moment in moments)


def listed_words(shown: Shown) -> list[str]:
    """What one moment of `show` lists, in words: each value, setting and
    output by its name, and each field a run entry shows as `score of sample`."""
    found = [*shown.values, *shown.inputs, *shown.outputs]
    for entry in shown.runs:
        found += [f"{name} of {entry.of}" for name in entry.fields]
    return found


def value_moment(show: ShowTimes, name: str) -> str | None:
    """The first moment `show` lists the value `name` under, in the order
    it is written, none when it lists it under none."""
    return next((moment for moment, shown in show.moments().items() if name in shown.values), None)


@dataclass(frozen=True, slots=True)
class ShowNames:
    """What a task's `show` may name, as its save knows the task: its values
    and its groups; the settings it gives once for every run, and of them
    the secrets; the entrant's inputs; the repeated inputs it fills, the
    `repeat` columns (`columns`); and each output of the workflow, with
    whether it is a number or text (true), a file (false), or not known yet,
    as an output of a manifest the task gives (none).
    """

    values: Collection[str]
    groups: Collection[str]
    settings: Collection[str]
    secrets: Collection[str]
    entrant: Collection[str]
    columns: Collection[str]
    outputs: Mapping[str, bool | None]


def show_problems(show: ShowTimes, names: ShowNames) -> list[tuple[Path, str]]:
    """Each entry of `show` that names nothing it may show, at its path
    below `scoring.show`: the same name twice in one list, or a name under a
    later moment that `always` already shows; a value, a setting, a group
    or a run's field the task does not have; and an output that is a file,
    since `show` shows numbers and text. A name may be listed under both
    later moments, to show at whichever comes first. Whether an output is
    given once or for each run is the planner's to check (`once_problems`).
    """
    found: list[tuple[Path, str]] = []
    always = show.always
    for moment, shown in show.moments().items():
        later = moment != ALWAYS
        for kind in KINDS:
            sooner = set(getattr(always, kind)) if later else set()
            seen: set[str] = set()
            for index, name in enumerate(getattr(shown, kind)):
                at: Path = (moment, kind, index)
                if name in seen:
                    found.append((at, _LISTED.format(name=name)))
                elif name in sooner:
                    found.append((at, _ALWAYS_SHOWN.format(name=name)))
                elif (problem := _name_problem(kind, name, names)) is not None:
                    found.append((at, problem))
                seen.add(name)
        shown_always = {entry.of: set(entry.fields) for entry in always.runs} if later else {}
        picked: set[str] = set()
        for index, entry in enumerate(shown.runs):
            at = (moment, "runs", index)
            if entry.of in picked:
                found.append(
                    (
                        (*at, "of"),
                        f"{entry.of} is already listed above. List its fields once, there.",
                    )
                )
                continue
            picked.add(entry.of)
            if entry.of not in (ALL, FIRST_NOT_PASSED, *names.groups):
                found.append(
                    (
                        (*at, "of"),
                        f"The task has no group {entry.of}. Pick runs by a group, {ALL} or "
                        f"{FIRST_NOT_PASSED}.",
                    )
                )
                continue
            seen = set()
            for place, name in enumerate(entry.fields):
                spot: Path = (*at, "fields", place)
                if name in seen:
                    found.append((spot, _LISTED.format(name=name)))
                elif name in shown_always.get(entry.of, ()):
                    found.append((spot, _ALWAYS_SHOWN.format(name=name)))
                elif (problem := _field_problem(name, names)) is not None:
                    found.append((spot, problem))
                seen.add(name)
    return found


def _name_problem(kind: str, name: str, names: ShowNames) -> str | None:
    if kind == "values":
        if name in names.values:
            return None
        if not names.values:
            return f"The task has no value {name}, and no values to show."
        return f"The task has no value {name}. Show one of {joined(sorted(names.values))}."
    if kind == "inputs":
        if name in names.secrets:
            return "A secret is never shown. Remove it."
        if name in names.settings:
            return None
        if name in names.entrant:
            return f"{name} is the entrant's own input, which they already have. Remove it."
        if name in names.columns:
            return f"{name} is repeated, so each run has its own. Show it among a run's fields."
        if not names.settings:
            return f"The task sets no input {name}, and gives no setting to show."
        return f"The task sets no input {name}. Show one of {joined(sorted(names.settings))}."
    if name in SHOWN_FACTS:
        return None
    if name not in names.outputs:
        return (
            f"The workflow has no output {name}. Show {joined([*SHOWN_FACTS])} or an output "
            "its steps give once."
        )
    if names.outputs[name] is False:
        return f"{name} is a file, and show shows numbers and text. Remove it."
    return None


def _field_problem(name: str, names: ShowNames) -> str | None:
    if name in RUN_FIELDS or name in names.columns:
        return None
    if name in names.entrant:
        return f"{name} is the entrant's own input, which they already have. Remove it."
    if name in names.settings:
        return f"{name} is set once for the whole task, so show it under inputs."
    if name in names.outputs:
        if names.outputs[name] is False:
            return f"{name} is a file, and show shows numbers and text. Remove it."
        return None
    inputs = sorted(names.columns)
    return (
        f"A run has no field {name}. Give "
        + (f"one of its repeated inputs, {joined(inputs)}, " if inputs else "")
        + f"{joined([*RUN_FIELDS])}, or an output of the workflow."
    )


def once_problems(
    show: ShowTimes, outputs: Collection[str], once: Collection[str]
) -> list[tuple[Path, str]]:
    """Each output `show` lists under `outputs` that a step gives for each
    run, which a run entry shows among its fields, and each field of a run
    entry that is an output given once for every run, which `outputs`
    shows: `outputs` are the workflow's outputs, and `once` those given
    once, by a step done once or as the same for every item.
    """
    found: list[tuple[Path, str]] = []
    for moment, shown in show.moments().items():
        for index, name in enumerate(shown.outputs):
            if name in outputs and name not in once:
                found.append(
                    (
                        (moment, "outputs", index),
                        f"{name} is given for each run, so show it among a run's fields.",
                    )
                )
        for index, entry in enumerate(shown.runs):
            for place, name in enumerate(entry.fields):
                if name in once:
                    found.append(
                        (
                            (moment, "runs", index, "fields", place),
                            f"{name} is given once for every run, so show it under outputs.",
                        )
                    )
    return found


def read_later(
    scoring: Scoring, values: ValuesCheck, columns: Collection[str]
) -> list[tuple[Path, str]]:
    """Each value `show` lists that reads, directly or through other
    values, a value or `compiled` shown later than itself, or the runs of a
    group shown once results are released while it is shown sooner, at its
    entry, since showing it would give away what is shown later (stage 5
    section 6 items 39 and 124). A value shown before the deadline may sum up
    runs a group shows after it, as `reward` does over `all`
    (SIMPLER-CORE.md 9), which is why its runs are early. A name `show`
    does not list is free to read. `columns` are the task's repeated
    inputs, a run entry showing only those showing no result.
    """
    show = scoring.show
    shown = {**listed(show, "values"), **listed(show, "outputs")}
    groups = {
        of: frozenset(moment for each in fields.values() for moment in each)
        for of, fields in picks(show).items()
        if results(of, fields, columns)
    }
    found: list[tuple[Path, str]] = []
    for moment, entries in show.moments().items():
        for index, entry in enumerate(entries.values):
            if entry not in values.reads:
                continue
            reads = values.through(entry)
            later = sorted(
                {
                    name: shown[name]
                    for name in (reads.values - {entry}) | (reads.facts & set(SHOWN_FACTS))
                    if name in shown and not covers(shown[name], moment)
                }.items()
            ) + sorted(
                (name, groups[name])
                for name in reads.groups
                if name in groups
                and AFTER_RELEASE in groups[name]
                and not covers(groups[name], moment)
            )
            for name, moments in later:
                found.append(
                    (
                        (moment, "values", index),
                        f"{entry} reads {name}, which is shown {shown_when(moments)}, so "
                        f"showing {entry} {SHOWN_WORDS[moment]} would give {name} away sooner. "
                        f"List {entry} no sooner than {name}.",
                    )
                )
    return found


def _compiled_later(moment: str, scoring: Scoring, columns: Collection[str]) -> str | None:
    """Why `compiled`, listed under the later `moment`, cannot be: a run
    entry shown `always` shows the runs' results, whose verdicts tell it
    then. The two later moments are not ordered against each other.
    """
    if moment == ALWAYS:
        return None
    for entry in scoring.show.always.runs:
        if results(entry.of, entry.fields, columns):
            return (
                f"{entry.of}, shown {SHOWN_WORDS[ALWAYS]}, shows {SHOWN_FACTS[0]} with its "
                f"runs, since their verdicts tell it. Remove {SHOWN_FACTS[0]} here, or show "
                f"{entry.of} later."
            )
    return None


def draft_problems(
    scoring: Scoring,
    names: ShowNames,
    once: Collection[str],
    values: ValuesCheck,
) -> list[Problem]:
    """Every problem a save would find with `scoring`'s `show` alone, at
    its path: what it names (`show_problems`), `compiled` shown later than
    a run entry tells it, an output listed where its once-ness does not
    fit (`once_problems`, `once` the outputs given once), and a value shown
    sooner than what it reads (`read_later`, `values` the task's values as
    checked). What the rest of the task would make of it, such as a hidden
    file a run entry shows the results of, is the save's alone.
    """
    show = scoring.show
    found = show_problems(show, names)
    for moment, shown in show.moments().items():
        for index, name in enumerate(shown.outputs):
            if name in SHOWN_FACTS:
                problem = _compiled_later(moment, scoring, names.columns)
                if problem is not None:
                    found.append(((moment, "outputs", index), problem))
    found += once_problems(show, names.outputs, once)
    found += read_later(scoring, values, names.columns)
    return [Problem(path=("scoring", "show", *at), message=message) for at, message in found]


def members(of: str, scoring: Scoring, keys: Mapping[str, Mapping[str, object]]) -> list[str]:
    """The ids of the runs a run entry picks `of`, of the runs `keys` holds
    by id with their keys, in its order: every run for `all`, a group's
    runs, and none for `first_not_passed`, which picks one per submission.
    """
    if of == ALL:
        return list(keys)
    picked = scoring.groups.get(of)
    if picked is None:
        return []
    return [run for run, key in keys.items() if holds(picked, key)]


Key = tuple[str, ...]
"""What a `show` shows, resolved: a value, a setting or an output by its
kind and name, `("values", "total")`, and a run's field by the run's id
and the field, `("runs", "r3", "score")`, `first_not_passed` standing in
for the id of whichever run it picks."""


@dataclass(frozen=True, slots=True)
class ShownUnder:
    """What a `show` shows (`Key`), each with the moments it is shown at."""

    shown: Mapping[Key, frozenset[str]] = field(default_factory=dict)


def shown_under(scoring: Scoring, keys: Mapping[str, Mapping[str, object]]) -> ShownUnder:
    """What `scoring`'s `show` shows of the runs `keys` holds, by id with
    their keys, as a publication's runs file has them, each with the
    moments it is shown at: so moving `score` from `{of: sample}` to
    `{of: all}` still shows each sample run's score.
    """
    found: dict[Key, set[str]] = {}
    for moment, shown in scoring.show.moments().items():
        for kind in KINDS:
            for name in getattr(shown, kind):
                found.setdefault((kind, name), set()).add(moment)
        for entry in shown.runs:
            runs = (
                [FIRST_NOT_PASSED]
                if entry.of == FIRST_NOT_PASSED
                else members(entry.of, scoring, keys)
            )
            for run in runs:
                for name in entry.fields:
                    found.setdefault(("runs", run, name), set()).add(moment)
    return ShownUnder({key: frozenset(each) for key, each in found.items()})


def openness(*, released: bool, revealed: bool) -> Mapping[str, int]:
    """How soon each moment of `show` shows an entry now: `RELEASED_OPENNESS`
    while the task's results are released and its deadline has not passed,
    and `OPENNESS` otherwise, a release not counting as sooner than the
    deadline once that has passed, since a take-back hides it again.
    """
    return RELEASED_OPENNESS if released and not revealed else OPENNESS


def most_shown(shown: Iterable[ShownUnder]) -> ShownUnder:
    """Everything any of `shown` shows, with every moment one shows it at."""
    found: dict[Key, frozenset[str]] = {}
    for each in shown:
        for key, moments in each.shown.items():
            found[key] = found.get(key, frozenset()) | moments
    return ShownUnder(found)


def hidden_again(
    before: ShownUnder,
    scoring: Scoring,
    keys: Mapping[str, Mapping[str, object]],
    rank: Mapping[str, int] = OPENNESS,
) -> list[Problem]:
    """A problem at `scoring`'s `show` for each thing `before` holds, with
    the moments it was shown at under a publication that graded a
    submission, that `show` drops or shows later by `rank` (`openness`,
    SIMPLER-CORE.md 9.4 and 16), the runs being those `keys` holds as the
    save leaves them: a run no longer there has nothing left to show. A
    run's field is compared run by run, whichever entry shows it, and the
    runs of one field hidden at once are told together.
    """
    now = shown_under(scoring, keys)
    hidden: dict[tuple[str, str, str, str | None], int] = {}
    for key, moments in before.shown.items():
        if key[0] == "runs" and key[1] != FIRST_NOT_PASSED and key[1] not in keys:
            continue
        then = _soonest(moments, rank)
        was = _now_soonest(key, now, keys, rank)
        if was is not None and rank[was] >= rank[then]:
            continue
        if key[0] != "runs":
            told = (key[0], key[1], then, was)
        elif key[1] == FIRST_NOT_PASSED:
            told = (FIRST_NOT_PASSED, f"{key[2]} of {FIRST_NOT_PASSED}", then, was)
        else:
            told = ("runs", key[2], then, was)
        hidden[told] = hidden.get(told, 0) + 1
    problems: list[Problem] = []
    for (kind, name, then, was), count in hidden.items():
        entry = f"{name} of {count} {'run' if count == 1 else 'runs'}" if kind == "runs" else name
        problems.append(
            Problem(
                path=("scoring", "show", was) if was else ("scoring", "show"),
                message=f"Entrants have been shown {entry} {SHOWN_WORDS[then]}, and what they "
                "have been shown may have been seen or kept; it would now be hidden or shown "
                "later.",
            )
        )
    return problems


def _soonest(moments: Collection[str], rank: Mapping[str, int]) -> str:
    return max(moments, key=lambda moment: rank[moment])


def _now_soonest(
    key: Key, now: ShownUnder, keys: Mapping[str, Mapping[str, object]], rank: Mapping[str, int]
) -> str | None:
    """The soonest moment `now` shows `key` at, by `rank`, none when it does
    not: the first run not passed's field is shown too where every run's is.
    """
    moments = now.shown.get(key)
    if moments:
        return _soonest(moments, rank)
    if key[0] != "runs" or key[1] != FIRST_NOT_PASSED or not keys:
        return None
    each = [now.shown.get(("runs", run, key[2])) for run in keys]
    if not all(each):
        return None
    return min((_soonest(moments, rank) for moments in each if moments), key=lambda m: rank[m])


def unreleased(shown: ShownUnder) -> ShownUnder:
    """`shown` without what it shows only once results are released: what a
    task whose results are not released has shown, a take-back included
    (stage 5 section 6 item 10).
    """
    return ShownUnder(
        {
            key: moments - {AFTER_RELEASE}
            for key, moments in shown.shown.items()
            if moments - {AFTER_RELEASE}
        }
    )
