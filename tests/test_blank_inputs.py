"""A field left blank is omitted, not sent empty.

Reported from a real session. A video tool was run with its optional Style
select untouched, so the form collected "" for it and the request carried
`style: ""`. The tool's provider answered:

    Input should be 'anime', '3d_animation', 'clay', 'comic' or 'cyberpunk'

An optional literal takes one of its values or nothing at all, and "" is
neither. The run failed after it was submitted rather than being refused up
front, which is the worst place for a mistake this avoidable to surface.

`build_inputs` already documented the right behaviour ("optional fields left
blank stay blank, so the tool applies its own behaviour rather than being
handed this plugin's idea of it"). Sending "" was this plugin's idea of it.
"""

from __future__ import annotations

import pytest

from rundiffusion_omniverse.api.tools import ToolDetail, ToolField, build_inputs


def _tool(*fields: ToolField) -> ToolDetail:
    return ToolDetail(
        id="tool-1",
        name="Pixverse v5.5",
        tool_fields_hash="v1:abc",
        fields=list(fields),
        tree=[],
    )


def _field(key: str, type_: str = "SINGLE_SELECT", required: bool = False, default=None):
    return ToolField(
        key=key, type=type_, label=key, required=required, default_value=default
    )


class TestBlanksAreOmitted:
    def test_an_untouched_optional_select_is_not_sent(self):
        """The exact run that failed."""
        tool = _tool(_field("style"))
        inputs = build_inputs(tool, {"style": ""}, has_capture=False)
        assert "style" not in inputs

    def test_a_chosen_value_is_sent(self):
        tool = _tool(_field("style"))
        inputs = build_inputs(tool, {"style": "anime"}, has_capture=False)
        assert inputs["style"] == "anime"

    def test_a_none_is_not_sent(self):
        tool = _tool(_field("style"))
        inputs = build_inputs(tool, {"style": None}, has_capture=False)
        assert "style" not in inputs

    @pytest.mark.parametrize("value", [False, 0, "0"])
    def test_a_falsey_value_that_is_not_blank_is_still_sent(self, value):
        """`false` and `0` are answers. Only absence is absence."""
        tool = _tool(_field("switch", type_="BOOLEAN"))
        inputs = build_inputs(tool, {"switch": value}, has_capture=False)
        assert inputs["switch"] == value


class TestRequiredFields:
    def test_a_blank_required_field_falls_back_to_its_default(self):
        tool = _tool(_field("resolution", required=True, default="540p"))
        inputs = build_inputs(tool, {"resolution": ""}, has_capture=False)
        assert inputs["resolution"] == "540p"

    def test_a_blank_required_field_with_no_default_is_left_out(self):
        # So `missing_required_fields` can refuse locally rather than the run
        # failing after it was submitted.
        tool = _tool(_field("prompt", type_="PROMPT", required=True))
        inputs = build_inputs(tool, {"prompt": ""}, has_capture=False)
        assert "prompt" not in inputs

    def test_the_refusal_names_it(self):
        from rundiffusion_omniverse.api.tools import missing_required_fields

        tool = _tool(_field("prompt", type_="PROMPT", required=True))
        inputs = build_inputs(tool, {"prompt": ""}, has_capture=False)
        assert [f.key for f in missing_required_fields(tool, inputs)] == ["prompt"]
