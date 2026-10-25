"""How a workflow port's value is written, checked and turned into text."""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from forge.domain.plan_basics import PLANS_FOLDER
from forge.domain.primitives import Port
from forge.domain.types import SCALARS, Type
from forge.domain.workflow_definition import (
    BadReference,
    Reference,
    RunWorkflow,
    references,
    whole_reference,
)
from forge.domain.yaml_models import Problem, exact_number, is_number, path_text


def is_reserved(path: str) -> bool:
    """Whether a path is inside the plans folder, which only the compiler
    writes.
    """
    return path == PLANS_FOLDER.rstrip("/") or path.startswith(PLANS_FOLDER)


def natural(text: str) -> tuple[tuple[int, int, str], ...]:
    """A key that orders text with the numbers in it compared as numbers."""
    return tuple(
        (0, int(part), part) if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", text)
        if part
    )


def scalar_to_text(value: object) -> str:
    """A scalar's one spelling as text: a boolean as `true` or `false`, a
    number as the shortest decimal equal to it, no exponent and no trailing
    zeros, an integer without a point; text and an enum's word as they are.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(Decimal(value))
    if isinstance(value, float | Decimal):
        # Every digit, never rounded to a context's precision, and never
        # through an int's text, which Python refuses past 4300 digits.
        text = format(Decimal(exact_number(value)), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return "0" if text in ("-0", "") else text
    return str(value)


def escaped(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


@dataclass(frozen=True, slots=True)
class Kind:
    """What a value is, to check it against the port it is given to: its
    type, the options it may take when it is an enum, and where it came from.
    """

    type: Type
    options: frozenset[str] | None = None
    source: Literal["literal", "input", "step", "text", "workflow"] = "literal"
    optional: bool = False


def stray_units(workflow: RunWorkflow) -> list[Problem]:
    """A unit on an input that is not a number, refused where a version is
    made or checked, naming the input and its type in words.
    """
    problems: list[Problem] = []
    for key, declared in workflow.inputs.items():
        if declared.unit is not None and declared.type is not Type.NUMBER:
            kind = "one of a list" if declared.type is Type.ENUM else type_in_words(declared.type)
            problems.append(
                Problem(
                    path=path_text(("inputs", key, "unit")),
                    message=f"Only a number takes a unit, and {key} is {kind}. Remove the "
                    f"unit, or make {key} a number.",
                )
            )
    return problems


class Refused(Exception):
    """One problem with a value, said in a sentence; the run workflow's
    checks (`forge.domain.run_workflow`) say theirs with it too.
    """


def literal_or_references(
    raw: object, *, flat: bool = False
) -> Kind | tuple[Reference | None, list[Reference]]:
    """The kind of `raw` when it is true or false or a number; for text,
    the reference it is whole, or else those written into it. `Refused`
    for anything else, and for a `${{ }}` that is no reference.
    """
    if isinstance(raw, bool):
        return Kind(Type.BOOLEAN)
    if is_number(raw):
        return Kind(Type.NUMBER)
    if raw is None or isinstance(raw, list | dict):
        raise Refused(
            "Give text, a number, true or false, or a ${{ }} reference, not a list, a set "
            "of settings or nothing."
        )
    if not isinstance(raw, str):
        raise Refused("Give text, a number, true or false, or a ${{ }} reference.")
    try:
        whole = whole_reference(raw, flat=flat)
        written = [found for _, found in references(raw, flat=flat)] if whole is None else []
    except BadReference as bad:
        raise Refused(str(bad)) from None
    return whole, written


def check_written(found: Reference, kind: Kind) -> None:
    """Refuse `found`, of `kind`, written into text: only text, a number,
    true or false or an enum, always given, may be.
    """
    if kind.type not in SCALARS:
        raise Refused(
            f"{found} is {type_in_words(kind.type)}, and only text, a number, true or false or an "
            "enum can be written into text."
        )
    if kind.optional:
        raise Refused(
            f"{found} is optional, so it can be given only whole, to an optional port, not "
            "written into text."
        )


def check_value_fits_port(kind: Kind, port: Port, raw: object) -> None:
    """Refuse a value of `kind` given to `port`, under the two widenings: a
    scalar fits a text port, and a file fits a folder port.
    """
    wanted = port.type
    if wanted is Type.TEXT and kind.type in SCALARS:
        return
    if wanted is Type.FOLDER and kind.type is Type.FILE:
        return
    if wanted is Type.ENUM:
        allowed = frozenset(port.options or ())
        if kind.type is Type.TEXT and kind.source == "literal":
            if raw in allowed:
                return
            raise Refused(f"This takes one of {', '.join(port.options or ())}.")
        if kind.type is Type.ENUM and kind.options is not None:
            extra = sorted(kind.options - allowed)
            if not extra:
                return
            raise Refused(
                f"This takes one of {', '.join(port.options or ())}, not {', '.join(extra)}."
            )
        raise Refused(f"This takes one of {', '.join(port.options or ())}.")
    if kind.type is not wanted:
        raise Refused(f"This takes {type_in_words(wanted)}, not {type_in_words(kind.type)}.")


def type_in_words(kind: Type) -> str:
    """A type as a sentence names one value of it."""
    return {
        Type.TEXT: "text",
        Type.NUMBER: "a number",
        Type.BOOLEAN: "true or false",
        Type.ENUM: "an enum",
        Type.FILE: "a file",
        Type.FOLDER: "a folder",
        Type.OUTCOME: "an outcome",
    }.get(kind, str(kind))
