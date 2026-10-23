from collections.abc import Mapping

from forge.domain.content import FileSet, WrittenEdit
from forge.domain.definitions import Show, TaskDefinition
from forge.domain.identity import Identity
from forge.domain.ids import TaskId
from forge.domain.port_values import is_reserved
from forge.domain.publication_record import Publication
from forge.domain.release import in_words, reveal_of
from forge.domain.roles import contest_id_of, task_scope
from forge.domain.save_effect import ReadChanges, SaveEffect, effect_of
from forge.runtime.context import Context
from forge.services import names, timelines
from forge.services.task_save import sources
from forge.services.task_save.guards import _present
from forge.services.task_save.records import Checked
from forge.services.task_save.writing import _contest


async def _report(
    ctx: Context, task: TaskId, definition: TaskDefinition, checked: Checked
) -> list[str]:
    """The note beside what the save's effect says: the task's reveal in words
    when it hides anything (a group not shown at once, or a sealed step).
    The scoring changes, and the leaderboards the save moves, are the effect's.
    """
    contest = await _contest(ctx, task)
    name = (await names.names_of(ctx, [task])).get(task)
    if contest is None or name is None:
        return []
    entry = contest.entry(name)
    found: list[str] = []
    compiled = checked.compiled
    if checked.run_task is not None:
        hides = bool(checked.run_task.show.after_close)
    else:
        assert compiled is not None
        hides = bool(compiled.sealed) or any(
            group.shown is not Show.ALWAYS for group in definition.test_groups.values()
        )
    if entry is not None and hides:
        extensions = await timelines.every_extension(ctx, contest_id_of(task_scope(task)))
        found.append(
            f"{name} shows its hidden results on {in_words(reveal_of(contest, entry, extensions))}."
        )
    return found


async def _save_effect(
    ctx: Context,
    task: TaskId,
    definition: TaskDefinition,
    latest: Publication | None,
    changed: tuple[str, ...],
    regrades: int,
    read: ReadChanges,
    *,
    steps: tuple[str, ...] | None = None,
    checked_first: bool = False,
    programs: tuple[str, ...] = (),
) -> SaveEffect:
    """What saving `definition` over the latest publication does, in the
    words a running contest asks with before a save and a save that
    published answers with; for a run task, the steps a submission runs
    again, whether that waits for the save's check, and the values whose
    program runs again.
    """
    name = (await names.names_of(ctx, [task])).get(task)
    before = await sources._definition_of(ctx, task, latest) if latest is not None else None
    return effect_of(
        name or definition.name,
        before,
        definition,
        await _contest(ctx, task),
        changes=changed,
        regrades=regrades,
        graded=bool(await sources._graded_under(ctx, task)),
        read=read,
        submitted=await sources._submitted(ctx, task),
        unread=latest is not None and before is None,
        steps=steps,
        checked_first=checked_first,
        programs=programs,
    )


def _programs_again(checked: Checked, steps: tuple[str, ...] | None) -> tuple[str, ...]:
    """The values whose `exec` program a submission runs again, each in a
    container of its own, of the steps that run again."""
    plan = checked.run_plan
    if plan is None or not steps:
        return ()
    return tuple(
        value.name
        for step in steps
        if step in plan.steps
        and (value := plan.steps[step].value) is not None
        and value.report is not None
    )


async def _differing(
    ctx: Context,
    as_: Identity,
    task: TaskId,
    head: FileSet,
    written: Mapping[str, WrittenEdit],
    published: FileSet,
) -> set[str]:
    """Every file outside `plans/` that differs between the state a save
    leaves and the latest publication: one only in either, and one in both
    with other content. A file the save writes is compared by its content,
    any other by its token, which is the same for the same content.
    """
    state = {path for path in _present(head, written) if not is_reserved(path)}
    then = {path for path in published.tokens if not is_reserved(path)}
    differ = state ^ then
    for path in state & then:
        if path in written:
            content = written[path].content
            before = await ctx.forge.content.read_file(as_, task, path, at=published.version)
            if before.content != content:
                differ.add(path)
        elif head.tokens[path] != published.tokens[path]:
            differ.add(path)
    return differ
