"""The short plain-text description a workflow's README supplies to search."""

import html
import re

MAX_DESCRIPTION = 2048


def description_of(readme: str) -> str:
    """The first non-heading paragraph, without Markdown decoration, cut at a word."""
    for paragraph in re.split(r"\n\s*\n", readme.strip()):
        lines = paragraph.splitlines()
        if len(lines) > 1 and re.fullmatch(r"\s*(?:=+|-+)\s*", lines[-1]):
            continue
        lines = [line.strip() for line in lines if not line.lstrip().startswith("#")]
        text = " ".join(lines).strip()
        if not text:
            continue
        text = re.sub(r"!?\[([^\[\]]*)\]\([^()\[\]]*\)", r"\1", text)
        text = re.sub(r"<[^<>]*>", "", text)
        text = html.unescape(re.sub(r"(?<!\w)[*_~]+|[*_~]+(?!\w)|`+", "", text))
        text = " ".join(text.split())
        if len(text) <= MAX_DESCRIPTION:
            return text
        cut = text[:MAX_DESCRIPTION]
        return cut.rsplit(" ", 1)[0] if " " in cut else cut
    return ""
