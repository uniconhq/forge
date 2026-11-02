"""Toolchain package names; versions always come from the task or registry."""

TOOLS = {"c": "gcc", "cpp": "gxx", "java": "openjdk", "python": "python"}
LANGUAGES = tuple(TOOLS)


def language(name: str) -> str:
    return "python" if name == "ipynb" else name
