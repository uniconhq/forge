"""Words math.js reads as an operator or a constant, which cannot name a
value, a group, a column or a leaderboard: a formula would not read the
name as a name.
"""

UNREADABLE = frozenset(
    {"mod", "to", "in", "and", "xor", "or", "not", "true", "false", "null", "undefined"}
)
"""The named delimiters and constants in math.js's expression parser."""


def unreadable_problem(name: str) -> str | None:
    """Why `name` cannot be used, once each `-` is written `_` as a formula
    writes it, or none when a formula would read it as a name.
    """
    read = name.replace("-", "_")
    if read not in UNREADABLE:
        return None
    return (
        f"{read} is a word a formula reads as an operator, so it can't name a value. "
        "Choose another name."
    )
