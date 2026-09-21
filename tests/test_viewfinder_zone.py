"""The viewfinder at the top of the Create tab, and the rules it lives by.

The frame is the widest thing on the tab and folds to a 40px strip once it has
done its job. What decides which of those it is, how tall it is, and which
field it captures into is plain arithmetic and state, so it is pinned here.
Whether it LOOKS right needs Kit, and is in the manual test script.
"""

from __future__ import annotations

import pytest

from rundiffusion_omniverse import prefs
from rundiffusion_omniverse.form import (
    STRIP_HEIGHT,
    VIEWFINDER_MIN_HEIGHT,
    VIEWFINDER_TARGET_HEIGHT,
    ImageSlot,
    ToolForm,
    aspect_label,
    summary_part,
    viewfinder_height,
)


class TestHowTallTheFrameIs:
    def test_a_panel_docked_at_the_usual_width_gets_the_target(self):
        assert viewfinder_height(380) == VIEWFINDER_TARGET_HEIGHT

    def test_it_follows_the_width_at_sixteen_by_nine_below_that(self):
        assert viewfinder_height(320) == 180

    def test_it_never_grows_past_the_target(self):
        """A panel pulled wide must not hand the whole tab to the frame."""
        assert viewfinder_height(1200) == VIEWFINDER_TARGET_HEIGHT

    def test_too_narrow_for_a_useful_frame_is_the_strip(self):
        assert viewfinder_height(280) is None

    def test_a_panel_not_laid_out_yet_is_treated_as_the_usual_width(self):
        """The first frame of every build has no width to report. Flashing the
        strip and then growing would be worse than being right a frame late."""
        assert viewfinder_height(0) == VIEWFINDER_TARGET_HEIGHT
        assert viewfinder_height(None) == VIEWFINDER_TARGET_HEIGHT

    def test_the_minimum_is_the_floor(self):
        assert viewfinder_height(300) >= VIEWFINDER_MIN_HEIGHT


class TestNamingTheShape:
    @pytest.mark.parametrize(
        "aspect,name",
        [(16 / 9, "16:9"), (1.0, "1:1"), (4 / 3, "4:3"), (9 / 16, "9:16"), (1918 / 1080, "16:9")],
    )
    def test_common_shapes_are_named(self, aspect, name):
        assert aspect_label(aspect) == name

    def test_anything_else_takes_the_nearest_standard_name(self):
        """A docked viewport is never exactly a ratio, and "1.71:1" in the
        corner of the frame read as arithmetic. Reported from Base Editor."""
        assert aspect_label(1.71) == "16:9"
        assert aspect_label(2.39) == "21:9"
        assert aspect_label(1.6) == "16:10"
        assert aspect_label(0.8) == "4:5"

    def test_no_shape_says_nothing(self):
        assert aspect_label(None) == ""
        assert aspect_label(0) == ""


class TestTheFoldedSummary:
    def test_steps_keep_the_word_that_makes_the_number_mean_something(self):
        assert summary_part("STEPS", "Steps", "28") == "28 steps"

    def test_a_seed_says_it_is_a_seed(self):
        assert summary_part("SEED", "Seed", "random") == "seed random"

    def test_a_switch_keeps_the_first_word_of_its_label(self):
        assert summary_part("BOOLEAN", "Expand prompt", "off") == "expand off"

    def test_a_value_that_says_what_it_is_stands_alone(self):
        assert summary_part("SELECT", "Sampler", "regular") == "regular"

    def test_nothing_set_contributes_nothing(self):
        assert summary_part("STEPS", "Steps", "") == ""


def _form(*keys: str) -> ToolForm:
    form = ToolForm()
    form.image_field_keys = list(keys)
    form.image_field_labels = {key: key.title() for key in keys}
    form._plural_keys = set(keys)
    form._capture_target = keys[0] if keys else None
    for key in keys:
        form.image_slots[key] = []
    # No container, so nothing is drawn: every test here is about state.
    return form


class TestWhenTheFrameFolds:
    def test_an_empty_field_shows_the_full_frame(self):
        assert _form("image").capture_zone_layout(380) == (False, VIEWFINDER_TARGET_HEIGHT)

    def test_a_capture_folds_it_to_the_strip(self):
        form = _form("image")
        form.image_slots["image"] = [ImageSlot(1, png=b"a")]

        assert form.capture_zone_layout(380) == (True, STRIP_HEIGHT)

    def test_a_chosen_library_image_does_not(self):
        """The frame folds once it has done ITS job, which is capturing. An
        image from the library says nothing about whether the view is framed."""
        form = _form("image")
        form.image_slots["image"] = [ImageSlot(1, reference="lib:1")]

        assert form.capture_zone_layout(380) == (False, VIEWFINDER_TARGET_HEIGHT)

    def test_show_viewfinder_puts_it_back_for_the_rest_of_the_session(self):
        form = _form("image")
        form.image_slots["image"] = [ImageSlot(1, png=b"a")]

        form.show_viewfinder("image")
        form.image_slots["image"].append(ImageSlot(2, png=b"b"))

        assert form.capture_zone_layout(380) == (False, VIEWFINDER_TARGET_HEIGHT)

    def test_asking_for_it_in_a_narrow_panel_gets_the_smallest_frame(self):
        form = _form("image")
        form.show_viewfinder("image")

        assert form.capture_zone_layout(260) == (False, VIEWFINDER_MIN_HEIGHT)

    def test_a_narrow_panel_starts_on_the_strip(self):
        assert _form("image").capture_zone_layout(260) == (True, STRIP_HEIGHT)

    def test_only_a_folded_frame_that_changes_shape_is_redrawn(self):
        """A dock edge being dragged fires size changes on every frame, and
        rebuilding a dropdown under the pointer for each one is a flicker."""
        drawn: list = []
        form = _form("image")
        form._capture_container = object()
        form._render_capture_zone = lambda: drawn.append("zone")
        form._capture_layout = form.capture_zone_layout(400)

        form.on_width_changed(420)
        assert drawn == []

        form.on_width_changed(260)
        assert drawn == ["zone"]


class TestWhatIsLive:
    def test_an_open_frame_on_the_active_viewport_is_live(self):
        form = _form("image")
        form._viewfinders = {"image": object()}

        assert form.viewfinder_is_live()

    def test_a_frame_on_a_named_camera_is_a_still(self):
        form = _form("image")
        form._viewfinders = {"image": object()}
        form._capture_source["image"] = "/World/Cameras/Lobby"

        assert not form.viewfinder_is_live()

    def test_a_folded_frame_is_not(self):
        """No frame was registered, because the strip draws a pinned capture."""
        assert not _form("image").viewfinder_is_live()

    def test_letting_go_of_the_zone_stops_it(self):
        """A tab switch destroys the frame. A live readback into it would be
        feeding a widget nobody can see."""
        form = _form("image")
        form._viewfinders = {"image": object()}
        form._capture_container = object()

        form.release_capture_zone()

        assert not form.viewfinder_is_live()
        assert form._capture_container is None


class TestWhichFieldTheFrameCapturesInto:
    def test_the_first_image_field_by_default(self):
        assert _form("start", "end").capture_target == "start"

    def test_it_can_be_pointed_at_another(self):
        form = _form("start", "end")

        form.set_capture_target("end")

        assert form.capture_target == "end"

    def test_a_field_this_tool_does_not_have_is_refused(self):
        form = _form("start", "end")

        form.set_capture_target("mask")

        assert form.capture_target == "start"

    def test_the_capture_button_hands_over_that_field(self):
        asked: list = []
        form = _form("start", "end")
        form._on_capture = lambda key, source: asked.append((key, source))
        form.set_capture_target("end")

        form._capture_into(form.capture_target)

        assert asked == [("end", None)]


class TestRecentTools:
    @pytest.fixture(autouse=True)
    def clean_store(self, monkeypatch):
        monkeypatch.setattr(prefs, "_FALLBACK", {})

    def test_nothing_picked_yet_is_nothing_recent(self):
        assert prefs.recent_tool_ids() == []

    def test_the_newest_pick_comes_first_and_the_list_is_short(self):
        for tool_id in ("a", "b", "c"):
            prefs.remember_tool(tool_id)

        assert prefs.recent_tool_ids() == ["c", "b"]

    def test_picking_one_again_moves_it_rather_than_repeating_it(self):
        prefs.remember_tool("a")
        prefs.remember_tool("b")
        prefs.remember_tool("a")

        assert prefs.recent_tool_ids() == ["a", "b"]


class TestHidingTheFrameAgain:
    """Reported from Base Editor: once Show viewfinder had been pressed the
    frame stayed open for the session, with nothing on screen to fold it."""

    def test_hide_folds_it_back_to_the_strip(self):
        form = _form("image")
        form.image_slots["image"] = [ImageSlot(1, png=b"a")]
        form.show_viewfinder("image")

        form.hide_viewfinder("image")

        assert form.capture_zone_layout(380) == (True, STRIP_HEIGHT)

    def test_hide_is_offered_only_where_it_would_fold_something(self):
        form = _form("image")
        form.show_viewfinder("image")

        assert not form._would_fold("image")

        form.image_slots["image"] = [ImageSlot(1, png=b"a")]

        assert form._would_fold("image")


class TestChoosingACameraIsNotACapture:
    def test_a_click_on_the_frame_right_after_choosing_a_camera_is_ignored(self):
        """The dropdown list opens over the frame, and the click that picks a
        row must not also take a photograph. Reported from Base Editor."""
        import time

        asked: list = []
        form = _form("image")
        form._on_capture = lambda key, source: asked.append(key)
        form._source_changed_at = time.monotonic()

        form._frame_clicked("image")

        assert asked == []

    def test_a_click_on_the_frame_otherwise_captures(self):
        asked: list = []
        form = _form("image")
        form._on_capture = lambda key, source: asked.append(key)

        form._frame_clicked("image")

        assert asked == ["image"]


class _Combo:
    def __init__(self, index: int) -> None:
        self.model = self
        self.as_int = index

    def get_item_value_model(self):
        return self


SQUARE_AND_WIDE = [("Square", 1024, 1024), ("Wide", 1344, 768)]


class TestTheChosenSizeShapesTheCapture:
    """Reported from Base Editor: changing the output size did not change the
    shape of what was captured, so a square run was made from a wide view."""

    def _form(self, index: int = 0) -> ToolForm:
        form = _form("image")
        form._size_fields = {"size": (SQUARE_AND_WIDE, _Combo(index))}
        return form

    def test_until_a_size_is_picked_the_viewport_decides(self):
        assert self._form().capture_aspect() is None

    def test_a_size_picked_by_hand_is_the_shape_of_the_capture(self):
        form = self._form(index=0)
        form._on_size_chosen("size")

        assert form.capture_aspect() == 1.0

    def test_a_size_a_capture_matched_is_not(self):
        form = self._form(index=1)
        form._size_inherited.add("size")

        assert form.capture_aspect() is None

    def test_putting_back_a_carried_over_value_is_not_picking_one(self):
        """A tab switch restores every value, which moves the dropdown. That
        used to mark the size as chosen by hand on every visit to Library."""
        form = self._form()
        restored: list = []

        class Control:
            key = "size"

            def restore(self, _value):
                form._on_size_chosen("size")
                restored.append(True)

        form._controls = [Control()]
        form._restore({"size": {"width": 1024, "height": 1024}})

        assert restored == [True]
        assert form.capture_aspect() is None
