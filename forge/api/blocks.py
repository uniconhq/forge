"""The blocks a task's values are built from (SIMPLER-CORE.md 14): every
block once, what it gives, and its forms with their inputs and kinds, the
one list the editor offers and the forge runs; with the leaves a tree may
hold, the facts a value may read, and the word a fold's `missing` takes to
leave a run out. It holds nothing of any task, so any caller may read it.
"""

from forge.domain.blocks.catalogue import (
    CATALOGUE,
    LEAVES,
    SKIP,
    TASK_FACTS,
    Block,
    BlockInput,
    Form,
    Kind,
)

__all__ = ["CATALOGUE", "LEAVES", "SKIP", "TASK_FACTS", "Block", "BlockInput", "Form", "Kind"]
