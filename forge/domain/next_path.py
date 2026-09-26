"""Where a sign-in may land afterwards. The candidate comes from the browser,
so only a path inside the platform is accepted.
"""

DEFAULT_NEXT = "/"
MAX_LENGTH = 2048


def safe_next(candidate: str | None) -> str:
    if not candidate or not candidate.startswith("/") or len(candidate) > MAX_LENGTH:
        return DEFAULT_NEXT
    if candidate.startswith(("//", "/\\")) or "\n" in candidate or "\r" in candidate:
        return DEFAULT_NEXT
    return candidate
