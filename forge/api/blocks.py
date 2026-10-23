"""The blocks a task's values and a board's columns are built from
(SIMPLER-CORE.md 10.1 and 14): every block once, what it gives, and its
forms with their inputs, kinds and where each may be used, the one list
the editor offers and the forge runs; with the leaves a value and a
column may hold, the facts each may read, the word a fold's `missing`
takes to leave a run out, and the languages an `exec` block's program may
be written in. It holds nothing of any task, so any caller may read it.
`Better` is not a value's nor a column's, which have no direction (#490):
it is which way a run port's report is better, given here for the
backend's workflow and publication schemas to read.
"""

from forge.domain.blocks.catalogue import (
    BOARD_LEAVES,
    CATALOGUE,
    FACTS,
    LANGUAGES,
    LEAVES,
    SKIP,
    TASK_FACTS,
    TASK_LEAVES,
    Block,
    BlockInput,
    Form,
    Kind,
    Scope,
)
from forge.domain.blocks.numbers import Better

__all__ = [
    "BOARD_LEAVES",
    "CATALOGUE",
    "FACTS",
    "LANGUAGES",
    "LEAVES",
    "SKIP",
    "TASK_FACTS",
    "TASK_LEAVES",
    "Better",
    "Block",
    "BlockInput",
    "Form",
    "Kind",
    "Scope",
]
