"""An organiser's program's manifest, its `program.yaml` (SIMPLER-CORE.md 7.3,
13.1): the reports the program writes when `exec` or `interact` runs it
given the manifest, each a `number`, the type when none is given, with
which way is better and its bounds, both inclusive, or a `text`, and each
`optional` or not; no other key.

```yaml
reports:
  score: {better: higher, at_least: 0, at_most: 1}
```

A report becomes an output of its name, so its name follows the
primitive's own rule (`REPORT_NAME`) and is none of the names the primitive
gives itself: the report folder's files and its own outputs, `exec`'s
(`EXEC_OWN_NAMES`) or `interact`'s (`INTERACT_OWN_NAMES`), each a copy of
that primitive's `OWN_NAMES`. So the save refuses what the primitive would.
Only the forge reads the YAML: the primitive is handed the manifest's JSON
(`ProgramManifest.as_json`), which its `read_manifest` checks again.
"""

import math
from collections.abc import Collection
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from forge.domain import exact_json
from forge.domain.scoring import Better
from forge.domain.workflow_definition import REPORT_NAME
from forge.domain.yaml_models import (
    InvalidDefinition,
    Model,
    Number,
    Problem,
    Problems,
    load_mapping,
    path_text,
    validate,
)

PROGRAM_FILE = "program.yaml"
MAX_MANIFEST = 256 * 1024
"""The most bytes a `program.yaml` may hold, as a `workflow.yaml` may."""
TOO_LARGE = f"A program.yaml can be at most {MAX_MANIFEST // 1024} KB. Make it smaller."
EXEC_OWN_NAMES = frozenset(
    {
        "verdict",
        "label",
        "message",
        "staff_message",
        "output",
        "time_ms",
        "memory_kb",
        "outcome",
        "extras",
    }
)
"""What no report of a program `exec` runs may be named: the report folder's
own files and `exec`'s own outputs."""
INTERACT_OWN_NAMES = EXEC_OWN_NAMES | {
    "ended",
    "signal",
    "ended_at",
    "stopped_by",
    "facts",
    "reports",
}
"""What no report of a program `interact` runs may be named: `exec`'s, and
what `interact` gives of each program in its facts."""

Flag = Annotated[bool, Field(strict=True)]


class Report(Model):
    """One report a program declares: a `number`, with which way is better
    and its bounds, or a `text`; and whether a verdict of `0` may leave it
    out.
    """

    type: Literal["number", "text"] = "number"
    optional: Flag = False
    better: Better | None = None
    at_least: Number | None = None
    at_most: Number | None = None


class ProgramManifest(Model):
    """A `program.yaml`: the reports the program declares, by name, in the
    order the file gives them.
    """

    reports: dict[str, Report]

    @model_validator(mode="after")
    def _check(self) -> ProgramManifest:
        problems = Problems()
        for name, report in self.reports.items():
            at = ("reports", name)
            if report.type == "text":
                for key in ("better", "at_least", "at_most"):
                    if getattr(report, key) is not None:
                        problems.add(
                            (*at, key),
                            f"{name} is text, and only a number says which way is better or "
                            "its bounds. Remove this.",
                        )
            for key in ("at_least", "at_most"):
                bound = getattr(report, key)
                if bound is not None and not math.isfinite(float(Decimal(bound))):
                    problems.add(
                        (*at, key),
                        f"{bound} is too large for the primitive to read, which reads a bound "
                        "as a double. Give a smaller one.",
                    )
            if (
                report.at_least is not None
                and report.at_most is not None
                and report.at_least > report.at_most
            ):
                problems.add(
                    (*at, "at_most"),
                    f"{name} has at_least {report.at_least} above at_most {report.at_most}, "
                    "so no value fits.",
                )
        problems.raise_any()
        return self

    def as_json(self) -> bytes:
        """The manifest as the primitive reads it, every key written out:
        `{"reports": {<name>: {"type": "number", "optional": false,
        "better": "higher", "at_least": 0, "at_most": 1}}}`, a key left out
        only where the file gives no direction or bound.
        """
        reports = {
            name: {
                "type": report.type,
                "optional": report.optional,
                **({"better": report.better.value} if report.better is not None else {}),
                **({"at_least": report.at_least} if report.at_least is not None else {}),
                **({"at_most": report.at_most} if report.at_most is not None else {}),
            }
            for name, report in self.reports.items()
        }
        return exact_json.dumps({"reports": reports}).encode()


def _written(key: object) -> str:
    """A mapping's key as the YAML wrote it: `true`, not `True`."""
    match key:
        case bool():
            return "true" if key else "false"
        case None:
            return "null"
        case _:
            return str(key)


def _name_problems(reports: object, own_names: Collection[str]) -> list[Problem]:
    """Each report whose name breaks `REPORT_NAME` or is one of `own_names`."""
    problems: list[Problem] = []
    for name in reports if isinstance(reports, dict) else {}:
        where = path_text(("reports", _written(name)))
        if not isinstance(name, str) or not REPORT_NAME.fullmatch(name):
            problems.append(
                Problem(
                    path=where,
                    message=f"{_written(name)!r} cannot be a report's name. Use at most 64 "
                    "lower case letters, digits and underscores (_), starting with a letter.",
                )
            )
        elif name in own_names:
            problems.append(
                Problem(
                    path=where,
                    message=f"{name} is a name the primitive running the program gives "
                    "itself. Report this under another name.",
                )
            )
    return problems


def parse_program(text: bytes | str, path: str, *, own_names: Collection[str]) -> ProgramManifest:
    """The `program.yaml` in `text`, found at `path`, for a program run by
    the primitive whose own names are `own_names` (`EXEC_OWN_NAMES` or
    `INTERACT_OWN_NAMES`). Raises `InvalidDefinition` naming `path` and
    listing every problem at its YAML path, a report's under
    `reports.<name>`, each naming the report. A report whose name is not
    text, a number say, is refused for its name alone: its settings are
    checked once it has one. One over `MAX_MANIFEST` is refused unread.
    """
    if len(text.encode() if isinstance(text, str) else text) > MAX_MANIFEST:
        raise InvalidDefinition(path, [Problem(path="", message=TOO_LARGE)])
    document = load_mapping(path, text)
    reports = document.get("reports")
    named = _name_problems(reports, own_names)
    if isinstance(reports, dict):
        texts = {name: report for name, report in reports.items() if isinstance(name, str)}
        document = {**document, "reports": texts}
    try:
        manifest = validate(ProgramManifest, path, document)
    except InvalidDefinition as invalid:
        raise InvalidDefinition(path, [*named, *invalid.errors]) from None
    if named:
        raise InvalidDefinition(path, named)
    return manifest
