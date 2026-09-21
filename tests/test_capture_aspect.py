"""Taking the shape of the run from the view that was captured.

Someone frames a wide view, captures it, generates, and gets a square image of
a squashed building. Then they change the size dropdown and generate again.
The panel had everything it needed to skip that round trip: the capture came
back at the viewport's own resolution, and the tool had already published the
shapes it can produce.

Two halves, both tested here. Deciding what shape was framed, and choosing
between the sizes a tool declares, which is a question about ratios rather than
pixels because a provider rejects dimensions off its own grid.

The first half asks the VIEWPORT before it looks at the captured bytes.
Reported from Base Editor: with Fill Viewport off and the render resolution set
to 1024x1024, the buffer that came back was 1920x1080, so the run took the shape
of the window rather than the shape the user had set. `resolution` is the number
they typed while Fill Viewport is off and the canvas size while it is on, which
is why one property answers both halves of the original request. The captured
bytes are the fallback for a host that will not say.

The third thing is a rule about whose choice wins. A size the user set by hand
is never moved by a later capture: they picked it while looking at the same
viewport, so it is a decision rather than a stale value.
"""

from __future__ import annotations

import struct

import omni.ui as ui
import pytest

from rundiffusion_omniverse import viewport_capture
from rundiffusion_omniverse.form import (
    SIZE_FOLLOWS_VIEW_TEXT,
    ToolForm,
    size_matching_aspect,
)
from rundiffusion_omniverse.viewport_capture import captured_aspect, png_aspect, png_size

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png(width: int, height: int) -> bytes:
    """A PNG's first 24 bytes, which is all the dimensions need."""
    return (
        PNG_SIGNATURE
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


class TestReadingTheShapeOffTheCapture:
    def test_the_dimensions_come_out_of_the_header(self):
        assert png_size(_png(1920, 1080)) == (1920, 1080)

    def test_the_aspect_is_width_over_height(self):
        assert png_aspect(_png(1600, 800)) == pytest.approx(2.0)

    def test_a_tall_viewport_reads_as_a_tall_picture(self):
        assert png_aspect(_png(600, 900)) == pytest.approx(2 / 3)

    def test_something_that_is_not_a_png_has_no_opinion(self):
        # None means "no opinion about the size", not "the capture failed".
        # The caller leaves the tool's own default alone.
        assert png_size(b"not a png at all, but long enough to look at") is None

    def test_a_truncated_png_has_no_opinion_either(self):
        assert png_size(PNG_SIGNATURE + b"\x00\x00") is None

    def test_a_header_claiming_no_pixels_is_refused(self):
        # A zero would divide by zero one call later.
        assert png_size(_png(0, 0)) is None
        assert png_aspect(_png(1920, 0)) is None


class TestWhichAnswerIsBelieved:
    """The viewport first, the pixels second."""

    def test_the_viewport_s_own_resolution_wins(self, monkeypatch):
        """A 1024x1024 render sitting letterboxed in a 1920x1080 window is a
        square picture. Taking the shape off the buffer made it 16:9 and the
        user got a wide render of a square view."""
        monkeypatch.setattr(viewport_capture, "viewport_aspect", lambda: 1.0)

        assert captured_aspect(_png(1920, 1080)) == pytest.approx(1.0)

    def test_the_captured_pixels_answer_when_the_viewport_will_not(
        self, monkeypatch
    ):
        """A host with no viewport module, or one mid-teardown. A worse answer
        than the viewport's and a much better one than nothing."""
        monkeypatch.setattr(viewport_capture, "viewport_aspect", lambda: None)

        assert captured_aspect(_png(1600, 900)) == pytest.approx(16 / 9)

    def test_neither_answering_is_no_opinion(self, monkeypatch):
        monkeypatch.setattr(viewport_capture, "viewport_aspect", lambda: None)

        assert captured_aspect(b"not a png") is None

    def test_a_viewport_reporting_nonsense_is_not_believed(self, monkeypatch):
        """Zero would divide by zero one call later, and a viewport being torn
        down reports one."""
        monkeypatch.setattr(viewport_capture, "viewport_aspect", lambda: 0)

        assert captured_aspect(_png(1600, 900)) == pytest.approx(16 / 9)


class TestChoosingBetweenTheSizesAToolDeclares:
    WIDE = ("Landscape (1344x768)", 1344, 768)
    SQUARE = ("Square (1024x1024)", 1024, 1024)
    TALL = ("Portrait (768x1344)", 768, 1344)

    def test_a_wide_view_takes_the_wide_size(self):
        sizes = [self.SQUARE, self.WIDE, self.TALL]

        assert size_matching_aspect(sizes, 16 / 9) == 1

    def test_a_tall_view_takes_the_tall_size(self):
        sizes = [self.SQUARE, self.WIDE, self.TALL]

        assert size_matching_aspect(sizes, 9 / 16) == 2

    def test_the_measure_is_symmetric(self):
        """4:3 must be exactly as far from 3:4 as 3:4 is from 4:3. Comparing
        ratios by subtraction is not, and would lean landscape on a tall
        viewport."""
        sizes = [("4:3", 4, 3), ("3:4", 3, 4)]

        assert size_matching_aspect(sizes, 4 / 3) == 0
        assert size_matching_aspect(sizes, 3 / 4) == 1

    def test_the_nearest_shape_wins_even_when_nothing_matches(self):
        """A tool with only square and portrait sizes still has a best answer
        for a wide view, and it is not the portrait one."""
        sizes = [self.TALL, self.SQUARE]

        assert size_matching_aspect(sizes, 16 / 9) == 1

    def test_the_larger_of_two_equal_shapes_wins(self):
        """Someone who framed a view wants it back at the best the tool
        offers, and would have to notice that the match took the small one."""
        sizes = [("Small", 512, 512), ("Large", 1024, 1024)]

        assert size_matching_aspect(sizes, 1.0) == 1

    def test_a_size_with_no_pixels_is_never_chosen(self):
        sizes = [("Broken", 0, 0), ("Square", 1024, 1024)]

        assert size_matching_aspect(sizes, 1.0) == 1

    def test_nothing_to_choose_from_is_no_choice(self):
        assert size_matching_aspect([], 1.5) is None

    def test_nothing_to_match_against_is_no_choice(self):
        assert size_matching_aspect([("Square", 8, 8)], None) is None
        assert size_matching_aspect([("Square", 8, 8)], 0) is None


class FakeLabel:
    def __init__(self, text: str = "", **_kwargs) -> None:
        self.text = text


class FakeComboModel:
    def __init__(self, index: int) -> None:
        self._value = _FakeInt(index)
        self.item_changed: list = []

    def get_item_value_model(self):
        return self._value

    def add_item_changed_fn(self, fn) -> None:
        self.item_changed.append(fn)


class _FakeInt:
    def __init__(self, value: int) -> None:
        self.as_int = value

    def set_value(self, value: int) -> None:
        self.as_int = value


class FakeCombo:
    def __init__(self, index: int = 0, *labels, **_kwargs) -> None:
        self.labels = labels
        self.model = FakeComboModel(index)

    def choose(self, index: int) -> None:
        """What a user doing it themselves looks like from here."""
        self.model.get_item_value_model().set_value(index)
        for fn in self.model.item_changed:
            fn(self.model, None)


class FakeStack:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


@pytest.fixture(autouse=True)
def drawable_ui(monkeypatch):
    monkeypatch.setattr(ui, "Label", FakeLabel, raising=False)
    monkeypatch.setattr(ui, "ComboBox", FakeCombo, raising=False)
    monkeypatch.setattr(ui, "HStack", FakeStack, raising=False)


SIZES = {
    "select": {
        "options": [
            {"text": "Square", "width": 1024, "height": 1024},
            {"text": "Landscape", "width": 1344, "height": 768},
            {"text": "Portrait", "width": 768, "height": 1344},
        ]
    }
}


def _form_with_a_size_field() -> ToolForm:
    form = ToolForm()
    form._build_width_height("size", "Output size", SIZES, None)
    return form


def _chosen(form: ToolForm) -> dict:
    return next(control.value() for control in form._controls if control.key == "size")


class TestPointingTheFormAtTheShapeThatWasCaptured:
    def test_the_size_follows_the_view(self):
        form = _form_with_a_size_field()

        form.match_capture_aspect(1344 / 768)

        assert _chosen(form) == {"width": 1344, "height": 768}

    def test_it_says_so_where_it_happened(self):
        """A control that moved on its own, in silence, is a control the user
        cannot trust. The note sits beside the heading, and the dropdown under
        it already shows the size it moved to, so the note says why."""
        form = _form_with_a_size_field()

        form.match_capture_aspect(768 / 1344)

        assert form._size_notes["size"].text == SIZE_FOLLOWS_VIEW_TEXT

    def test_a_size_the_user_chose_is_left_alone(self):
        """They picked it while looking at the same viewport, so a later
        capture putting it back would be the panel arguing with them."""
        form = _form_with_a_size_field()
        _sizes, widget = form._size_fields["size"]
        widget.choose(2)

        form.match_capture_aspect(1344 / 768)

        assert _chosen(form) == {"width": 768, "height": 1344}

    def test_and_the_note_goes_away_when_they_take_over(self):
        form = _form_with_a_size_field()
        form.match_capture_aspect(1344 / 768)
        _sizes, widget = form._size_fields["size"]

        widget.choose(0)

        assert form._size_notes["size"].text == ""

    def test_a_capture_with_no_readable_shape_changes_nothing(self):
        form = _form_with_a_size_field()

        form.match_capture_aspect(None)

        assert _chosen(form) == {"width": 1024, "height": 1024}

    def test_a_tool_with_no_output_size_is_untouched_rather_than_broken(self):
        """Most tools declare one. A tool that does not must not make a
        capture fail."""
        form = ToolForm()

        form.match_capture_aspect(16 / 9)

        assert form._size_fields == {}

    def test_every_output_size_on_the_form_follows(self):
        """A tool with two of them means both, and neither is more the shape
        of the captured view than the other."""
        form = _form_with_a_size_field()
        form._build_width_height("second_size", "Second output size", SIZES, None)

        form.match_capture_aspect(1344 / 768)

        chosen = {
            control.key: control.value()
            for control in form._controls
            if control.key.endswith("size")
        }
        assert chosen["size"] == {"width": 1344, "height": 768}
        assert chosen["second_size"] == {"width": 1344, "height": 768}


def test_the_shape_travels_from_the_capture_to_the_form(monkeypatch):
    """The two halves joined up, which is the journey the panel makes: a shape
    decided at the moment of capture, and a dropdown moved to match it."""
    monkeypatch.setattr(viewport_capture, "viewport_aspect", lambda: None)
    form = _form_with_a_size_field()

    form.match_capture_aspect(captured_aspect(_png(1920, 1080)))

    assert _chosen(form) == {"width": 1344, "height": 768}
