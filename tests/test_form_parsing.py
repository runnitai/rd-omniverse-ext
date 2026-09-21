"""Tests for reading a tool's declared schema.

These cover the parsing half of the form, which is the half that can be wrong
without looking wrong: a select whose options are nested under a presentation
key this plugin does not know reads as "no options" and silently renders a text
box instead of a dropdown.

The widget half needs a running Kit app and is verified by hand.
"""

from __future__ import annotations

import pytest

from rundiffusion_omniverse.api.tools import (
    IMAGE_TYPES,
    ImageSelection,
    ordered_captures,
    MULTI_SELECT_TYPES,
    SINGLE_SELECT_TYPES,
    SLIDER_TYPES,
    ToolDetail,
    ToolField,
    build_inputs,
)
from rundiffusion_omniverse.form import (
    MAX_INPUT_FILES,
    ToolForm,
    options_for,
    size_options_for,
)


class TestOptionParsing:
    """Choices can be nested under any of three presentation keys. The options
    are the same either way, so callers must not have to care which the tool
    author used."""

    def test_reads_options_nested_under_text(self):
        display = {"text": {"options": [{"text": "Fast", "value": "fast"}]}}

        assert options_for(display) == [("Fast", "fast")]

    def test_reads_options_nested_under_radio(self):
        display = {"radio": {"options": [{"text": "A", "value": "a"}]}}

        assert options_for(display) == [("A", "a")]

    def test_reads_options_nested_under_image(self):
        display = {"image": {"options": [{"text": "Sketch", "value": "sketch"}]}}

        assert options_for(display) == [("Sketch", "sketch")]

    def test_no_options_is_empty_rather_than_an_error(self):
        """A select with no declared choices falls back to a text box, which is
        a real case rather than a defect."""
        assert options_for({}) == []
        assert options_for({"text": {}}) == []


class TestSizeOptionParsing:
    def test_reads_declared_output_sizes(self):
        display = {
            "select": {
                "options": [
                    {"text": "Square", "width": 1024, "height": 1024},
                    {"text": "Landscape", "width": 1344, "height": 768},
                ]
            }
        }

        assert size_options_for(display) == [
            ("Square (1024x1024)", 1024, 1024),
            ("Landscape (1344x768)", 1344, 768),
        ]

    def test_labels_fall_back_to_the_dimensions(self):
        display = {"select": {"options": [{"width": 512, "height": 512}]}}

        assert size_options_for(display) == [("512x512", 512, 512)]

    def test_options_without_dimensions_are_skipped(self):
        """A size option that names no size cannot be chosen from, and passing
        it on would put a label in the request where a number belongs."""
        display = {
            "select": {
                "options": [
                    {"text": "Broken"},
                    {"text": "Fine", "width": 768, "height": 768},
                ]
            }
        }

        assert size_options_for(display) == [("Fine (768x768)", 768, 768)]


class TestFieldTypeVocabulary:
    """Spelled as RUNNIT_NODE_FIELD_TYPE spells them. An earlier draft guessed
    and got three wrong, and because an unrecognised type falls through to a
    text box, every one of those mistakes still rendered something."""

    def test_text_area_is_underscored(self):
        from rundiffusion_omniverse.api.tools import TEXT_TYPES

        assert "TEXT_AREA" in TEXT_TYPES
        assert "TEXTAREA" not in TEXT_TYPES

    def test_there_is_no_string_type(self):
        from rundiffusion_omniverse.api.tools import TEXT_TYPES

        assert "STRING" not in TEXT_TYPES

    def test_slider_family_covers_the_four_declared_types(self):
        assert SLIDER_TYPES == {"SLIDER", "UPSCALE_FACTOR_SLIDER", "CFG", "STEPS"}

    def test_model_single_select_is_a_select_not_a_model(self):
        """MODEL_SINGLE_SELECT is a dropdown. The MODEL *type* is what v2
        refuses, and conflating them would hide runnable tools."""
        assert "MODEL_SINGLE_SELECT" in SINGLE_SELECT_TYPES

    def test_image_types_are_the_two_declared_ones(self):
        assert IMAGE_TYPES == {"IMG", "IMGS"}

    def test_multi_select_is_its_own_family(self):
        assert MULTI_SELECT_TYPES == {"MULTI_SELECT"}


class TestFormValuesReachTheRequest:
    def test_collected_values_survive_into_inputs(self):
        tool = ToolDetail(
            id="t",
            name="T",
            tool_fields_hash="v1:abc",
            fields=[
                ToolField(key="prompt", type="PROMPT", label=None, required=True),
                ToolField(key="cfg", type="CFG", label=None, required=False),
                ToolField(key="size", type="WIDTH_HEIGHT", label=None, required=False),
            ],
        )

        inputs = build_inputs(
            tool,
            {"prompt": "a castle", "cfg": 3.5, "size": {"width": 1024, "height": 1024}},
            has_capture=False,
        )

        assert inputs["prompt"] == "a castle"
        assert inputs["cfg"] == 3.5
        assert inputs["size"] == {"width": 1024, "height": 1024}

    def test_the_capture_overrides_whatever_the_form_had_for_the_image_field(self):
        """An image field collects no value of its own, so anything left under
        its key is stale and the image must win."""
        tool = ToolDetail(
            id="t",
            name="T",
            tool_fields_hash="v1:abc",
            fields=[ToolField(key="img", type="IMG", label=None, required=True)],
        )

        inputs = build_inputs(
            tool,
            {"img": "stale"},
            image_selections={"img": [ImageSelection(png=b"PNG")]},
        )

        assert inputs["img"] == {"kind": "MULTIPART", "file_index": 0}


class TestChosenImages:
    """An image field can take the viewport OR something already on the server.
    A chosen asset is a plain reference; only the viewport needs a MULTIPART
    descriptor, because only the viewport's bytes travel with the request."""

    def _image_tool(self) -> ToolDetail:
        return ToolDetail(
            id="t",
            name="T",
            tool_fields_hash="v1:abc",
            fields=[ToolField(key="img", type="IMG", label=None, required=True)],
        )

    def test_a_chosen_upload_replaces_the_capture(self):
        inputs = build_inputs(
            self._image_tool(),
            {},
            image_selections={
                "img": [ImageSelection(reference={"kind": "UPLOAD_REF", "id": "u1"})]
            },
        )

        assert inputs["img"] == {"kind": "UPLOAD_REF", "id": "u1"}

    def test_a_chosen_library_item_uses_the_whole_id(self):
        """v2 takes the compound id back whole. v1 made callers split it on the
        colon, which produced a reference matching nothing when done wrong."""
        inputs = build_inputs(
            self._image_tool(),
            {},
            image_selections={
                "img": [
                    ImageSelection(
                        reference={"kind": "LIBRARY_REF", "id": "run123:result456"}
                    )
                ]
            },
        )

        assert inputs["img"] == {"kind": "LIBRARY_REF", "id": "run123:result456"}

    def test_a_captured_view_is_a_selection_like_any_other(self):
        inputs = build_inputs(
            self._image_tool(),
            {},
            image_selections={"img": [ImageSelection(png=b"PNG")]},
        )

        assert inputs["img"] == {"kind": "MULTIPART", "file_index": 0}


class TestUploadMimes:
    def test_known_image_types_declare_their_mime(self):
        from rundiffusion_omniverse.api.assets import mime_for

        assert mime_for("shot.png") == "image/png"
        assert mime_for("SHOT.JPG") == "image/jpeg"
        assert mime_for("shot.webp") == "image/webp"

    def test_an_unsupported_type_is_refused_before_upload(self):
        """The server allow-lists the part MIME and answers 415, so saying no
        here saves a round trip and a confusing error."""
        from rundiffusion_omniverse.api.assets import mime_for

        assert mime_for("model.usd") is None
        assert mime_for("notes.txt") is None


class TestImageSlots:
    """What each image field will send, which is now a LIST of images.

    A plural field can hold several, mixing viewport captures with library and
    upload references, in an order the tool is not free to ignore. The widgets
    need Kit; the state behind them does not, and the state is what reaches the
    request. `_image_containers` is left empty here, so a redraw updates state
    and draws nothing.
    """

    def _form(self, *keys: str, plural: tuple = ()) -> ToolForm:
        form = ToolForm()
        form.image_field_keys = list(keys)
        form._plural_keys = set(plural)
        return form

    def test_a_field_starts_empty_until_something_is_added(self):
        form = self._form("img")

        assert form.image_selections() == {}

    def test_nothing_is_pre_filled_with_the_viewport(self):
        """A capture is bytes taken at a moment, so there is nothing to assume
        before the user has framed anything."""
        form = self._form("img")

        assert form.image_slots.get("img") in (None, [])

    def test_a_plural_field_holds_several_images_in_order(self):
        """The whole point: a reference field takes more than one image, and a
        tool given several does not treat them interchangeably."""
        form = self._form("imgs", plural=("imgs",))
        form.add_capture("imgs", b"FRONT")
        form.add_chosen("imgs", {"kind": "UPLOAD_REF", "id": "u1"})
        form.add_capture("imgs", b"SIDE")

        entries = form.image_selections()["imgs"]

        assert [entry.is_capture for entry in entries] == [True, False, True]
        assert entries[1].reference == {"kind": "UPLOAD_REF", "id": "u1"}

    def test_two_captures_are_two_different_views(self):
        """Each capture pins its own bytes when it is taken, which is what lets
        the user frame a shot, capture, orbit, and capture something else. They
        used to be one reading of the camera at submit time, so N of them were N
        copies of one frame."""
        form = self._form("imgs", plural=("imgs",))
        form.add_capture("imgs", b"FRONT")
        form.add_capture("imgs", b"SIDE")

        assert [entry.png for entry in form.image_selections()["imgs"]] == [
            b"FRONT",
            b"SIDE",
        ]

    def test_a_singular_field_replaces_rather_than_stacking(self):
        """IMG takes one image. A second choice has to displace the first, not
        queue behind it to be dropped silently at submit time."""
        form = self._form("img")
        form.add_capture("img", b"VIEW")
        form.add_chosen("img", {"kind": "UPLOAD_REF", "id": "u1"})

        entries = form.image_selections()["img"]

        assert len(entries) == 1
        assert entries[0].reference == {"kind": "UPLOAD_REF", "id": "u1"}

    def test_a_plural_field_stops_at_the_server_file_limit(self):
        """Refusing here is kinder than a rejection after the bytes have gone up."""
        form = self._form("imgs", plural=("imgs",))
        for _ in range(MAX_INPUT_FILES + 5):
            form.add_capture("imgs", b"VIEW")

        assert len(form.image_selections()["imgs"]) == MAX_INPUT_FILES

    def test_the_limit_is_counted_across_the_WHOLE_form(self):
        """The server takes 12 input files per REQUEST, counting multipart
        parts and references together. Enforced per field, a tool with a start
        frame and an end frame accepted 24, uploaded them, and got a 413: the
        exact outcome the check exists to prevent."""
        form = self._form("first", "second", plural=("first", "second"))
        for _ in range(MAX_INPUT_FILES):
            form.add_capture("first", b"VIEW")

        assert form.has_room("second") is False
        form.add_capture("second", b"VIEW")
        assert form.image_selections().get("second") in (None, [])

    def test_a_singular_field_can_still_be_corrected_at_the_limit(self):
        """Swapping the one image a singular field holds carries the same
        number of files. Refusing it would leave a field that cannot be fixed,
        only emptied."""
        form = self._form("img", "imgs", plural=("imgs",))
        form.add_capture("img", b"FIRST")
        for _ in range(MAX_INPUT_FILES - 1):
            form.add_capture("imgs", b"VIEW")

        assert form.total_images() == MAX_INPUT_FILES
        assert form.has_room("img") is True

        form.add_chosen("img", {"kind": "UPLOAD_REF", "id": "u1"})

        assert form.total_images() == MAX_INPUT_FILES
        assert form.image_selections()["img"][0].reference["id"] == "u1"

    def test_an_empty_singular_field_at_the_limit_is_refused(self):
        """It would ADD a file rather than swap one, so the same limit applies
        to it as to a plural field."""
        form = self._form("img", "imgs", plural=("imgs",))
        for _ in range(MAX_INPUT_FILES):
            form.add_capture("imgs", b"VIEW")

        assert form.has_room("img") is False

    def test_removing_one_image_leaves_the_others_alone(self):
        form = self._form("imgs", plural=("imgs",))
        form.add_chosen("imgs", {"kind": "UPLOAD_REF", "id": "first"})
        middle = form.add_chosen("imgs", {"kind": "UPLOAD_REF", "id": "second"})
        form.add_chosen("imgs", {"kind": "UPLOAD_REF", "id": "third"})

        form.remove_slot("imgs", middle)

        assert [entry.reference["id"] for entry in form.image_selections()["imgs"]] == [
            "first",
            "third",
        ]

    def test_slot_ids_are_not_positions(self):
        """The panel keys each preview by slot id. If ids were positions,
        removing an early image would leave every later preview showing its
        neighbour's picture."""
        form = self._form("imgs", plural=("imgs",))
        first = form.add_chosen("imgs", {"kind": "UPLOAD_REF", "id": "first"})
        second = form.add_chosen("imgs", {"kind": "UPLOAD_REF", "id": "second"})

        form.remove_slot("imgs", first)

        assert form.image_slots["imgs"][0].id == second

    def test_clearing_a_field_sends_nothing_for_it(self):
        form = self._form("imgs", plural=("imgs",))
        form.add_capture("imgs", b"FRONT")
        form.add_capture("imgs", b"SIDE")
        form.clear_image("imgs")

        assert form.image_selections() == {}

    def test_captures_are_counted_across_every_field(self):
        form = self._form("img", "imgs", plural=("imgs",))
        form.add_capture("img", b"VIEW")
        form.add_capture("imgs", b"VIEW")
        form.add_chosen("imgs", {"kind": "UPLOAD_REF", "id": "u1"})
        form.add_capture("imgs", b"VIEW")

        captures = [
            entry
            for entries in form.image_selections().values()
            for entry in entries
            if entry.is_capture
        ]

        assert len(captures) == 3

    def test_image_choices_survive_a_tab_rebuild(self):
        """Switching tabs destroys every control, and an image choice does not
        live in one, so it has to be carried out and put back by hand."""
        form = self._form("img", "imgs", plural=("imgs",))
        form.add_capture("img", b"VIEW")
        form.add_chosen("imgs", {"kind": "LIBRARY_REF", "id": "run:res"})
        form.add_capture("imgs", b"VIEW")
        state = form.export_image_state()

        rebuilt = self._form("img", "imgs", plural=("imgs",))
        rebuilt.restore_image_state(state)

        assert [entry.png for entry in rebuilt.image_selections()["img"]] == [b"VIEW"]
        assert rebuilt.image_selections()["imgs"][0].reference == {
            "kind": "LIBRARY_REF",
            "id": "run:res",
        }

    def test_a_restored_slot_cannot_collide_with_a_new_one(self):
        """Ids come from one counter. A restore that did not advance it would
        hand a new slot an id already on screen, and two previews would fight
        over one asset."""
        form = self._form("imgs", plural=("imgs",))
        form.add_chosen("imgs", {"kind": "UPLOAD_REF", "id": "u1"})
        form.add_chosen("imgs", {"kind": "UPLOAD_REF", "id": "u2"})
        state = form.export_image_state()

        rebuilt = self._form("imgs", plural=("imgs",))
        rebuilt.restore_image_state(state)
        rebuilt.add_capture("imgs", b"VIEW")

        ids = [slot.id for slot in rebuilt.image_slots["imgs"]]
        new_id = ids[-1]

        assert len(set(ids)) == len(ids)
        assert new_id == max(ids)

    def test_a_choice_for_a_field_the_next_tool_lacks_is_dropped(self):
        """Same rule the typed values follow: it carries over only where the
        fieldKey exists in both schemas."""
        form = self._form("img", "gone")
        form.add_capture("img", b"VIEW")
        form.add_chosen("gone", {"kind": "UPLOAD_REF", "id": "u1"})
        state = form.export_image_state()

        other_tool = self._form("img")
        other_tool.restore_image_state(state)

        assert set(other_tool.image_selections()) == {"img"}

    def test_the_selections_reach_the_request_in_order(self):
        """The two halves the panel hands to build_inputs, checked as one."""
        form = self._form("imgs", plural=("imgs",))
        form.add_capture("imgs", b"FRONT")
        form.add_chosen("imgs", {"kind": "UPLOAD_REF", "id": "u1"})
        form.add_capture("imgs", b"SIDE")

        inputs = build_inputs(
            ToolDetail(
                id="t",
                name="T",
                tool_fields_hash="v1:abc",
                fields=[ToolField(key="imgs", type="IMGS", label=None, required=True)],
            ),
            {},
            image_selections=form.image_selections(),
        )

        assert inputs["imgs"] == [
            {"kind": "MULTIPART", "file_index": 0},
            {"kind": "UPLOAD_REF", "id": "u1"},
            {"kind": "MULTIPART", "file_index": 1},
        ]


class TestImageOrder:
    """Moving an image along the send order.

    A tool handed several reference images does not treat them
    interchangeably, so the order IS an input. Reordering by move-left and
    move-right rather than by drag, because omni.ui has no drag-and-drop and a
    pair of buttons in the row header costs no vertical space.
    """

    def _form(self, *ids: str) -> ToolForm:
        form = ToolForm()
        form.image_field_keys = ["imgs"]
        form._plural_keys = {"imgs"}
        for marker in ids:
            form.add_capture("imgs", marker.encode())
        return form

    def _order(self, form: ToolForm) -> list:
        return [entry.png for entry in form.image_selections()["imgs"]]

    def test_moving_left_swaps_with_the_one_before(self):
        form = self._form("a", "b", "c")
        second = form.image_slots["imgs"][1].id

        form._move_slot("imgs", second, -1)

        assert self._order(form) == [b"b", b"a", b"c"]

    def test_moving_right_swaps_with_the_one_after(self):
        form = self._form("a", "b", "c")
        second = form.image_slots["imgs"][1].id

        form._move_slot("imgs", second, 1)

        assert self._order(form) == [b"a", b"c", b"b"]

    def test_the_ends_hold(self):
        """The buttons are disabled there, but the state must not depend on the
        widget being right about it."""
        form = self._form("a", "b")
        first, last = (slot.id for slot in form.image_slots["imgs"])

        form._move_slot("imgs", first, -1)
        form._move_slot("imgs", last, 1)

        assert self._order(form) == [b"a", b"b"]

    def test_reordering_changes_what_the_request_carries(self):
        """The point of the feature, checked end to end rather than on the list:
        file_index follows the row, so moving an image moves its bytes."""
        form = self._form("front", "side")
        second = form.image_slots["imgs"][1].id
        form._move_slot("imgs", second, -1)

        tool = ToolDetail(
            id="t",
            name="T",
            tool_fields_hash="v1:abc",
            fields=[ToolField(key="imgs", type="IMGS", label=None, required=True)],
        )
        inputs = build_inputs(tool, {}, image_selections=form.image_selections())

        assert inputs["imgs"] == [
            {"kind": "MULTIPART", "file_index": 0},
            {"kind": "MULTIPART", "file_index": 1},
        ]
        assert ordered_captures(tool, form.image_selections()) == [b"side", b"front"]

    def test_selecting_a_slot_twice_deselects_it(self):
        """The row header is the controls only while something is selected, so
        there has to be a way back to the count."""
        form = self._form("a")
        slot_id = form.image_slots["imgs"][0].id

        form._select_slot("imgs", slot_id)
        assert form._selected_slot["imgs"] == slot_id

        form._select_slot("imgs", slot_id)
        assert "imgs" not in form._selected_slot


class FakeValueModel:
    def __init__(self, value=0) -> None:
        self._value = value

    def set_value(self, value) -> None:
        self._value = value

    def get_value_as_bool(self) -> bool:
        return bool(self._value)

    def add_value_changed_fn(self, _fn) -> None:
        return None

    @property
    def as_int(self) -> int:
        return int(self._value)


class FakeComboModel:
    def __init__(self, index: int) -> None:
        self._item = FakeValueModel(index)

    def get_item_value_model(self, *_args):
        return self._item

    def add_item_changed_fn(self, _fn) -> None:
        return None


class FakeCombo:
    def __init__(self, index=0, *_labels, **_kwargs) -> None:
        self.model = FakeComboModel(index)


class FakeCheckBox:
    def __init__(self, *_args, **_kwargs) -> None:
        self.model = FakeValueModel(False)


class FakeStack:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


@pytest.fixture
def form_with_controls(monkeypatch):
    """A form holding one of each control whose value used to be lost."""
    import omni.ui as ui

    monkeypatch.setattr(ui, "Label", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ui, "ComboBox", FakeCombo, raising=False)
    monkeypatch.setattr(ui, "CheckBox", FakeCheckBox, raising=False)
    monkeypatch.setattr(ui, "HStack", FakeStack, raising=False)

    form = ToolForm()
    form._controls = []
    form._build_single_select(
        "model",
        "Model",
        {"text": {"options": [{"text": "A", "value": "a"}, {"text": "B", "value": "b"}]}},
        "a",
    )
    form._build_multi_select(
        "styles",
        "Styles",
        {"text": {"options": [{"text": "X", "value": "x"}, {"text": "Y", "value": "y"}]}},
        [],
    )
    form._build_width_height(
        "size",
        "Size",
        {
            "select": {
                "options": [
                    {"text": "Square", "width": 512, "height": 512},
                    {"text": "Wide", "width": 1024, "height": 576},
                ]
            }
        },
        {"width": 512, "height": 512},
    )
    return form, None


class TestValuesSurviveARebuild:
    """The Create tab is rebuilt on every visit, which destroys every control,
    so what the user chose has to be carried out and put back by hand. Only the
    text, boolean and numeric controls registered a `restore`, so the DROPDOWNS
    silently reverted to the tool's default on a trip to the Library, and
    "Reuse its inputs" restored none of them either.

    Silent is the problem. A reverted checkbox is visible; a reverted model
    picker on a tool with a dozen fields is a run spent on the wrong model.
    """

    def _rebuilt(self, form, values):
        """What a tab switch does: collect, throw the widgets away, put back."""
        form._restore(values)
        return form.collect()

    def test_a_single_select_keeps_its_choice(self, form_with_controls):
        form, _ = form_with_controls

        assert self._rebuilt(form, {"model": "b"})["model"] == "b"

    def test_a_choice_the_tool_no_longer_offers_is_left_alone(self, form_with_controls):
        """Carried over from another schema. Forcing it to position 0 would
        turn "we could not keep this" into "you chose the first one"."""
        form, _ = form_with_controls

        assert self._rebuilt(form, {"model": "gone"})["model"] == "a"

    def test_a_multi_select_keeps_every_box(self, form_with_controls):
        form, _ = form_with_controls

        assert self._rebuilt(form, {"styles": ["y"]})["styles"] == ["y"]

    def test_a_multi_select_restored_to_nothing_clears_every_box(
        self, form_with_controls
    ):
        """Restoring an empty selection has to UNCHECK, not leave what was
        there. It then drops out of `collect` entirely, which is how an empty
        control lets the tool fall back to its own default rather than being
        handed an empty list."""
        form, _ = form_with_controls
        form._restore({"styles": ["x", "y"]})

        assert "styles" not in self._rebuilt(form, {"styles": []})

    def test_a_size_keeps_the_pair_it_was_set_to(self, form_with_controls):
        form, _ = form_with_controls

        restored = self._rebuilt(form, {"size": {"width": 1024, "height": 576}})

        assert restored["size"] == {"width": 1024, "height": 576}

    def test_a_size_that_matches_no_option_is_left_alone(self, form_with_controls):
        form, _ = form_with_controls

        restored = self._rebuilt(form, {"size": {"width": 99, "height": 99}})

        assert restored["size"] == {"width": 512, "height": 512}


class TestARefusedImageIsNotRecorded:
    """`add_chosen` and `add_capture` returned a slot id unconditionally, so an
    image refused at the request limit was still reported as added. The panel
    keys its preview by that id, so it recorded an asset against a slot that
    does not exist: an entry nothing ever draws, reads or clears, and no sign
    to the user that the pick did not take.
    """

    def _form(self, *keys: str, plural: tuple = ()) -> ToolForm:
        form = ToolForm()
        form.image_field_keys = list(keys)
        form._plural_keys = set(plural)
        return form

    def _fill(self, form) -> None:
        for _ in range(MAX_INPUT_FILES):
            form.add_capture("imgs", b"VIEW")

    def test_an_accepted_image_reports_its_slot(self):
        form = self._form("imgs", plural=("imgs",))

        assert form.add_capture("imgs", b"VIEW") is not None

    def test_a_refused_capture_reports_nothing(self):
        form = self._form("imgs", plural=("imgs",))
        self._fill(form)

        assert form.add_capture("imgs", b"VIEW") is None

    def test_a_refused_choice_reports_nothing(self):
        form = self._form("imgs", plural=("imgs",))
        self._fill(form)

        assert form.add_chosen("imgs", {"kind": "UPLOAD_REF", "id": "u1"}) is None

    def test_a_swap_into_a_full_form_still_reports_its_slot(self):
        """A singular field that already holds one carries the same number of
        files either way, so it is not refused and must not report as though it
        were."""
        form = self._form("img", "imgs", plural=("imgs",))
        form.add_capture("img", b"FIRST")
        for _ in range(MAX_INPUT_FILES - 1):
            form.add_capture("imgs", b"VIEW")

        assert form.add_chosen("img", {"kind": "UPLOAD_REF", "id": "u1"}) is not None
