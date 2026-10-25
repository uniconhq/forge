import itertools
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from forge.domain.blocks.catalogue import ALL, EXEC, EXEC_MEMORY_LIMIT, EXEC_TIME_LIMIT, SHOWN_FACTS
from forge.domain.blocks.check import ValuesCheck, check_names, check_values
from forge.domain.blocks.evaluate import blocks_in
from forge.domain.content import has_path, path_problem
from forge.domain.definitions import ENTRANT, TASK_FILE, Form, TaskDefinition, form_problems
from forge.domain.grading import Machine
from forge.domain.plan_basics import PLANS_FOLDER, SECRET_NAME
from forge.domain.port_values import is_reserved, scalar_to_text, type_in_words
from forge.domain.primitives import PROGRAMS, Port
from forge.domain.program_manifest import EXEC_OWN_NAMES, PROGRAM_FILE, parse_program
from forge.domain.run_task.models import EntrantInput, ProgramValue, Run, RunsFile
from forge.domain.run_task.records import MOST_RUNS, SECRETS_LATER, VERDICT_WORDS, Part
from forge.domain.run_task.show_rules import (
    _compiled_later,
    _under_sooner,
    canonical,
    holds,
    show_names,
)
from forge.domain.run_task.task_helpers import (
    _cell,
    _described,
    _entrant_form,
    _read_manifest,
    _value_problem,
)
from forge.domain.run_workflow import (
    InputManifest,
    Program,
    ResolvedReport,
    ResolvedStep,
    RunWorkflowCheck,
    report_ports,
)
from forge.domain.types import Type
from forge.domain.workflow_definition import (
    RUN_NAMES,
    BadReference,
    RunInput,
    RunWorkflow,
    whole_reference,
)
from forge.domain.yaml_models import InvalidDefinition, Path, Problem, is_number, path_text


def manifest_inputs(checked: RunWorkflowCheck) -> set[str]:
    """Every workflow input wired into a manifest port, whose `program.yaml`
    the task's save reads.
    """
    return {
        program.manifest.input
        for step in checked.steps.values()
        for program in (step.program, *step.programs.values())
        if isinstance(program.manifest, InputManifest)
    }


@dataclass(frozen=True, slots=True)
class _Secret:
    name: str


@dataclass
class _Axis:
    """An axis as bound: its inputs that read, by their column, and its rows."""

    index: int
    columns: dict[int, str]
    rows: list[tuple[object, ...]]
    given_as: list[object]


class _Binder:
    def __init__(
        self,
        definition: TaskDefinition,
        workflow: RunWorkflow,
        checked: RunWorkflowCheck,
        paths: Collection[str],
        secrets: Collection[str],
        machine: Machine,
    ) -> None:
        self.task = definition
        self.workflow = workflow
        self.checked = checked
        self.paths = paths
        self.secrets = secrets
        self.machine = machine
        self.problems: list[Problem] = []
        self.settings: dict[str, object] = {}
        self.entrant: dict[str, EntrantInput] = {}
        self.where: dict[str, Path] = {}
        """Where in `task.yaml` each workflow input is filled."""
        self.repeated: dict[str, list[object]] = {}
        """Each repeated input the task fills, with its values in the order
        its axis gives them."""
        self.axes: list[_Axis] = []
        self.ids: dict[str, str] = {}

    def refuse(self, at: Path, message: str) -> None:
        self.problems.append(Problem(path=path_text(at), message=message))

    # 8.1: inputs

    def bind(self) -> None:
        declared = self.workflow.inputs
        for name, given in self.task.inputs.items():
            at: Path = ("inputs", name)
            wanted = declared.get(name)
            if wanted is None:
                self.refuse(
                    at, f"The workflow {self.task.workflow} has no input called {name}. Remove it."
                )
                continue
            self.where[name] = at
            form = _entrant_form(given)
            if isinstance(form, dict):
                self._entrant(at, name, wanted, form, None)
                continue
            if form is not None:
                self.refuse(
                    (*at, ENTRANT),
                    "Give the form details as settings, such as {entrant: {label: Your "
                    "solution}}, or write entrant alone.",
                )
                continue
            problem = self._setting_problem(wanted, given)
            if problem is not None:
                self.refuse(at, problem)
            elif isinstance(given, dict):
                self.settings[name] = _Secret(str(given["secret"]))
            else:
                self.settings[name] = given
        for index, axis in enumerate(self.task.repeat):
            self._axis(index, axis.inputs, axis.rows, axis.tuples)
        for name, wanted in declared.items():
            if name not in self.where and not wanted.optional:
                self.refuse(
                    ("inputs", name),
                    f"Give this input {_described(wanted.type)}, make it the entrant's with "
                    f"{name}: entrant, or repeat it.",
                )
        if not self.entrant and not self.problems:
            self.refuse(
                ("inputs",),
                "No input is the entrant's, so there is nothing to submit. Make one the "
                "entrant's, such as code: entrant.",
            )

    def _entrant(
        self, at: Path, name: str, wanted: RunInput, form: Mapping[str, Any], parts: int | None
    ) -> None:
        problems = form_problems(form, wanted.type.value, wanted.options)
        for path, message in problems:
            self.refuse((*at, ENTRANT, *path), message)
        details = Form() if problems else Form.model_validate(form)
        self.entrant[name] = EntrantInput(name, wanted.type, wanted.options, details, parts)

    def _setting_problem(self, wanted: RunInput, given: object) -> str | None:
        if isinstance(given, dict) and "secret" in given:
            secret = given.get("secret")
            if (
                set(given) != {"secret"}
                or not isinstance(secret, str)
                or not SECRET_NAME.match(secret)
            ):
                return "Give a secret as {secret: <name>}, a name the org holds it under."
            if wanted.type is not Type.TEXT:
                return (
                    f"Only a text input can be a secret, and this one takes "
                    f"{type_in_words(wanted.type)}."
                )
            if secret not in self.secrets:
                return SECRETS_LATER
            return None
        return _value_problem(wanted, given, self.paths)

    def _axis(
        self,
        index: int,
        names: Sequence[str],
        rows: list[tuple[object, ...]],
        given_as: list[object],
    ) -> None:
        declared = self.workflow.inputs
        columns: dict[int, str] = {}
        for column, name in enumerate(names):
            at: Path = ("repeat", index, "inputs", column)
            wanted = declared.get(name)
            if wanted is None:
                self.refuse(at, f"The workflow {self.task.workflow} has no input called {name}.")
                continue
            if name in self.where:
                self.refuse(
                    at,
                    f"{name} is already filled at {path_text(self.where[name])}, and each "
                    "input is filled once: in inputs, or by one axis.",
                )
                continue
            self.where[name] = at
            columns[column] = name
            values = [row[column] for row in rows]
            by_entrant = [value == ENTRANT for value in values]
            if all(by_entrant):
                self._entrant(at, name, wanted, {}, len(rows))
                continue
            if any(by_entrant):
                first = by_entrant.index(not by_entrant[0])
                self.refuse(
                    _cell(index, first, column, name, given_as[first]),
                    f"Either every tuple gives entrant for {name}, or none does: a column is "
                    "filled by the entrant or by the task.",
                )
                continue
            for row, value in enumerate(values):
                problem = _value_problem(wanted, value, self.paths)
                if problem is not None:
                    self.refuse(_cell(index, row, column, name, given_as[row]), problem)
            self.repeated[name] = values
        self.axes.append(_Axis(index, columns, rows, given_as))

    # 8.2: the runs

    def make_runs(self, previous: RunsFile | None) -> list[Run]:
        """Every combination of the axes' tuples, the first axis outermost,
        each with its id; none while an axis is refused.
        """
        if self.problems:
            return []
        keyed: list[list[dict[str, object]]] = []
        for axis in self.axes:
            rows: list[dict[str, object]] = []
            seen: dict[str, int] = {}
            for number, row in enumerate(axis.rows):
                cells = {
                    name: Part(number + 1) if row[column] == ENTRANT else row[column]
                    for column, name in axis.columns.items()
                }
                text = canonical(cells)
                if text in seen:
                    self.refuse(
                        ("repeat", axis.index, "tuples", number),
                        f"This tuple is the same as tuple {seen[text] + 1}, so their runs would "
                        "share a key. List each tuple once.",
                    )
                seen.setdefault(text, number)
                rows.append(cells)
            keyed.append(rows)
        if self.problems:
            return []
        count = math.prod(len(rows) for rows in keyed)
        if count > MOST_RUNS:
            self.refuse(
                ("repeat",),
                f"Every combination of these tuples is {count} runs, and a task can have at most "
                f"{MOST_RUNS}. Give fewer tuples.",
            )
            return []
        ids = dict(previous.ids) if previous is not None else {}
        taken = [int(each[1:]) for each in ids.values() if each[1:].isdigit()]
        following = max(taken, default=0) + 1
        runs: list[Run] = []
        for combination in itertools.product(*keyed):
            key: dict[str, object] = {}
            for part in combination:
                key.update(part)
            text = canonical(key)
            if text not in ids:
                ids[text] = f"r{following}"
                following += 1
            runs.append(Run(ids[text], key, False))
        self.ids = ids
        return runs

    # 9.1: groups

    def groups(self, runs: Sequence[Run]) -> dict[str, tuple[str, ...]]:
        found: dict[str, tuple[str, ...]] = {ALL: tuple(run.id for run in runs)}
        scoring = self.task.scoring
        if scoring is None:
            return found
        for name, picks in scoring.groups.items():
            at: Path = ("scoring", "groups", name)
            if not picks:
                self.refuse(
                    at,
                    "Pick the group's runs by at least one repeated input, such as "
                    "input: [tests/1.in].",
                )
                continue
            wanted: dict[str, list[object]] = {}
            for key, given in picks.items():
                values = given if isinstance(given, list) else [given]
                problem = self._pick_problem(key, values)
                if problem is not None:
                    self.refuse((*at, key), problem)
                    continue
                wanted[key] = values
            if len(wanted) != len(picks):
                continue
            held = tuple(run.id for run in runs if holds(wanted, run.key))
            if not held and runs:
                self.refuse(at, "This group holds no run: no run has all of these values.")
            found[name] = held
        return found

    def _pick_problem(self, key: str, values: list[object]) -> str | None:
        if key in self.entrant:
            return (
                f"{key} is filled by the entrant, and only what the task fills picks a "
                "group's runs."
            )
        if key not in self.repeated:
            if key in self.workflow.inputs:
                return (
                    f"{key} is not repeated, so every run has the same {key}, and it picks "
                    "nothing. Pick by a repeated input."
                )
            return f"There is no repeated input called {key}."
        if not values:
            return "Give at least one value."
        for value in values:
            if value is None or isinstance(value, list | dict):
                return "Give each value as text, a number or true or false."
        return None

    # 9.2 and 9.5: names and values

    def scoring(self, runs: Sequence[Run], manifests: Mapping[str, bytes]) -> ValuesCheck | None:
        scoring = self.task.scoring
        if scoring is None:
            self.refuse(("scoring",), "This is required.")
            return None
        for path, message in check_names(scoring.groups, scoring.values):
            self.refuse(("scoring", *path), message)
        for kind, names in (("groups", scoring.groups), ("values", scoring.values)):
            for name in names:
                if name in self.paths or has_path(self.paths, f"{name}/"):
                    self.refuse(
                        ("scoring", kind, name),
                        f"{name} is also a path in the task, so show could not tell them "
                        "apart. Choose another name.",
                    )
        if ALL in self.paths or has_path(self.paths, f"{ALL}/"):
            self.refuse(
                ("scoring",),
                f"{ALL} is a path in the task, and the name of the group of every run, so "
                "show could not tell them apart. Rename it in the task's files.",
            )
        found = check_values(
            scoring.values,
            groups=[*scoring.groups],
            columns=self._columns(manifests),
            keys=self.repeated,
        )
        for path, message in found.problems:
            self.refuse(("scoring", *path), message)
        return found

    def _columns(self, manifests: Mapping[str, bytes]) -> dict[str, str | None]:
        """Each column of a run, none for a number a fold may read and the
        reason for any other.
        """
        columns: dict[str, str | None] = {}
        for name in [*self.repeated, *(name for name, each in self.entrant.items() if each.parts)]:
            kind = self.workflow.inputs[name].type
            columns[name] = (
                None
                if kind is Type.NUMBER
                else f"{name} is {type_in_words(kind)}, and blocks read numbers only."
            )
        for name, report in self.checked.reports.items():
            port = report.port or self._deferred_port(report, manifests)
            if port is None or port.type is Type.NUMBER:
                columns[name] = None
            else:
                columns[name] = f"{name} is text, and blocks read numbers only."
        columns["verdict"] = VERDICT_WORDS
        columns["passed"] = None
        return columns

    def _deferred_port(self, report: ResolvedReport, manifests: Mapping[str, bytes]) -> Port | None:
        """The type a report of a manifest the task gives has, in the first
        file the task fills it with that reads and reports it; two files that
        disagree are refused at `manifest_types`.
        """
        program = report.made_by
        if program is None or not isinstance(program.manifest, InputManifest):
            return None
        types: dict[str, str] = {}
        for path in self._filled_with(program.manifest.input):
            read = _read_manifest(path, manifests, program)
            if read is not None and report.output in read.reports:
                types.setdefault(read.reports[report.output].type, path)
        if not types:
            return None
        return Port.model_validate({"type": min(types, key=lambda each: types[each])})

    def programs(
        self,
        groups: Mapping[str, tuple[str, ...]],
        manifests: Mapping[str, bytes],
        values: ValuesCheck,
    ) -> dict[str, ProgramValue]:
        """Each value's `exec` program, its blocks checked (`values`): its
        folder one of the task's, holding a `program.yaml` that declares
        one report, a number that is never left out, with which way is
        better; and its group holding runs. A value whose blocks were
        refused is left out, so each mistake is said once.
        """
        scoring = self.task.scoring
        refused = {path[1] for path, _ in values.problems if len(path) > 1 and path[0] == "values"}
        found: dict[str, ProgramValue] = {}
        for name, tree in (scoring.values if scoring is not None else {}).items():
            nodes = blocks_in(tree, EXEC)
            if name in refused or len(nodes) != 1:
                continue
            node = nodes[0]
            at: Path = ("scoring", "values", name)
            folder, language, over = node.get("program"), node.get("language"), node.get("over")
            group = over.get("runs") if isinstance(over, dict) else None
            if not (
                isinstance(folder, str) and isinstance(language, str) and isinstance(group, str)
            ):
                continue
            if not groups.get(group):
                self.refuse(
                    (*at, "over"),
                    f"{group} holds no runs, so the program would have none to read.",
                )
                continue
            if not has_path(self.paths, folder):
                self.refuse((*at, "program"), f"{folder} is not a folder of the task.")
                continue
            path = f"{folder}{PROGRAM_FILE}"
            text = manifests.get(path)
            if text is None:
                self.refuse(
                    (*at, "program"),
                    f"{folder} holds no {PROGRAM_FILE}, which declares the one number the "
                    "program reports.",
                )
                continue
            try:
                read = parse_program(text, path, own_names=EXEC_OWN_NAMES)
            except InvalidDefinition as invalid:
                self.refuse((*at, "program"), f"{path} does not read: {invalid.detail}")
                continue
            if len(read.reports) != 1:
                self.refuse(
                    (*at, "program"),
                    f"{path} declares {len(read.reports)} reports, and a value's program "
                    "reports one number, the value's.",
                )
                continue
            [(report, declared)] = read.reports.items()
            if declared.type != "number" or declared.optional or declared.better is None:
                self.refuse(
                    (*at, "program"),
                    f"{path} declares {report} as {declared.type}"
                    f"{', optional' if declared.optional else ''}"
                    f"{'' if declared.better is not None else ', with no better'}. A value's "
                    "program reports one number, never left out, saying which way is better.",
                )
                continue
            limits = [node.get("time_limit"), node.get("memory_limit")]
            time_limit, memory_limit = (
                given if is_number(given) and not isinstance(given, bool) else default
                for given, default in zip(limits, (EXEC_TIME_LIMIT, EXEC_MEMORY_LIMIT), strict=True)
            )
            assert isinstance(time_limit, int | Decimal) and isinstance(memory_limit, int | Decimal)
            found[name] = ProgramValue(
                name, folder, language, group, time_limit, memory_limit, report, declared.better
            )
        return found

    # 9.4: show

    def show(self) -> None:
        scoring = self.task.scoring
        if scoring is None:
            return
        names = show_names(scoring)
        seen: dict[str, str] = {}
        for time, entries in scoring.show.lists().items():
            for index, entry in enumerate(entries):
                at: Path = ("scoring", "show", time, index)
                if entry in seen:
                    self.refuse(
                        at,
                        f"{entry} is already listed under {seen[entry]}. An entry is shown at "
                        "one time: list it once."
                        if seen[entry] != time
                        else f"{entry} is already listed above. Remove this one.",
                    )
                    continue
                seen[entry] = time
                if entry in SHOWN_FACTS:
                    problem = _compiled_later(entry, time, scoring)
                    if problem is not None:
                        self.refuse(at, problem)
                    continue
                if entry in names:
                    continue
                if entry in self.checked.reports:
                    if entry in self.paths:
                        self.refuse(
                            at,
                            f"{entry} names both a report of the workflow and a file of the task, "
                            "so show cannot tell which it lists. Rename the file.",
                        )
                    continue
                problem = self._shown_path_problem(entry)
                if problem is not None:
                    self.refuse(at, problem)
                    continue
                problem = _under_sooner(entry, time, scoring.show)
                if problem is not None:
                    self.refuse(at, problem)

    def _shown_path_problem(self, entry: str) -> str | None:
        folder = entry.endswith("/")
        problem = path_problem(entry, folder=folder)
        if problem is not None or not (folder or "/" in entry or entry in self.paths):
            return (
                f"{entry} names no group, value, report, file or folder of the task. Give a "
                "group, a value, compiled or a report by its name, a file by its path, or a "
                "folder with a / at its end."
            )
        if is_reserved(entry):
            return f"Unicon writes the files in {PLANS_FOLDER} itself, and serves none of them."
        if entry == TASK_FILE:
            return (
                f"{TASK_FILE} names the task's hidden files and gives its settings, so it is "
                "never served. Remove it."
            )
        if not has_path(self.paths, entry):
            return (
                f"The task has no files in {entry}." if folder else f"The task has no file {entry}."
            )
        return None

    # 8.1, kept: limits, secrets, manifests

    def limits(self, runs: Sequence[Run]) -> None:
        for name, uses in self.checked.uses.items():
            at = self.where.get(name)
            if at is None:
                continue
            if name in self.entrant and any(use.raises_limit for use in uses):
                self.refuse(
                    at, "This value raises a limit, so the task must give it, not the entrant."
                )
            if isinstance(self.settings.get(name), _Secret):
                for use in uses:
                    if not use.whole:
                        self.refuse(
                            at,
                            f"{name} is a secret, and the workflow writes it into text at "
                            f"{use.path}. Give a secret whole, to a port of its own.",
                        )
                    elif not use.port.secret or use.port.type is not Type.TEXT:
                        self.refuse(
                            at,
                            f"{name} is a secret, and the workflow gives it at {use.path}, to a "
                            "port that could pass it on to the entrant's program.",
                        )
        if self.problems:
            return
        for order in self.checked.time_limits:
            self._time_order(
                order.longer,
                order.longer_limit,
                order.shorter,
                order.shorter_limit,
                order.step,
                runs,
            )
        for step in self.workflow.steps:
            resolved = self.checked.steps.get(step.id)
            if resolved is not None:
                self._fit(resolved, step.with_, runs)

    def _time_order(
        self,
        longer: str,
        longer_limit: object,
        shorter: str,
        shorter_limit: object,
        step: str,
        runs: Sequence[Run],
    ) -> None:
        for run in runs:
            first, second = self._limit(longer_limit, run), self._limit(shorter_limit, run)
            if first is None or second is None or first[0] > second[0]:
                continue
            blamed = next((origin for origin in (first[1], second[1]) if origin is not None), None)
            at = self.where.get(blamed, ("inputs",)) if blamed is not None else ("inputs",)
            self.refuse(
                at,
                f"In the step {step}, {longer} is given a manifest, so it judges, and needs a "
                f"longer time limit than {shorter}: {scalar_to_text(first[0])} is not more than "
                f"{scalar_to_text(second[0])}. Each program's wall clock is twice its time limit "
                "and "
                f"a second, and {shorter}'s should run out first.",
            )
            return

    def _limit(self, limit: object, run: Run) -> tuple[int | Decimal, str | None] | None:
        """A time limit a `TimeLimitOrder` holds, a number or an input's id,
        as the number it is in `run`, and the input it comes from.
        """
        if isinstance(limit, str):
            return self._input_number(limit, run)
        return self._number(limit, run)

    def _number(self, raw: object, run: Run) -> tuple[int | Decimal, str | None] | None:
        """A number a port is given in `run`, and the input it comes from;
        none when it is not a number known at the save.
        """
        if is_number(raw):
            assert isinstance(raw, int | Decimal)
            return raw, None
        if not isinstance(raw, str):
            return None
        try:
            found = whole_reference(raw, runs=True)
        except BadReference:
            return None
        if found is None or found.kind != "inputs":
            return None
        return self._input_number(found.name, run)

    def _input_number(self, name: str, run: Run) -> tuple[int | Decimal, str | None] | None:
        value = run.key.get(name, self.settings.get(name))
        if not is_number(value):
            return None
        assert isinstance(value, int | Decimal)
        return value, name

    def _fit(self, step: ResolvedStep, given: Mapping[str, Any], runs: Sequence[Run]) -> None:
        declaration = step.declaration
        if declaration.network and not self.machine.network:
            self.refuse(
                ("workflow",),
                f"The step {step.id} uses {step.use}, which needs the network, but no machine "
                "this task may run on gives a step the network.",
            )
        told: set[str] = set()
        for run in runs:
            numbers: dict[str, int | Decimal] = {}
            origins: list[tuple[str, int | Decimal]] = []
            for port, raw in given.items():
                found = self._number(raw, run) if port != PROGRAMS else None
                if found is not None:
                    numbers[port] = found[0]
                    if found[1] is not None:
                        origins.append((found[1], found[0]))
            programs: dict[str, dict[str, int | Decimal]] = {}
            inner = given.get(PROGRAMS)
            if isinstance(inner, dict):
                for name, values in inner.items():
                    if not isinstance(values, dict):
                        continue
                    for port, raw in values.items():
                        found = self._number(raw, run)
                        if found is not None:
                            programs.setdefault(str(name), {})[port] = found[0]
                            if found[1] is not None:
                                origins.append((found[1], found[0]))
            limits = declaration.item_limits(numbers, programs)
            for limit, most, unit in (
                ("memory_mb", self.machine.memory_mb, "MB of memory"),
                ("gpus", self.machine.gpus, "GPUs"),
            ):
                wanted = limits[limit]
                if wanted <= most or limit in told:
                    continue
                told.add(limit)
                said = (
                    f"the step {step.id} needs {wanted} {unit}, but no machine this task may "
                    f"run on has more than {most}."
                )
                if origins:
                    name, value = max(origins, key=lambda origin: origin[1])
                    self.refuse(self.where[name], f"With {name} at {scalar_to_text(value)}, {said}")
                else:
                    self.refuse(("workflow",), f"As {self.task.workflow} sets it, {said}")

    def manifests(self, runs: Sequence[Run], manifests: Mapping[str, bytes]) -> None:
        """A manifest the task gives: never the entrant's, each file there
        and reading, and each report the workflow reads of it declared, of
        a type its port takes.
        """
        entrant = sorted(manifest_inputs(self.checked) & set(self.entrant))
        for name in entrant:
            self.refuse(
                self.where[name],
                f"{name} is a program's manifest, which the save reads to check what reads "
                "its reports, so the task must give it, not the entrant.",
            )
        if entrant:
            return
        for deferred in self.checked.deferred:
            step = self.checked.steps.get(deferred.step)
            if step is None:
                continue
            program = step.programs.get(deferred.program) if deferred.program else step.program
            if program is None or not isinstance(program.manifest, InputManifest):
                continue
            name = program.manifest.input
            for path in self._filled_with(name):
                read = _read_manifest(path, manifests, program)
                at = self.where.get(name, ("inputs", name))
                if read is None:
                    self._unread(at, path, manifests, program)
                    continue
                report = read.reports.get(deferred.report)
                if report is None:
                    self.refuse(
                        at,
                        f"{path} reports no {deferred.report}, which the workflow reads at "
                        f"{deferred.path}.",
                    )
                    continue
                optional = report.optional or deferred.program is not None
                if deferred.port is None:
                    if deferred.unit and report.type != "number":
                        self.refuse(
                            at,
                            f"{path} reports {deferred.report} as text, and the workflow "
                            f"gives it a unit at {deferred.path}, which only a number takes.",
                        )
                    continue
                wanted = deferred.port.type
                if wanted is Type.NUMBER and report.type != "number":
                    self.refuse(
                        at,
                        f"{path} reports {deferred.report} as text, and the workflow gives it at "
                        f"{deferred.path} to a port that takes a number.",
                    )
                elif optional and not deferred.port.optional:
                    self.refuse(
                        at,
                        f"{path} reports {deferred.report} as optional, so it may be missing, "
                        f"and the workflow gives it at {deferred.path} to a port that must have "
                        "it.",
                    )

    def manifest_types(self, manifests: Mapping[str, bytes]) -> None:
        """Every report of the files the task fills one manifest with has one
        type in all of them, whether or not the workflow reads it: the step
        declares each report once, as one output of every run.
        """
        for step in self.checked.steps.values():
            for program in [step.program, *step.programs.values()]:
                if not isinstance(program.manifest, InputManifest):
                    continue
                name = program.manifest.input
                types: dict[str, dict[str, str]] = {}
                for path in self._filled_with(name):
                    read = _read_manifest(path, manifests, program)
                    for report, each in (read.reports if read is not None else {}).items():
                        types.setdefault(report, {}).setdefault(each.type, path)
                for report, seen in sorted(types.items()):
                    if len(seen) < 2:
                        continue
                    (one, first), (other, second) = sorted(seen.items(), key=lambda x: x[1])[:2]
                    self.refuse(
                        self.where.get(name, ("inputs", name)),
                        f"{first} reports {report} as {type_in_words(Type(one))} and {second} as "
                        f"{type_in_words(Type(other))}, but step {step.id} writes {report} as one "
                        "output of every run. Give it one type in every file.",
                    )

    def manifest_outputs(self, manifests: Mapping[str, bytes]) -> dict[str, dict[str, Port]]:
        """Each step's outputs that a manifest the task gives declares, keyed
        as the step writes them, `<program>.<report>` for one of its
        programs: every report of every file the task fills the manifest
        with, of the one type `manifest_types` holds them to. A report is
        optional where the file says so or one of those files leaves it
        out, and every report of one of the step's programs, whose step is
        accepted whatever the program says, as a manifest of the workflow's
        makes them (`run_workflow._outputs`); the step's own program's are
        not.
        """
        found: dict[str, dict[str, Port]] = {}
        for step in self.checked.steps.values():
            for name, program in [(None, step.program), *step.programs.items()]:
                if not isinstance(program.manifest, InputManifest):
                    continue
                read = [
                    _read_manifest(path, manifests, program)
                    for path in self._filled_with(program.manifest.input)
                ]
                files = [each for each in read if each is not None]
                for each in files:
                    ports = report_ports(each, every_optional=name is not None)
                    for report, port in ports.items():
                        if not all(report in other.reports for other in files):
                            port = port.model_copy(update={"optional": True})
                        key = f"{name}.{report}" if name is not None else report
                        found.setdefault(step.id, {}).setdefault(key, port)
        return found

    def _unread(
        self, at: Path, path: str, manifests: Mapping[str, bytes], program: Program
    ) -> None:
        text = manifests.get(path)
        if text is None:
            self.refuse(at, f"{path} is not a file of the task, so its manifest cannot be read.")
            return
        assert program.manifest is not None
        try:
            parse_program(text, path, own_names=program.manifest.own_names)
        except InvalidDefinition as invalid:
            for problem in invalid.errors:
                where = f", at {problem['path']}" if problem["path"] else ""
                self.refuse(at, f"{path}{where}: {problem['message']}")

    def _filled_with(self, name: str) -> list[str]:
        """The task files an input is filled with, once or in each tuple."""
        value = self.settings.get(name)
        if isinstance(value, str):
            return [value]
        return sorted({each for each in self.repeated.get(name, []) if isinstance(each, str)})

    # #504: what the entrant could set

    def self_reported(self) -> dict[str, frozenset[str]]:
        """Each column the entrant could set, with the entrant inputs that
        let them; and a refusal where an entrant input reaches a program
        given a manifest that its primitive cannot mark as the entrant's.
        """
        entrant = frozenset(self.entrant)
        for step in self.checked.steps.values():
            for program in (step.program, *step.programs.values()):
                reached = program.controlled_by & entrant
                if program.manifest is None or program.self_reportable or not reached:
                    continue
                name = sorted(reached)[0]
                which = f"the program {program.name} of " if program.name else ""
                self.refuse(
                    self.where[name],
                    f"{name} is the entrant's, and reaches {which}the step {step.id}, which is "
                    f"given a manifest. {step.use} cannot mark what such a program reports as "
                    f"the entrant's own, so the task must give {name} itself.",
                )
        marks: dict[str, frozenset[str]] = {}
        for name, report in self.checked.reports.items():
            if reached := report.self_reported_by & entrant:
                marks[name] = reached
        if reached := self.checked.verdict_self_reported_by & entrant:
            for name in RUN_NAMES:
                marks[name] = reached
        for name, each in self.entrant.items():
            # A number given in parts is a column of its own, which the
            # entrant sets outright.
            if each.parts and each.type is Type.NUMBER:
                marks[name] = frozenset({name})
        return marks

    # 8.1, kept: the entrant's inputs once anyone has submitted

    def entrant_kept(self, previous: RunsFile) -> None:
        now = {name: (each.type, each.parts) for name, each in self.entrant.items()}
        for name in sorted(previous.entrant.keys() | now.keys()):
            then, given = previous.entrant.get(name), now.get(name)
            if then == given:
                continue
            at = self.where.get(name, ("inputs",))
            if then is None:
                said = f"{name} was not the entrant's"
            elif given is None:
                said = f"{name} was the entrant's"
            elif then[0] != given[0]:
                said = f"{name} was {type_in_words(then[0])}"
            else:
                said = (
                    f"{name} was given once"
                    if then[1] is None
                    else f"{name} was given in {then[1]} parts"
                )
                if given[1] is not None and then[1] is not None:
                    continue  # more or fewer parts keeps each part's layout
            self.refuse(
                at,
                f"{said}, and entrants have submitted, so which inputs are theirs, and of what "
                "type, cannot change.",
            )
