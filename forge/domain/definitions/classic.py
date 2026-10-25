from decimal import Decimal
from typing import Annotated

from pydantic import Field, PlainValidator

from forge.domain.definitions.basics import (
    STARTER_STATEMENT,
    STATEMENT_FILE,
    TASK_FILE,
    TESTS_FOLDER,
    VALUE_NAME,
    Show,
    _yaml_text,
    exact_sum,
)
from forge.domain.yaml_models import Model, Number


class Group(Model):
    """One test group: its rule weights, the share of its tests `pass` asks
    for, its tests' weights, and when it is shown. A rule weight left out is
    0; `show` left out is `always`, except on a group added once the task
    has a graded submission, which must say (T10).
    """

    each: Annotated[Number, Field(ge=0)] | None = None
    worst: Annotated[Number, Field(ge=0)] | None = None
    pass_: Annotated[Number, Field(ge=0)] | None = Field(default=None, alias="pass")
    pass_at: Number | None = None
    test_weights: dict[str, Number] | None = None
    show: Show | None = None

    @property
    def weight(self) -> int | Decimal:
        """What the group can earn, `R_g`: its rule weights summed, to the
        digit."""
        return exact_sum(
            value for value in (self.each, self.worst, self.pass_) if value is not None
        )

    @property
    def shown(self) -> Show:
        return self.show if self.show is not None else Show.ALWAYS


def _value_name(value: object) -> str:
    if not isinstance(value, str) or not VALUE_NAME.match(value):
        raise ValueError("Give the name of a value the workflow reports.")
    return value


class Relative(Model):
    """`{relative: <value>}`: a test earns its value against the best any
    contestant reached on it.
    """

    relative: Annotated[str, PlainValidator(_value_name)]


def _credit(value: object) -> str | Relative:
    if isinstance(value, dict):
        return Relative.model_validate(value)
    return _value_name(value)


def starter_task(name: str) -> dict[str, bytes]:
    """The files a new task is created with: a `task.yaml` titled `name` that
    grades Python with `unicon/classic@v2` on one group of tests, a
    placeholder statement (`STARTER_STATEMENT`), no `public/`, which its first
    picture makes, since git keeps no empty folder, and one
    test, `main/1`, whose input is `1 2` and whose answer is `3`, so the
    first save publishes.
    """
    task = f"""\
# The task's settings. The format is TASK-FORMAT.md, section 1.2.
name: {_yaml_text(name)}
workflow: unicon/classic@v2
inputs:
  submission: {{label: Your solution}}
  language: {{label: Language, options: [python]}}
  time_limit: 2
  memory_limit: 256
test_groups:
  main: {{each: 100}}
"""
    return {
        TASK_FILE: task.encode(),
        STATEMENT_FILE: STARTER_STATEMENT,
        f"{TESTS_FOLDER}main/1/input": b"1 2\n",
        f"{TESTS_FOLDER}main/1/answer": b"3\n",
    }
