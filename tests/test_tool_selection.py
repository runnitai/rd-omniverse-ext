"""Which tool is selected, now that the dropdown is gone.

The picker used to be a ComboBox and the selection was its integer index, read
back off the widget on demand. It is a button now, so the selection is held as
an id and the lookup has cases an index never had: nothing chosen yet, and a
chosen tool that is no longer in the catalogue (an account switch drops the
list, and the next account may not be able to run what the last one had).
"""

from __future__ import annotations

import pytest

from rundiffusion_omniverse.api.tools import ToolSummary
from rundiffusion_omniverse.panel import RunDiffusionPanel


def _panel(tools, selected_id=None) -> RunDiffusionPanel:
    """A panel with only the two attributes the lookup reads.

    Built without `__init__` on purpose: constructing the real thing needs a
    running Kit app, and this is a pure function over two fields.
    """
    panel = object.__new__(RunDiffusionPanel)
    panel._tools = list(tools)
    panel._selected_tool_id = selected_id
    return panel


TOOLS = [
    ToolSummary(id="a", name="Alpha"),
    ToolSummary(id="b", name="Beta"),
]


class TestSelectedTool:
    def test_nothing_is_selected_when_there_are_no_tools(self):
        assert _panel([])._selected_tool() is None

    def test_the_chosen_tool_is_returned(self):
        assert _panel(TOOLS, "b")._selected_tool().id == "b"

    def test_the_first_tool_stands_in_before_anything_is_chosen(self):
        # What the ComboBox did by opening on index 0.
        assert _panel(TOOLS, None)._selected_tool().id == "a"

    def test_a_selection_that_no_longer_exists_falls_back(self):
        # An account switch drops the catalogue; the id can outlive the tool.
        assert _panel(TOOLS, "gone")._selected_tool().id == "a"

    @pytest.mark.parametrize("selected", [None, "gone"])
    def test_the_fallback_never_raises_on_an_empty_catalogue(self, selected):
        assert _panel([], selected)._selected_tool() is None
