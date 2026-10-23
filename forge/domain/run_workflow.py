"""A run workflow checked against the primitives its steps use, as making a
version of it checks it (SIMPLER-CORE.md 7, TASK-FORMAT.md 1.3's typing),
and what the check learns that a task's save and the planner read after it.

Every step uses a contract 6 primitive, as today's format uses only contract
5's, so neither is handed a step it cannot run. Each port is given a value of
its type under the two widenings, and the ports only contract 6 has take
their own shapes:

- a `named_files` port, a mapping of names to files, each name a file name
  (no leading `.`, no `/`), among the port's `names` when it has any, and
  given once among the program's named files;
- a `programs` port, a mapping of one to eight program names to each
  program's ports, typed against what the port declares;
- a `links` port, a list of `"<from> -> <to>"` between programs given, none
  to itself and none twice, and no named file called like a pipe.

`${{ workflow.<path> }}` is a file of the workflow at the commit the version
names, or a folder when it ends in `/`; it must be there, and be what the
port takes. A step whose primitive has `outputs_from: manifest` gives the
reports of the `program.yaml` on its `manifest` port as outputs too, and
under `outputs_from: programs` each program's outputs and reports are
`<name>.<output>`; a program's reports there may be missing, since a
program gives them only on its own verdict of `0` and the step is accepted
whatever it says. A manifest that is a file of the workflow is read here,
with the names the primitive keeps for itself refused; one from an input is
known only once a task fills that input with one of its files, so what reads
it is recorded (`Deferred`) for the task's save; one from a step is never
known at a save, and is refused.

What a number means comes from the declaration or the manifest that made
it, so a report gives it on unchanged (`ResolvedReport.meaning`). Which
inputs are the entrant's is the task's, so for each program a step runs, the
check records which workflow inputs reach its `runs: true` ports, directly
or through any port of an earlier step (`Program.controlled_by`), program by
program inside a programs port, so the entrant's code reaching one program
does not mark another's reports. A manifest program's verdict decides its
step's outcome, and its `REPORT_TEXTS` are outputs too, so the same inputs
are recorded for those (`ResolvedStep.verdict_self_reported_by`,
`RunWorkflowCheck.verdict_self_reported_by`, `ResolvedReport.written_by`).
A task's save marks a report, or the run's `verdict` and `passed`,
self-reported when an entrant input is among them, and refuses an
entrant-controlled manifest program where the primitive has no
`self_reported` port to say so. It also compares what this check could not:
a manifest program's time limit against each program's given no manifest,
when one is an input (`TimeLimitOrder`), and every input that raises a limit
or reaches a secret port (`InputUse`).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from forge.domain.content import has_path
from forge.domain.meanings import ReportMeaning
from forge.domain.plans import (
    Kind,
    Refused,
    a_type,
    check_written,
    fits,
    literal_or_references,
)
from forge.domain.primitives import (
    FILE_NAME,
    LINK,
    MANIFEST,
    MAX_LINKS,
    MAX_PROGRAMS,
    PROGRAM_NAME,
    PROGRAMS,
    WALL_CLOCK,
    FromInput,
    FromMax,
    FromSum,
    Port,
    PrimitiveDeclaration,
)
from forge.domain.program_manifest import (
    EXEC_OWN_NAMES,
    INTERACT_OWN_NAMES,
    TOO_LARGE,
    ProgramManifest,
    parse_program,
)
from forge.domain.types import Type
from forge.domain.workflow_definition import (
    SELF_REPORTED,
    SELF_REPORTED_GIVEN,
    BadReference,
    Reference,
    RunReport,
    RunWorkflow,
    references,
    whole_reference,
)
from forge.domain.yaml_models import (
    InvalidDefinition,
    Path,
    Problem,
    already_listed,
    is_number,
    path_text,
)

TIME_LIMIT = "time_limit"
REPORT_TEXTS = ("label", "message", "staff_message")
"""What a program given a manifest writes in its report folder beside its
verdict and reports, each an output of `exec` of its name."""
STAFF_OUTPUTS = ("staff_message", "extras")
"""The outputs only staff see (SIMPLER-CORE.md 13), which no report may
name, since a report is shown to entrants as `show` lists it."""
MANIFEST_FROM_STEP = (
    "A manifest must be known when the workflow or the task is saved, and a step's output "
    "is known only when it runs. Give a file of the workflow, such as "
    "${{ workflow.checker/program.yaml }}, or an input the task fills with one of its files."
)


@dataclass(frozen=True, slots=True)
class WorkflowFiles:
    """The files of a workflow at the commit a version names: every path,
    and the content of each one wired into a manifest port
    (`manifest_paths`), but those over `MAX_MANIFEST`, which are `oversized`
    and never read; and the size in bytes of each the forge gave one for,
    an upload's its pointer's.
    """

    paths: frozenset[str]
    manifests: Mapping[str, bytes] = field(default_factory=dict)
    oversized: frozenset[str] = frozenset()
    sizes: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowManifest:
    """A `program.yaml` on a manifest port that is a file of the workflow,
    at `path`, which the check reads: `program`, none when it does not read.
    `own_names` are what no report of it may be called: the primitive's own
    outputs and its report folder's.
    """

    path: str
    program: ProgramManifest | None
    own_names: frozenset[str]

    @property
    def source(self) -> str:
        return self.path


@dataclass(frozen=True, slots=True)
class InputManifest:
    """A `program.yaml` on a manifest port given by the workflow input
    `input`, which the task's save reads once the task fills it with one of
    its files. `own_names` as for a `WorkflowManifest`.
    """

    input: str
    own_names: frozenset[str]

    @property
    def source(self) -> str:
        return self.input


Manifest = WorkflowManifest | InputManifest


@dataclass(frozen=True, slots=True)
class Program:
    """What a step runs: the step's own program, `name` none, or one
    program of its programs port. `controlled_by` holds the workflow inputs
    that reach its `runs: true` ports, directly or through earlier steps;
    `manifest` is its `program.yaml`, if it is given one; and
    `self_reportable` says whether the primitive takes `self_reported` for
    it, so whether a program the entrant controls may be given a manifest at
    all.
    """

    name: str | None
    controlled_by: frozenset[str]
    manifest: Manifest | None
    self_reportable: bool


@dataclass(frozen=True, slots=True)
class ResolvedStep:
    """A step as the check resolved it: its declaration, every output a
    later step or a report may read, a manifest's reports among them once
    read, keyed as a reference names them (`<program>.<output>` for one
    program), its own program and each program of its programs port.
    """

    id: str
    use: str
    declaration: PrimitiveDeclaration
    outputs: Mapping[str, Port]
    program: Program
    programs: Mapping[str, Program]

    @property
    def verdict_self_reported_by(self) -> frozenset[str]:
        """The workflow inputs whose being the entrant's lets the entrant's
        code choose the step's outcome: those that reach a program given a
        manifest, whose verdict file decides it.
        """
        found: frozenset[str] = frozenset()
        for program in (self.program, *self.programs.values()):
            if program.manifest is not None:
                found |= program.controlled_by
        return found


@dataclass(frozen=True, slots=True)
class ResolvedReport:
    """A report as the check resolved it: the step and output it reads, with
    the program it is one program's output of, the output's port, none
    while it is a report of a manifest the task gives, the report's title
    and unit, and `made_by`, the program whose manifest declares it, none
    for an output the primitive measured. `written_by` is the program given
    a manifest whose report folder wrote one of `REPORT_TEXTS`, which no
    manifest declares.
    """

    name: str
    step: str
    program: str | None
    output: str
    port: Port | None
    title: str | None
    unit: str | None
    made_by: Program | None
    written_by: Program | None = None

    @property
    def self_reported_by(self) -> frozenset[str]:
        """The workflow inputs whose being the entrant's makes the report
        self-reported (#504): those that reach the program whose report
        folder wrote it.
        """
        program = self.made_by or self.written_by
        return program.controlled_by if program is not None else frozenset()

    def meaning(self, use: str) -> ReportMeaning | None:
        """What the report means, the number's better and bounds from the
        file that made it, `use` the step's primitive; none while it is not
        known.
        """
        if self.port is None:
            return None
        manifest = self.made_by.manifest if self.made_by is not None else None
        made_by = manifest.source if manifest is not None else None
        return ReportMeaning(
            self.name,
            made_by or use,
            self.title,
            self.unit if self.port.type is Type.NUMBER else None,
            self.port.better,
            self.port.at_least,
            self.port.at_most,
        )


@dataclass(frozen=True, slots=True)
class Deferred:
    """A reference to a report of a manifest the task gives, which only the
    task's save can check: where it is, the step and program, the report's
    name, the port it feeds, none for a report of the workflow's, and
    whether that report gives a unit.
    """

    path: str
    step: str
    program: str | None
    report: str
    port: Port | None
    unit: bool = False


@dataclass(frozen=True, slots=True)
class InputUse:
    """One place a workflow input is given: its path, the port, whether that
    port raises a limit, and whether the input is given whole or written
    into text. A task's save reads these to refuse an entrant input that
    raises a limit, and a secret given anywhere but whole to a secret port.
    """

    path: str
    port: Port
    raises_limit: bool
    whole: bool


@dataclass(frozen=True, slots=True)
class TimeLimitOrder:
    """A program given a manifest, `longer`, must have a longer time limit
    than `shorter`, another program of its step, so another program's wall
    clock, twice its time limit and a second, runs out first when they wait
    on each other. Recorded where one of the two is an input, each limit
    being a number or an input's id, for the task's save to compare.
    """

    path: str
    step: str
    longer: str
    longer_limit: int | Decimal | str
    shorter: str
    shorter_limit: int | Decimal | str


@dataclass(frozen=True, slots=True)
class RunWorkflowCheck:
    """Every problem a version of a run workflow is refused for, each at its
    path in `workflow.yaml`, and what a task's save and the planner read of
    it: each step and report resolved, every place an input is given, and
    what only a task can settle.
    """

    problems: list[Problem]
    steps: dict[str, ResolvedStep]
    reports: dict[str, ResolvedReport]
    uses: dict[str, list[InputUse]]
    deferred: list[Deferred]
    time_limits: list[TimeLimitOrder]

    @property
    def verdict_self_reported_by(self) -> frozenset[str]:
        """The workflow inputs whose being the entrant's makes the run's
        `verdict`, and so `passed`, self-reported: the run's verdict is the
        first step's outcome that is not `accepted`, so any step's.
        """
        found: frozenset[str] = frozenset()
        for step in self.steps.values():
            found |= step.verdict_self_reported_by
        return found


def manifest_paths(workflow: RunWorkflow) -> set[str]:
    """Every file of the workflow wired into a manifest port, a step's own
    or a program's, which the check reads.
    """
    found: set[str] = set()
    for step in workflow.steps:
        given = [step.with_.get(MANIFEST)]
        programs = step.with_.get(PROGRAMS)
        if isinstance(programs, dict):
            given.extend(each.get(MANIFEST) for each in programs.values() if isinstance(each, dict))
        for raw in given:
            try:
                reference = whole_reference(raw, runs=True) if isinstance(raw, str) else None
            except BadReference:
                continue
            if reference is not None and reference.kind == "workflow":
                found.add(reference.name)
    return found


def check_run_workflow(
    workflow: RunWorkflow,
    primitives: Mapping[str, PrimitiveDeclaration],
    files: WorkflowFiles | None,
) -> RunWorkflowCheck:
    """What making a version of `workflow` finds, `primitives` holding the
    declaration of every `use:` that is a primitive, by the reference as
    text, and `files` the workflow's own at the version's commit. Without
    `files`, as a check of a text alone, a workflow's path is checked only
    for its form and its manifests are left unread.
    """
    checker = _Checker(workflow, primitives, files)
    for index, step in enumerate(workflow.steps):
        checker.step(("steps", index), step.id, str(step.use), step.with_)
    for name, entry in workflow.reports.items():
        checker.report(name, entry)
    return RunWorkflowCheck(
        checker.problems,
        checker.steps,
        checker.reports,
        checker.uses,
        checker.deferred,
        checker.time_limits,
    )


@dataclass(frozen=True, slots=True)
class _Pending:
    """An output that is a report of a manifest not read yet."""

    step: str
    program: str | None
    report: str


@dataclass
class _Values:
    """What one program's ports were given, as the check reads them back:
    the files it names, and its manifest.
    """

    file_names: set[str] = field(default_factory=set)
    manifest: Manifest | None = None


class _Checker:
    def __init__(
        self,
        workflow: RunWorkflow,
        primitives: Mapping[str, PrimitiveDeclaration],
        files: WorkflowFiles | None,
    ) -> None:
        self.workflow = workflow
        self.primitives = primitives
        self.files = files
        self.problems: list[Problem] = []
        self.steps: dict[str, ResolvedStep] = {}
        self.reports: dict[str, ResolvedReport] = {}
        self.uses: dict[str, list[InputUse]] = {}
        self.deferred: list[Deferred] = []
        self.time_limits: list[TimeLimitOrder] = []
        self.reached: dict[str, frozenset[str]] = {}
        """The workflow inputs that reach each step, through any port."""
        self.refused: set[str] = set()
        """The steps whose `use:` was refused, so a reference to one is
        not refused again.
        """

    def refuse(self, at: Path, message: str) -> None:
        if not message:
            return  # a reference to a step already refused at its `use:`
        self.problems.append(Problem(path=path_text(at), message=message))

    # Steps

    def step(self, where: Path, step_id: str, use: str, given: Mapping[str, Any]) -> None:
        declaration = self.primitives.get(use)
        if declaration is None or declaration.schema_version != 6:
            self.refused.add(step_id)
        if declaration is None:
            self.refuse(
                (*where, "use"),
                f"{use} is not a primitive Unicon knows. Check its owner, name and version.",
            )
            return
        if declaration.schema_version != 6:
            self.refuse(
                (*where, "use"),
                f"{use} is written for workflows with a test block, and this one has none, so "
                "it describes one run. Use a primitive written for runs, such as "
                "unicon/exec@v1.",
            )
            return
        at = (*where, "with")
        self._ports(at, use, declaration.inputs, given)
        raised = {
            source.input
            for source in declaration.limits_from.values()
            if isinstance(source, FromInput)
        }
        own = _Values()
        program_values: dict[str, _Values] = {}
        given_programs: dict[str, Mapping[str, Any]] = {}
        links: tuple[Path, object] | None = None
        for name, raw in given.items():
            port = declaration.inputs.get(name)
            if port is None or name == SELF_REPORTED:
                continue
            if port.type is Type.PROGRAMS:
                over = {
                    source.program_input
                    for source in declaration.limits_from.values()
                    if isinstance(source, FromSum | FromMax)
                }
                given_programs = self._programs((*at, name), use, port, raw, over, program_values)
            elif port.type is Type.LINKS:
                links = ((*at, name), raw)
            elif name == MANIFEST and declaration.outputs_from == "manifest":
                own_names = EXEC_OWN_NAMES | set(declaration.outputs)
                own.manifest = self._manifest((*at, name), raw, port, own_names)
            else:
                self._value((*at, name), raw, port, name in raised, own.file_names)
        programs_port = declaration.inputs.get(PROGRAMS)
        if links is not None:
            names = set(given_programs) if given_programs or PROGRAMS not in given else None
            found = self._links(*links, names)
            self._pipes(at, found, program_values)
        if programs_port is not None:
            self._time_limits((*at, PROGRAMS), step_id, programs_port, given_programs)
        self.reached[step_id] = frozenset(self._inputs_in(given.values()))
        runs = [raw for name, raw in given.items() if _runs(declaration.inputs.get(name))]
        program = Program(
            None,
            frozenset(self._inputs_in(runs)),
            own.manifest,
            SELF_REPORTED in declaration.inputs,
        )
        programs: dict[str, Program] = {}
        for name, values in given_programs.items():
            inner = (programs_port.inputs or {}) if programs_port is not None else {}
            runs = [raw for key, raw in values.items() if _runs(inner.get(key))]
            programs[name] = Program(
                name,
                frozenset(self._inputs_in(runs)),
                program_values[name].manifest,
                SELF_REPORTED in inner,
            )
        self.steps[step_id] = ResolvedStep(
            step_id,
            use,
            declaration,
            self._outputs(declaration, program, programs),
            program,
            programs,
        )

    def _ports(
        self, at: Path, use: str, ports: Mapping[str, Port], given: Mapping[str, Any]
    ) -> None:
        # A program's settings are read as YAML wrote them, so a key may be a
        # number or nothing, which is no input's name and sorts with no text.
        for name in given:
            if not isinstance(name, str):
                self.refuse((*at, str(name)), f"{name!r} is not an input's name. Remove it.")
        unknown = {name for name in given if isinstance(name, str)} - set(ports) - {SELF_REPORTED}
        for name in sorted(unknown):
            self.refuse((*at, name), f"{use} has no input called {name}. Remove it.")
        for name, port in ports.items():
            if not port.optional and name not in given:
                self.refuse((*at, name), f"{use} needs a value for its input {name}.")

    def _outputs(
        self, declaration: PrimitiveDeclaration, program: Program, programs: Mapping[str, Program]
    ) -> dict[str, Port]:
        outputs = dict(declaration.outputs)
        manifest = program.manifest
        if isinstance(manifest, WorkflowManifest) and manifest.program is not None:
            outputs.update(report_ports(manifest.program, every_optional=False))
        held = declaration.inputs.get(PROGRAMS)
        for name, each in programs.items():
            for output, port in ((held.outputs or {}) if held is not None else {}).items():
                outputs[f"{name}.{output}"] = port
            read = each.manifest.program if isinstance(each.manifest, WorkflowManifest) else None
            if read is not None:
                for report, port in report_ports(read, every_optional=True).items():
                    outputs[f"{name}.{report}"] = port
        return outputs

    def _value(
        self, at: Path, raw: object, port: Port, raises_limit: bool, file_names: set[str]
    ) -> None:
        try:
            if port.type is Type.NAMED_FILES:
                self._named_files(at, raw, port, file_names)
            else:
                self._plain(at, raw, port, raises_limit)
        except Refused as refused:
            self.refuse(at, str(refused))

    def _plain(self, at: Path, raw: object, port: Port, raises_limit: bool) -> None:
        kind, written = self._raw_kind(raw)
        if isinstance(kind, _Pending):
            if raises_limit:
                raise Refused(
                    "This value raises a limit, so it must be known when the task is saved."
                )
            if port.type not in (Type.TEXT, Type.NUMBER):
                raise Refused(f"This takes {a_type(port.type)}, and a report is a number or text.")
            self.deferred.append(
                Deferred(path_text(at), kind.step, kind.program, kind.report, port)
            )
            return
        if raises_limit and kind.source in ("step", "text"):
            raise Refused("This value raises a limit, so it must be known when the task is saved.")
        if kind.optional and not port.optional:
            if kind.source == "step":
                raise Refused(
                    "That output may be missing, so it can be given only to an optional port."
                )
            raise Refused("That input is optional, so it can be given only to an optional port.")
        fits(kind, port, raw)
        for name, whole in written:
            self.uses.setdefault(name, []).append(
                InputUse(path_text(at), port, raises_limit, whole)
            )

    def _raw_kind(self, raw: object) -> tuple[Kind | _Pending, list[tuple[str, bool]]]:
        """What `raw` is, and each input it gives, whole or written into
        text.
        """
        read = literal_or_references(raw, runs=True)
        if isinstance(read, Kind):
            return read, []
        whole, written = read
        if whole is not None:
            given = [(whole.name, True)] if whole.kind == "inputs" else []
            return self._kind(whole), given
        inputs: list[tuple[str, bool]] = []
        for found in written:
            if found.kind == "steps":
                raise Refused("A step's output can be given only whole, not written into text.")
            if found.kind == "workflow":
                raise Refused(
                    "A file of the workflow can be given only whole, not written into text."
                )
            kind = self._kind(found)
            assert isinstance(kind, Kind)
            check_written(found, kind)
            inputs.append((found.name, False))
        return Kind(Type.TEXT, source="text" if written else "literal"), inputs

    def _kind(self, found: Reference) -> Kind | _Pending:
        match found.kind:
            case "inputs":
                declared = self.workflow.inputs.get(found.name)
                if declared is None:
                    raise Refused(f"The workflow has no input called {found.name}.")
                options = frozenset(declared.options) if declared.options else None
                return Kind(declared.type, options, "input", declared.optional)
            case "test":
                raise Refused(
                    "A workflow without a test block has no test fields: the task gives each "
                    "run's values in its inputs and says which repeat."
                )
            case "workflow":
                return self._workflow_file(found.name)
        assert found.output is not None
        step, port, _ = self._output(found)
        if port is None:
            return _Pending(step.id, found.program, found.output)
        if port.type is Type.OUTCOME:
            raise Refused(
                "A step's outcome is read by Unicon itself, and cannot be given to another step."
            )
        options = frozenset(port.options) if port.options else None
        return Kind(port.type, options, "step", port.optional)

    def _workflow_file(self, path: str) -> Kind:
        folder = path.endswith("/")
        if self.files is not None:
            if folder and not has_path(self.files.paths, path):
                raise Refused(f"The workflow has no folder {path}.")
            if not folder and path not in self.files.paths:
                if has_path(self.files.paths, f"{path}/"):
                    raise Refused(
                        f"{path} is a folder of the workflow, not a file. A folder is written "
                        f"with a / at its end, ${{{{ workflow.{path}/ }}}}, and given only "
                        "where a folder is taken."
                    )
                raise Refused(f"The workflow has no file {path}.")
        return Kind(Type.FOLDER if folder else Type.FILE, source="workflow")

    def _output(self, found: Reference) -> tuple[ResolvedStep, Port | None, Program | None]:
        """The step and the port of the output `found` reads, none for a
        report of a manifest not read yet, and the program whose manifest
        declares it, if any.
        """
        step_id, name, report, output = found.name, found.program, found.output, found.port
        assert report is not None and output is not None
        step = self.steps.get(step_id)
        if step_id in self.refused:
            raise Refused("")
        if step is None:
            raise Refused(f"There is no step called {step_id} before this one.")
        if name is not None and not (step.programs or PROGRAMS in step.declaration.inputs):
            raise Refused(f"The step {step_id} has no output called {output}.")
        program = step.programs.get(name) if name is not None else step.program
        if name is not None and program is None:
            raise Refused(f"The step {step_id} runs no program called {name}.")
        port = step.outputs.get(output)
        manifest = program.manifest if program is not None else None
        read = manifest.program if isinstance(manifest, WorkflowManifest) else None
        if port is not None:
            declared = (
                read is not None
                and report in read.reports
                and output not in step.declaration.outputs
            )
            return step, port, program if declared else None
        if manifest is not None and read is None and report not in manifest.own_names:
            return step, None, program
        if isinstance(manifest, WorkflowManifest):
            raise Refused(
                f"The step {step_id} has no output called {output}: neither {step.use} gives "
                f"it nor {manifest.path} reports it."
            )
        if name is not None:
            raise Refused(
                f"The step {step_id} has no output called {output}: {step.use} gives no "
                f"{report} of a program, and {name} is given no manifest that reports one."
            )
        raise Refused(f"The step {step_id} has no output called {output}.")

    # The ports only contract 6 has

    def _named_files(self, at: Path, raw: object, port: Port, taken: set[str]) -> None:
        if not isinstance(raw, dict):
            raise Refused(
                "Give the files as a mapping of the names the program sees them under to files, "
                "such as input: ${{ inputs.input }}."
            )
        for name, value in raw.items():
            where = (*at, str(name))
            if not isinstance(name, str) or not FILE_NAME.fullmatch(name):
                self.refuse(
                    where,
                    f"{name!r} cannot be a file's name. Use at most 255 letters, digits, _, . "
                    "and -, not starting with a ., so never a path.",
                )
                continue
            if port.names is not None and name not in port.names:
                self.refuse(where, f"This port takes only the names {', '.join(port.names)}.")
                continue
            if name in taken:
                self.refuse(
                    where,
                    f"{name} is already given to this program. Each name is one file in its "
                    "working directory.",
                )
                continue
            taken.add(name)
            try:
                self._named_file(where, value, port)
            except Refused as refused:
                self.refuse(where, str(refused))

    def _named_file(self, at: Path, value: object, port: Port) -> None:
        try:
            found = whole_reference(value, runs=True) if isinstance(value, str) else None
        except BadReference as bad:
            raise Refused(str(bad)) from None
        if found is None:
            raise Refused(
                "Give each file as a reference, such as ${{ inputs.input }}, "
                "${{ steps.<id>.<output> }} or ${{ workflow.<path> }}."
            )
        kind = self._kind(found)
        if isinstance(kind, _Pending):
            raise Refused("A report is a number or text, and a named file is a file.")
        if kind.type is not Type.FILE:
            raise Refused(f"A named file is one file, not {a_type(kind.type)}.")
        if kind.optional:
            raise Refused("That may be missing, and a named file must be there.")
        if found.kind == "inputs":
            self.uses.setdefault(found.name, []).append(InputUse(path_text(at), port, False, True))

    def _programs(
        self,
        at: Path,
        use: str,
        port: Port,
        raw: object,
        raised: set[str],
        values: dict[str, _Values],
    ) -> dict[str, Mapping[str, Any]]:
        """Check a programs value; the programs that read, with their
        ports' values.
        """
        if not isinstance(raw, dict) or not raw:
            self.refuse(
                at,
                f"Give the programs as a mapping of names to each one's settings, one to "
                f"{MAX_PROGRAMS}, such as entrant: {{program: ${{{{ steps.compile.program }}}}}}.",
            )
            return {}
        if len(raw) > MAX_PROGRAMS:
            self.refuse(at, f"A step runs at most {MAX_PROGRAMS} programs. Run fewer.")
        inner = port.inputs or {}
        found: dict[str, Mapping[str, Any]] = {}
        for name, given in raw.items():
            where = (*at, str(name))
            if not isinstance(name, str) or not PROGRAM_NAME.fullmatch(name):
                self.refuse(
                    where,
                    f"{name!r} cannot be a program's name. Use at most 64 lower case letters, "
                    "digits and underscores (_), starting with a letter.",
                )
                continue
            if name == WALL_CLOCK:
                self.refuse(
                    where,
                    f"{WALL_CLOCK} is what Unicon says stopped a program that ran out of time "
                    "waiting. Call this program something else.",
                )
                continue
            if not isinstance(given, dict):
                self.refuse(
                    where,
                    f"Give the program's settings, such as program: "
                    f"${{{{ steps.compile.program }}}}. {use} takes {', '.join(inner)}.",
                )
                continue
            self._ports(where, use, inner, given)
            if SELF_REPORTED in given:
                self.refuse((*where, SELF_REPORTED), SELF_REPORTED_GIVEN)
            each = values.setdefault(name, _Values())
            for key, value in given.items():
                inner_port = inner.get(key)
                if inner_port is None or key == SELF_REPORTED:
                    continue
                if key == MANIFEST:
                    own_names = INTERACT_OWN_NAMES | set(port.outputs or {})
                    each.manifest = self._manifest((*where, key), value, inner_port, own_names)
                else:
                    self._value((*where, key), value, inner_port, key in raised, each.file_names)
            found[name] = given
        return found

    def _links(self, at: Path, raw: object, names: set[str] | None) -> list[tuple[str, str]]:
        """Check a links value against the programs given, `names`, or
        against nothing when the programs value did not read; the links
        that read, in order.
        """
        if not isinstance(raw, list):
            self.refuse(
                at, 'Give the links as a list, each "<from> -> <to>", such as "entrant -> judge".'
            )
            return []
        if len(raw) > MAX_LINKS:
            self.refuse(at, f"A step has at most {MAX_LINKS} links. Use fewer.")
        found: list[tuple[str, str]] = []
        for index, text in enumerate(raw):
            where = (*at, index)
            matched = LINK.fullmatch(text) if isinstance(text, str) else None
            if matched is None:
                self.refuse(
                    where,
                    'Write each link as "<from> -> <to>", two program names, such as '
                    '"entrant -> judge".',
                )
                continue
            writer, reader = matched.group(1), matched.group(2)
            missing = [name for name in (writer, reader) if names is not None and name not in names]
            if missing:
                self.refuse(where, f"{missing[0]} is not one of the step's programs.")
                continue
            if writer == reader:
                self.refuse(where, f"{writer} cannot be linked to itself.")
                continue
            if (writer, reader) in found:
                self.refuse(where, already_listed(text))
                continue
            found.append((writer, reader))
        return found

    def _pipes(self, at: Path, links: list[tuple[str, str]], values: Mapping[str, _Values]) -> None:
        """Refuse a named file called as one of its program's named pipes:
        after a program's first link out, `to_<reader>`, and after its first
        in, `from_<writer>`.
        """
        writes: set[str] = set()
        reads: set[str] = set()
        pipes: dict[str, list[str]] = {}
        for writer, reader in links:
            if writer in writes:
                pipes.setdefault(writer, []).append(f"to_{reader}")
            writes.add(writer)
            if reader in reads:
                pipes.setdefault(reader, []).append(f"from_{writer}")
            reads.add(reader)
        for name, named in pipes.items():
            given = values.get(name)
            for pipe in named:
                if given is not None and pipe in given.file_names:
                    self.refuse(
                        (*at, PROGRAMS, name),
                        f"{name} has a named file {pipe}, the name of one of its pipes from the "
                        "links. Call the file something else.",
                    )

    def _manifest(
        self, at: Path, raw: object, port: Port, own_names: Iterable[str]
    ) -> Manifest | None:
        """Check what a manifest port is given, and read the manifest when it
        is a file of the workflow.
        """
        try:
            found = whole_reference(raw, runs=True) if isinstance(raw, str) else None
        except BadReference:
            found = None
        if found is not None and found.kind == "steps":
            self.refuse(at, MANIFEST_FROM_STEP)
            return None
        before = len(self.problems)
        self._value(at, raw, port, False, set())
        if found is None or found.kind not in ("inputs", "workflow"):
            return None
        names = frozenset(own_names)
        if found.kind == "inputs":
            return InputManifest(found.name, names)
        if len(self.problems) > before:
            # One that is refused is left unread, so what reads its reports
            # is not refused again for it.
            return WorkflowManifest(found.name, None, names)
        if self.files is not None and found.name in self.files.oversized:
            self.refuse(at, f"{found.name}: {TOO_LARGE}")
            return WorkflowManifest(found.name, None, names)
        text = self.files.manifests.get(found.name) if self.files is not None else None
        if text is None:
            return WorkflowManifest(found.name, None, names)
        try:
            read = parse_program(text, found.name, own_names=names)
        except InvalidDefinition as invalid:
            for problem in invalid.errors:
                where = f", at {problem['path']}" if problem["path"] else ""
                self.refuse(at, f"{found.name}{where}: {problem['message']}")
            return WorkflowManifest(found.name, None, names)
        return WorkflowManifest(found.name, read, names)

    def _time_limits(
        self, at: Path, step_id: str, port: Port, programs: Mapping[str, Mapping[str, Any]]
    ) -> None:
        """A program given a manifest needs a longer time limit than each
        program given none, refused where both are numbers and recorded
        where one is an input. Two programs given manifests are not
        compared, since neither can be the longer of the two.
        """
        if TIME_LIMIT not in (port.inputs or {}):
            return
        for longer, given in programs.items():
            if MANIFEST not in given:
                continue
            for shorter, other in programs.items():
                if MANIFEST in other:
                    continue
                first, second = _limit(given.get(TIME_LIMIT)), _limit(other.get(TIME_LIMIT))
                if first is None or second is None:
                    continue
                where = (*at, longer, TIME_LIMIT)
                if first == second and isinstance(first, str):
                    self.refuse(
                        where,
                        f"{longer} is given a manifest, so it judges, and needs a longer time "
                        f"limit than {shorter}, which this gives the same input, {first}: each "
                        f"program's wall clock is twice its time limit and a second, and "
                        f"{shorter}'s should run out first.",
                    )
                elif isinstance(first, str) or isinstance(second, str):
                    self.time_limits.append(
                        TimeLimitOrder(path_text(where), step_id, longer, first, shorter, second)
                    )
                elif first <= second:
                    self.refuse(
                        where,
                        f"{longer} is given a manifest, so it judges, and needs a longer time "
                        f"limit than {shorter}'s {second}: each program's wall clock is twice "
                        f"its time limit and a second, and {shorter}'s should run out first.",
                    )

    def _inputs_in(self, values: Iterable[object]) -> set[str]:
        """The workflow inputs that reach `values`: each input they name,
        and each that reaches a step they read.
        """
        found: set[str] = set()
        for found_reference in _references_in(values):
            if found_reference.kind == "inputs":
                found.add(found_reference.name)
            elif found_reference.kind == "steps":
                found |= self.reached.get(found_reference.name, frozenset())
        return found

    # Reports

    def report(self, name: str, entry: str | RunReport) -> None:
        at = ("reports", name)
        raw = entry if isinstance(entry, str) else entry.from_
        title = entry.title if isinstance(entry, RunReport) else None
        unit = entry.unit if isinstance(entry, RunReport) else None
        try:
            found = whole_reference(raw, runs=True)
        except BadReference as bad:
            self.refuse(at, str(bad))
            return
        if found is None or found.kind != "steps" or found.output is None:
            self.refuse(at, "Give the step output this reports, as ${{ steps.<id>.<output> }}.")
            return
        if found.output in STAFF_OUTPUTS:
            self.refuse(
                at,
                f"{found.output} is only for staff to see, and a report is shown to entrants. "
                "Report another output, or leave this one to staff.",
            )
            return
        try:
            step, port, made_by = self._output(found)
        except Refused as refused:
            self.refuse(at, str(refused))
            return
        program, report = found.program, found.output
        if port is None:
            self.deferred.append(
                Deferred(path_text(at), step.id, program, report, None, unit is not None)
            )
            pending = step.programs.get(program) if program is not None else step.program
            self.reports[name] = ResolvedReport(
                name, step.id, program, report, None, title, unit, pending
            )
            return
        if port.type not in (Type.TEXT, Type.NUMBER):
            self.refuse(
                at,
                f"steps.{found.name}.{found.port} is {a_type(port.type)}, and only text or a "
                "number can be reported.",
            )
            return
        if unit is not None and port.type is not Type.NUMBER:
            self.refuse(
                (*at, "unit"),
                f"Only a number takes a unit, and {name} is {a_type(port.type)}. Remove the unit.",
            )
        written_by = (
            step.program
            if made_by is None
            and program is None
            and report in REPORT_TEXTS
            and step.declaration.outputs_from == MANIFEST
            and step.program.manifest is not None
            else None
        )
        self.reports[name] = ResolvedReport(
            name, step.id, program, report, port, title, unit, made_by, written_by
        )


def report_ports(manifest: ProgramManifest, *, every_optional: bool) -> dict[str, Port]:
    """Each report of `manifest` as the output it becomes; every one
    optional where the step is accepted whatever the program says.
    """
    return {
        name: Port.model_validate(
            {
                "type": report.type,
                "optional": report.optional or every_optional,
                "better": report.better,
                "at_least": report.at_least,
                "at_most": report.at_most,
            }
        )
        for name, report in manifest.reports.items()
    }


def _runs(port: Port | None) -> bool:
    return port is not None and port.runs is True


def _limit(raw: object) -> int | Decimal | str | None:
    """A time limit as a number, or as the id of the input that gives it."""
    if is_number(raw):
        assert isinstance(raw, int | Decimal)
        return raw
    try:
        found = whole_reference(raw, runs=True) if isinstance(raw, str) else None
    except BadReference:
        return None
    return found.name if found is not None and found.kind == "inputs" else None


def _references_in(values: Iterable[object]) -> list[Reference]:
    """Every reference that reads in `values`, inside mappings and lists
    too.
    """
    found: list[Reference] = []
    for value in values:
        if isinstance(value, str):
            try:
                found.extend(reference for _, reference in references(value, runs=True))
            except BadReference:
                continue
        elif isinstance(value, dict):
            found.extend(_references_in(value.values()))
        elif isinstance(value, list):
            found.extend(_references_in(value))
    return found


__all__ = [
    "STAFF_OUTPUTS",
    "Deferred",
    "InputManifest",
    "InputUse",
    "Manifest",
    "Program",
    "ResolvedReport",
    "ResolvedStep",
    "RunWorkflowCheck",
    "TimeLimitOrder",
    "WorkflowFiles",
    "WorkflowManifest",
    "check_run_workflow",
    "manifest_paths",
    "report_ports",
]
