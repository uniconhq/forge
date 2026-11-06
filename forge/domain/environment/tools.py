"""Toolchain package names, the versions each language offers, and where its
packages come from; the exact versions always come from the registry."""

TOOLS = {
    "c": "gcc",
    "cpp": "gxx",
    "java": "openjdk",
    "python": "python",
    "javascript": "nodejs",
    "typescript": "typescript",
    "kotlin": "kotlin",
    "csharp": "dotnet",
    "rust": "rust",
    "r": "r-base",
    "ruby": "ruby",
}
LANGUAGES = tuple(TOOLS)
VERSIONS = {
    "c": ("16",),
    "cpp": ("16",),
    "java": ("25",),
    "python": ("3.15",),
    "javascript": ("26", "24"),
    "typescript": ("6.0",),
    "kotlin": ("2.4",),
    "csharp": ("10.0",),
    "rust": ("1.99",),
    "r": ("4.6", "4.5"),
    "ruby": ("4.0",),
}
"""The versions a language may name, newest first: the newest conda-forge
ships for both platforms, and Node's LTS beside its current release. R
offers 4.5 too, since R packages such as `r-testthat` may not be built for
the newest R yet."""
COMPANIONS = {"typescript": "javascript", "kotlin": "java", "rust": "c"}
"""The language whose toolchain another's needs, at that language's own
versions: Node runs TypeScript's output, a JVM runs Kotlin's, and gcc links
Rust's, since the image has no linker."""
SOURCES = {
    "python": "pypi",
    "r": "r",
    "java": "maven",
    "kotlin": "maven",
    "javascript": "npm",
    "typescript": "npm",
}
"""Where a language's `packages` come from; the others take none."""
NAMES = {
    "c": "C",
    "cpp": "C++",
    "java": "Java",
    "python": "Python",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "kotlin": "Kotlin",
    "csharp": "C#",
    "rust": "Rust",
    "r": "R",
    "ruby": "Ruby",
}


def language(name: str) -> str:
    return "python" if name == "ipynb" else name
