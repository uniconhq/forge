"""Where a sign-in may land afterwards. The candidate comes from the browser,
so only a path inside the platform is accepted.
"""

DEFAULT_NEXT = "/"
MAX_LENGTH = 2048


def safe_next(candidate: str | None) -> str:
    if not candidate or not candidate.startswith("/") or len(candidate) > MAX_LENGTH:
        return DEFAULT_NEXT
    # A browser drops a tab or a line break from a URL, so `/<tab>/x` would
    # land on `//x`; none is taken.
    if candidate.startswith(("//", "/\\")) or any(c in candidate for c in "\t\n\r"):
        return DEFAULT_NEXT
    return candidate
