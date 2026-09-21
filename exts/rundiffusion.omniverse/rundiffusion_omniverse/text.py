"""Text trimming shared by the panel and the windows it opens.

Here rather than in `panel.py` because `panel` imports `dialogs`, so anything
both need has to sit under both of them.
"""

from __future__ import annotations


def shorten(text: str, limit: int) -> str:
    """Trim to `limit`, on a word boundary where there is one.

    Cutting mid-word reads as a rendering fault rather than as a summary, and
    the ellipsis is what says the rest exists.
    """
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    # The ellipsis is part of the budget, not something added after it. Getting
    # that wrong overruns the tile by exactly the characters that say there is
    # more, which is the one place an overrun is least welcome.
    cut = text[: max(1, limit - 3)]
    spaced = cut.rsplit(" ", 1)[0]
    # Only honour the word boundary if it did not eat most of the line: one
    # very long word would otherwise shorten to nothing at all.
    if len(spaced) >= limit // 2:
        cut = spaced
    return f"{cut.rstrip()}..."
