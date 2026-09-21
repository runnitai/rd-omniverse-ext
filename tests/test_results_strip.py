"""Two small rules about a finished render, and one about shortening text.

Neither needs Kit. Both were reported from a real session rather than imagined,
which is why they are pinned: "Clear finished" left the results it had just
cleared sitting on screen, and a tile for a run in flight named the tool without
naming which run of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rundiffusion_omniverse.panel import (
    PROMPT_PREVIEW_CHARS,
    SessionResult,
)
from rundiffusion_omniverse.text import shorten as _shorten


def _result(prompt: str = "") -> SessionResult:
    return SessionResult(
        "req-1", Path("render.png"), "Tool", "tool-1", {}, {}, prompt
    )


class TestClearing:
    def test_a_fresh_render_is_not_cleared(self):
        assert _result().cleared is False

    def test_clearing_is_a_flag_rather_than_a_deletion(self):
        """The Create tab stops showing it; the Library tab still can. The file
        is untouched, because clearing a list must not destroy the render the
        user has not saved anywhere yet."""
        result = _result()
        result.cleared = True

        assert result.path == Path("render.png")

    def test_a_render_carries_the_prompt_it_was_made_from(self):
        """Carried rather than looked up: finding it in `values` needs the tool
        schema, and the form has usually moved on to another tool by the time
        anyone opens the render."""
        assert _result("a red barn at dusk").prompt == "a red barn at dusk"


class TestShorten:
    def test_something_short_is_left_alone(self):
        assert _shorten("a red barn", 48) == "a red barn"

    def test_a_long_prompt_is_cut_and_says_so(self):
        text = "a red barn at dusk with long shadows across the wheat field"
        short = _shorten(text, 30)

        assert short.endswith("...")
        assert len(short) <= 30
        assert text.startswith(short[:-3].rstrip())

    def test_it_cuts_on_a_word_boundary(self):
        """Cutting mid-word reads as a rendering fault rather than a summary."""
        assert _shorten("alpha beta gamma delta", 14) == "alpha beta..."

    def test_one_enormous_word_is_cut_anyway(self):
        """Honouring the boundary here would leave nothing at all."""
        short = _shorten("x" * 100, 20)

        assert len(short) <= 20
        assert short.endswith("...")

    def test_newlines_and_runs_of_spaces_collapse(self):
        """A prompt is typed into a multi-line box, and its line breaks are not
        layout instructions for a 96px tile."""
        assert _shorten("a red\n\n  barn", 48) == "a red barn"

    @pytest.mark.parametrize("value", ["", None])
    def test_nothing_shortens_to_nothing(self, value):
        assert _shorten(value, PROMPT_PREVIEW_CHARS) == ""
