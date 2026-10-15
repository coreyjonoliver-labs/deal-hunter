"""Plain-text cleanup for AI rating summaries.

aimm takes a summary to be the rest of the model's ``Rating <n>:`` line, plus
any lines after it, so a model that bolds its conclusion (``**Rating 2: ...**``)
leaves the closing ``**`` in the stored text. The summary is AI output shaped by
seller-written listing text, so it is also capped in length. Cleaning only
removes characters: words, numbers, ``$``, ``~``, ``;`` and ``/`` stay.

The result is plain text, not safe markup: whatever renders it (dashboard HTML,
Discord, notifier formats) must still escape it. A marker removed from between
two non-space characters leaves a space, so cleaning can't join the characters
either side into a link, tag or attribute (``ja**vascript:`` stays split).
"""

from __future__ import annotations

import re

MAX_LENGTH = 300

# Runs of two or more are never meaningful in a one-line price summary.
_DOUBLED = re.compile(r"\*{2,}|_{2,}")
# A single * or _ is emphasis only when it hugs a word: opening (not preceded by
# a word character, followed by non-space) or closing (preceded by non-space,
# not followed by a word character). So snake_case, 2*3 and "3 * 4" survive.
_OPEN = re.compile(r"(?<![\w*_])[*_](?=[^\s*_])")
_CLOSE = re.compile(r"(?<=[^\s*_])[*_](?![\w*_])")
_BACKTICKS = re.compile(r"`+")
# Heading and quote markers at the start only; "#1 pick" is left alone.
_LEADING_MARKER = re.compile(r"^(?:#{1,6}\s+|>\s*)+")
# aimm already stripped "Rating <n>:" once; a model that repeats it leaves a copy.
_RATING_PREFIX = re.compile(r"^(?:Rating\s*[1-5]\s*:\s*)+", re.IGNORECASE)


def _remove(match: re.Match[str]) -> str:
    """Drop a marker; between two non-space characters leave a space instead."""
    text = match.string
    before = text[match.start() - 1] if match.start() > 0 else " "
    after = text[match.end()] if match.end() < len(text) else " "
    return " " if not before.isspace() and not after.isspace() else ""


def cap(text: str) -> str:
    """Truncate to MAX_LENGTH characters on a word boundary, ending in an ellipsis."""
    if len(text) <= MAX_LENGTH:
        return text
    cut = text[: MAX_LENGTH - 1]
    if text[MAX_LENGTH - 1] != " ":  # mid-word: back up to the last whole word
        head, sep, _ = cut.rpartition(" ")
        cut = head if sep else cut
    return cut.rstrip(" ;,:-") + "…"


def clean_summary(text: str) -> str:
    """Return text as one plain line of at most MAX_LENGTH characters."""
    text = _BACKTICKS.sub(_remove, text)
    text = _DOUBLED.sub(_remove, text)
    text = _OPEN.sub(_remove, text)
    text = _CLOSE.sub(_remove, text)
    text = " ".join(text.split())
    text = _LEADING_MARKER.sub("", text)
    text = _RATING_PREFIX.sub("", text)
    return cap(" ".join(text.split()))
