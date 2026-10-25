import re
from typing import Annotated, Any, Literal

from pydantic import Field, PlainValidator, model_validator

from forge.domain.yaml_models import Line, Model, fail

COLUMN_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
"""A board's column's name: lower case letters, digits, hyphens and
underscores, starting with a letter, as a formula reads it once each `-`
is written `_`."""
COLUMN_LONGEST = 40


def _rows(value: object) -> Literal["all", "own"] | int:
    if value in ("all", "own"):
        return "all" if value == "all" else "own"
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    raise ValueError(
        "Choose every row, only each entrant's own row, or a number of top rows of at least 1."
    )


class LeaderboardAudience(Model):
    """Who reads a board (SIMPLER-CORE.md 10.2 rule 9): `who`, staff,
    entrants or everyone, and which `rows` they are given: every row, each
    entrant's own, or a number of top rows, those tied with the last and
    the viewer's own added.
    """

    who: Literal["staff", "entrants", "everyone"] = "staff"
    rows: Annotated[Literal["all", "own"] | int, PlainValidator(_rows)] = "all"

    @model_validator(mode="after")
    def _check(self) -> LeaderboardAudience:
        if self.rows == "own" and self.who == "everyone":
            fail(
                [
                    (
                        ("rows",),
                        "A leaderboard shown to everyone cannot show each entrant only their "
                        "own row, since a guest has none. Show every row or the top rows.",
                    )
                ]
            )
        return self


STAFF_ONLY = LeaderboardAudience()
"""A board with no `audience`: staff's alone (#512)."""


def _column_name(value: object) -> str:
    if not isinstance(value, str) or not COLUMN_NAME.match(value) or len(value) > COLUMN_LONGEST:
        raise ValueError(
            f"{value!r} cannot be a column's name. Use at most {COLUMN_LONGEST} lower case "
            "letters, digits, hyphens (-) and underscores (_), starting with a letter."
        )
    return value


ColumnName = Annotated[str, PlainValidator(_column_name)]


def _sort_key(value: object) -> tuple[str, Literal["desc", "asc"]]:
    if isinstance(value, dict) and len(value) == 1:
        [(name, way)] = value.items()
        if way in ("desc", "asc"):
            return _column_name(name), way
    raise ValueError(
        "Give one column and which way it sorts, such as {total: desc}: desc puts the "
        "highest first, asc the lowest."
    )


def _shown(value: object) -> tuple[str, int | None]:
    if isinstance(value, str):
        return _column_name(value), None
    if isinstance(value, dict) and len(value) == 1:
        [(name, how)] = value.items()
        places = how.get("places") if isinstance(how, dict) and set(how) == {"places"} else None
        if isinstance(places, int) and not isinstance(places, bool) and 0 <= places <= 10:
            return _column_name(name), places
    raise ValueError(
        "Give a column's name, or its name with how many decimal places it is shown, such "
        "as {total: {places: 2}}, from 0 to 10."
    )


class Leaderboard(Model):
    """One board (SIMPLER-CORE.md 10, the leaderboards note's 5.10): its
    `title`, the key when left out; who reads it, staff alone when left
    out; its `columns`, each a tree of blocks, checked against the
    catalogue and the tasks they read at the contest's save and at a
    task's (`forge.domain.board_checks`); the
    columns it `sort`s by, in turn, each `desc` or `asc`; and those it
    `display`s, in order, each with how many decimal `places` it is shown
    to, a column's number itself kept exact.
    """

    title: Line | None = None
    audience: LeaderboardAudience = STAFF_ONLY
    columns: dict[Annotated[str, PlainValidator(_column_name)], Any] = Field(default_factory=dict)
    sort: tuple[Annotated[tuple[str, str], PlainValidator(_sort_key)], ...] = ()
    display: tuple[Annotated[tuple[str, int | None], PlainValidator(_shown)], ...] = ()

    def title_or(self, key: str) -> str:
        return self.title if self.title is not None else key

    @property
    def shown(self) -> tuple[str, ...]:
        """The columns displayed, in order."""
        return tuple(name for name, _ in self.display)

    def places(self, column: str) -> int | None:
        return next((places for name, places in self.display if name == column), None)
