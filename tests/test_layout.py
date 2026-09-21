"""Tests for where each of a tool's fields lands on the Create tab.

The panel groups inputs by POSITION rather than by meaning, because a tool
schema carries no reliable notion of basic versus advanced and the group labels
it does carry are authored per tool. Position needs no such knowledge, but it
does need to be exactly right: the prompt and the images are found by type, the
next two inputs stay on the surface, and everything after them folds away.

Getting this wrong is quiet. A prompt that lands in the folded frame is a tool
that looks like it takes no prompt, and a schema whose every field folds away is
a panel that looks empty.
"""

from __future__ import annotations

import omni.ui as ui
import pytest

from rundiffusion_omniverse.api.tools import ToolDetail, ToolField
from rundiffusion_omniverse.form import INLINE_FIELD_LIMIT, plan_layout, prompt_text
from rundiffusion_omniverse.panel import TAB_CREATE, TAB_LIBRARY, RunDiffusionPanel


class FakeContainer:
    """Enough of an omni.ui container to record what was done to it."""

    def __init__(self) -> None:
        self.cleared = 0
        self.entered = 0

    def clear(self) -> None:
        self.cleared += 1

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *_exc) -> None:
        return None


class FakeModel:
    def __init__(self) -> None:
        self._value = 0

    def set_value(self, value) -> None:
        self._value = value

    def add_value_changed_fn(self, _fn) -> None:
        return None


class FakeWidget:
    def __init__(self, *_args, **_kwargs) -> None:
        self.model = FakeModel()


@pytest.fixture(autouse=True)
def drawable_ui(monkeypatch):
    """The conftest stub is bare; the footer needs the names it builds with."""
    monkeypatch.setattr(ui, "VStack", lambda *a, **k: FakeContainer(), raising=False)
    monkeypatch.setattr(ui, "HStack", lambda *a, **k: FakeContainer(), raising=False)
    monkeypatch.setattr(ui, "Label", FakeWidget, raising=False)
    monkeypatch.setattr(ui, "Button", FakeWidget, raising=False)
    monkeypatch.setattr(ui, "IntSlider", FakeWidget, raising=False)


def _tool(*fields: ToolField) -> ToolDetail:
    return ToolDetail(id="t", name="T", tool_fields_hash="v1:abc", fields=list(fields))


def _field(key: str, type_: str = "SLIDER") -> ToolField:
    return ToolField(key=key, type=type_, label=key.title(), required=False)


class TestPlanLayout:
    def test_the_prompt_is_found_by_type_not_by_position(self):
        """It renders first whatever order the schema declares it in."""
        plan = plan_layout(
            _tool(_field("steps"), _field("prompt", "PROMPT"), _field("cfg"))
        )

        assert plan.prompt is not None
        assert plan.prompt.key == "prompt"

    def test_image_fields_are_collected_in_schema_order(self):
        plan = plan_layout(
            _tool(_field("start", "IMG"), _field("prompt", "PROMPT"), _field("refs", "IMGS"))
        )

        assert [f.key for f in plan.images] == ["start", "refs"]

    def test_the_first_two_remaining_inputs_stay_on_the_surface(self):
        plan = plan_layout(
            _tool(
                _field("prompt", "PROMPT"),
                _field("size", "WIDTH_HEIGHT"),
                _field("count", "NUM"),
                _field("cfg"),
                _field("steps"),
            )
        )

        assert [f.key for f in plan.inline] == ["size", "count"]
        assert [f.key for f in plan.more] == ["cfg", "steps"]
        assert len(plan.inline) <= INLINE_FIELD_LIMIT

    def test_a_small_tool_never_folds_anything_away(self):
        """A three-input tool showing a More settings frame would be chrome
        around nothing."""
        plan = plan_layout(
            _tool(_field("prompt", "PROMPT"), _field("img", "IMG"), _field("size"))
        )

        assert plan.more == []
        assert plan.has_more is False

    def test_a_prompt_and_images_never_count_against_the_inline_budget(self):
        """They are found by type and rendered first, so a tool with a prompt,
        an image field and two settings shows all four."""
        plan = plan_layout(
            _tool(
                _field("prompt", "PROMPT"),
                _field("img", "IMG"),
                _field("size"),
                _field("count"),
            )
        )

        assert [f.key for f in plan.inline] == ["size", "count"]
        assert plan.has_more is False

    def test_display_only_fields_are_not_laid_out_at_all(self):
        """HEADER and HTML carry no value and are never sent, so they must not
        take one of the two inline places from a field that does."""
        plan = plan_layout(
            _tool(
                _field("heading", "HEADER"),
                _field("blurb", "HTML"),
                _field("size"),
                _field("count"),
                _field("cfg"),
            )
        )

        assert [f.key for f in plan.inline] == ["size", "count"]
        assert [f.key for f in plan.more] == ["cfg"]

    def test_a_second_prompt_field_is_treated_as_an_ordinary_input(self):
        """Promoting two prompts would mean choosing which one is THE prompt,
        which is the kind of guess this layout exists to avoid."""
        plan = plan_layout(
            _tool(_field("prompt", "PROMPT"), _field("prompt2", "PROMPT"), _field("cfg"))
        )

        assert plan.prompt.key == "prompt"
        assert [f.key for f in plan.inline] == ["prompt2", "cfg"]

    def test_a_negative_prompt_is_not_the_prompt(self):
        """NEG_PROMPT is a prompt in shape only, and writing the user's words
        into it inverts their intent."""
        plan = plan_layout(_tool(_field("neg", "NEG_PROMPT"), _field("prompt", "PROMPT")))

        assert plan.prompt.key == "prompt"
        assert [f.key for f in plan.inline] == ["neg"]

    def test_every_field_lands_somewhere(self):
        """The invariant behind the whole layout: nothing is dropped, so
        nothing a tool declares can become unfillable."""
        fields = [
            _field("prompt", "PROMPT"),
            _field("img", "IMG"),
            _field("a"),
            _field("b"),
            _field("c"),
            _field("d"),
        ]
        plan = plan_layout(_tool(*fields))

        placed = (
            ([plan.prompt] if plan.prompt else []) + plan.images + plan.inline + plan.more
        )

        assert [f.key for f in placed] == [f.key for f in fields]


class TestPromptText:
    """What was asked for, in words, for the tile that stands in for a render
    while it is being made and for the caption in the viewer.

    Found through the same layout plan the form renders by, so the words shown
    beside a run cannot drift from the field the user actually typed into. A
    tool's field keys are its own, so there is nothing here to hardcode.
    """

    def test_it_reads_the_field_the_form_calls_the_prompt(self):
        tool = _tool(_field("steps"), _field("caption", "PROMPT"))

        assert prompt_text(tool, {"caption": "a red barn", "steps": 20}) == "a red barn"

    def test_a_tool_with_no_prompt_field_has_no_prompt(self):
        """Upscalers and background removers take an image and nothing to say
        about it. The tile shows the tool alone rather than an empty line."""
        tool = _tool(_field("scale"), _field("image", "IMG"))

        assert prompt_text(tool, {"scale": 2}) == ""

    def test_an_unfilled_prompt_is_empty_rather_than_none(self):
        tool = _tool(_field("prompt", "PROMPT"))

        assert prompt_text(tool, {}) == ""
        assert prompt_text(tool, {"prompt": None}) == ""

    def test_surrounding_whitespace_is_dropped(self):
        tool = _tool(_field("prompt", "PROMPT"))

        assert prompt_text(tool, {"prompt": "  a red barn \n"}) == "a red barn"

    def test_the_first_prompt_wins_the_same_way_the_form_picks_it(self):
        """plan_layout promotes the first PROMPT and leaves any second one in
        the fields below. Disagreeing here would caption a run with a field the
        user did not think of as the prompt."""
        tool = _tool(_field("prompt", "PROMPT"), _field("style", "PROMPT"))

        assert prompt_text(tool, {"prompt": "first", "style": "second"}) == "first"


class TestTheFooterAction:
    """Generate is in the footer, not at the bottom of the Create tab.

    The form scrolls and the results strip grows under it, so the one button
    that spends money was the thing most likely to be off screen at the moment
    someone wanted it. Pinned in the footer it is in the same place on every
    visit, and directly above the line naming who pays for it.

    It is empty on every other tab: a Generate button on the Library tab would
    either do nothing or act on a form nobody is looking at.
    """

    def _panel(self, tab: int):
        panel = object.__new__(RunDiffusionPanel)
        panel._action_bar = FakeContainer()
        panel._current_tab = tab
        panel._cost_label = object()
        panel._num_results = object()
        panel._generate_button = object()
        panel._result_count = 1
        panel._schedule_cost_refresh = lambda: None
        return panel

    def test_another_tab_leaves_the_row_empty(self):
        panel = self._panel(TAB_LIBRARY)

        panel._render_action_bar_now()

        assert panel._action_bar.cleared == 1
        assert panel._action_bar.entered == 0

    def test_another_tab_drops_the_widgets_it_held(self):
        """They belong to a row that no longer exists, and a destroyed widget
        still answers: the cost refresh would write into one nobody can see."""
        panel = self._panel(TAB_LIBRARY)

        panel._render_action_bar_now()

        assert panel._cost_label is None
        assert panel._num_results is None
        assert panel._generate_button is None

    def test_the_create_tab_builds_it(self, monkeypatch):
        panel = self._panel(TAB_CREATE)

        panel._render_action_bar_now()

        assert panel._action_bar.entered == 1
        assert panel._generate_button is not None

    def test_it_is_cleared_before_it_is_rebuilt(self):
        panel = self._panel(TAB_CREATE)

        panel._render_action_bar_now()

        assert panel._action_bar.cleared == 1
