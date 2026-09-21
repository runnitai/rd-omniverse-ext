"""Saying why a capture did not happen, in the frame that was captured.

Reported from Base Editor after a capture was correctly refused: the refusal
was only in the status line at the very bottom of the panel, under BILLED TO,
which is a long way from the Capture button that had just been pressed. The
message was right and nobody was looking at it.

So the viewfinder says it, in red, in the band along the bottom of the frame,
or on the strip's line once the frame has folded away. Only there: the status
line is off by default and a panel's height from where the person is looking.

The same place reports a capture IN PROGRESS, not in red. Every capture
settles for sixty frames before it reads anything, and on a heavy path-traced
stage that is seconds in which the panel used to say nothing at all.
"""

from __future__ import annotations

import omni.ui as ui
import pytest

from rundiffusion_omniverse.form import CAPTURING_TEXT, ImageSlot, ToolForm
from rundiffusion_omniverse.panel import RunDiffusionPanel
from rundiffusion_omniverse.theme import DANGER


class FakeWidget:
    open_containers: list = []

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.parent = self.open_containers[-1] if self.open_containers else None
        self.children: list = []
        if self.parent is not None:
            self.parent.children.append(self)

    def __enter__(self):
        FakeWidget.open_containers.append(self)
        return self

    def __exit__(self, *_exc) -> None:
        FakeWidget.open_containers.pop()

    def clear(self) -> None:
        self.children = []


def _kind(name: str):
    return type(name, (FakeWidget,), {"kind": name})


class FakeCombo(FakeWidget):
    """Enough of ui.ComboBox for a row that offers more than one source."""

    kind = "ComboBox"

    def __init__(self, index: int = 0, *labels, **kwargs) -> None:
        super().__init__(index, *labels, **kwargs)
        self.index = index
        self.model = _FakeComboModel(index)


class _FakeComboModel:
    def __init__(self, index: int) -> None:
        self._value = type("V", (), {"as_int": index})()
        self.changed: list = []

    def get_item_value_model(self):
        return self._value

    def add_item_changed_fn(self, fn) -> None:
        self.changed.append(fn)


class FakeCheckBox(FakeWidget):
    """Enough of ui.CheckBox for the Keep this view box in the capture column."""

    kind = "CheckBox"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.model = _FakeBoolModel()


class _FakeBoolModel:
    def __init__(self) -> None:
        self._value = False
        self.changed: list = []

    def set_value(self, value) -> None:
        self._value = bool(value)

    def get_value_as_bool(self) -> bool:
        return self._value

    def add_value_changed_fn(self, fn) -> None:
        self.changed.append(fn)


@pytest.fixture(autouse=True)
def drawable_ui(monkeypatch):
    FakeWidget.open_containers.clear()
    for name in (
        "VStack", "HStack", "ZStack", "VGrid", "Frame",
        "Button", "Label", "Spacer", "InvisibleButton", "Rectangle", "Circle",
    ):
        monkeypatch.setattr(ui, name, _kind(name), raising=False)
    monkeypatch.setattr(ui, "CheckBox", FakeCheckBox, raising=False)
    monkeypatch.setattr(ui, "ComboBox", FakeCombo, raising=False)


def _descendants(widget):
    for child in widget.children:
        yield child
        yield from _descendants(child)


class RecordingRedraws:
    """Redraws with the frame taken out, so a test needs no event loop.

    `booked` is the point of it: what proves a rebuild was deferred out of the
    dispatch that asked for it rather than run inside it.
    """

    def __init__(self) -> None:
        self.booked: list[str] = []

    def request(self, key: str, render) -> None:
        self.booked.append(key)
        render()

    def cancel(self, keep_prefix: str | None = None) -> None:
        self.booked = []


def _form(captured: bool = True) -> ToolForm:
    """A form with one image field. `captured` decides whether the frame has
    already folded to its strip, which is where most refusals are read: the
    field holds a capture, and the next one is the one that fails."""
    form = ToolForm()
    form.image_field_keys = ["image"]
    form.image_field_labels = {"image": "Image"}
    form._plural_keys = {"image"}
    form.image_slots["image"] = [ImageSlot(1, png=b"png")] if captured else []
    form._capture_target = "image"
    form._redraws = RecordingRedraws()
    return form


def _built(form: ToolForm):
    """The capture zone, drawn into a fake frame, which is where it speaks."""
    container = ui.Frame()
    form._capture_container = container
    form._render_capture_zone_now()
    return container


def _labels(container):
    return [
        node for node in _descendants(container)
        if getattr(node, "kind", None) == "Label"
    ]


class TestTheLineInTheFrame:
    def test_the_full_frame_says_it_too(self):
        """Before the first capture the frame is open, and the refusal takes
        the band's line where the count of images to send would be."""
        form = _form(captured=False)
        form._render_image_field = lambda _key: None
        form.show_capture_error("image", "There is no active viewport to capture.")

        texts = [node.args[0] for node in _labels(_built(form)) if node.args]

        assert "There is no active viewport to capture." in texts
        assert "Nothing sent yet" not in texts

    def test_nothing_wrong_in_the_full_frame_says_what_will_be_sent(self):
        form = _form(captured=False)
        form._render_image_field = lambda _key: None

        texts = [node.args[0] for node in _labels(_built(form)) if node.args]

        assert "Nothing sent yet" in texts

    def test_nothing_is_wrong_so_there_is_no_line(self):
        """Absent rather than blank, so it takes no room in a docked panel."""
        form = _form()
        form._render_image_field = lambda _key: None

        texts = [node.args[0] for node in _labels(_built(form)) if node.args]

        assert not any("no longer on the stage" in str(text) for text in texts)

    def test_a_refusal_is_shown(self):
        form = _form()
        form._render_image_field = lambda _key: None
        form.show_capture_error("image", "/World/Cameras/Lobby is no longer on the stage.")

        texts = [node.args[0] for node in _labels(_built(form)) if node.args]

        assert "/World/Cameras/Lobby is no longer on the stage." in texts

    def test_it_is_red(self):
        """Colour is what separates it from the labels around it."""
        form = _form()
        form._render_image_field = lambda _key: None
        form.show_capture_error("image", "That camera is gone.")

        styled = [
            node for node in _labels(_built(form))
            if node.kwargs.get("style", {}).get("color") == DANGER
        ]

        assert len(styled) == 1
        assert styled[0].args[0] == "That camera is gone."

    def test_it_survives_the_row_being_rebuilt(self):
        """The row is rebuilt whenever an image is added, removed or selected.
        A message living only in the widget would vanish on the next rebuild
        without the thing it reported having changed."""
        form = _form()
        form._render_image_field = lambda _key: None
        form.show_capture_error("image", "That camera is gone.")

        _built(form)
        texts = [node.args[0] for node in _labels(_built(form)) if node.args]

        assert "That camera is gone." in texts


class TestSayingItIsHappening:
    def test_a_capture_in_flight_says_so_in_the_frame(self):
        """The status line at the foot of the panel is off by default, so for
        most people this is the only sign the click landed."""
        form = _form()
        form._render_image_field = lambda _key: None
        form.show_capture_busy("image", CAPTURING_TEXT)

        texts = [node.args[0] for node in _labels(_built(form)) if node.args]

        assert CAPTURING_TEXT in texts

    def test_it_is_not_dressed_as_a_failure(self):
        """Muted, not DANGER. Something is happening, not something is wrong."""
        form = _form()
        form._render_image_field = lambda _key: None
        form.show_capture_busy("image", CAPTURING_TEXT)

        styled = [
            node
            for node in _labels(_built(form))
            if node.kwargs.get("style", {}).get("color") == DANGER
        ]

        assert styled == []

    def test_it_survives_the_row_being_rebuilt(self):
        """A capture takes a second or more, and selecting an image rebuilds
        the row. A message living only in the widget would vanish with the
        capture still running."""
        form = _form()
        form._render_image_field = lambda _key: None
        form.show_capture_busy("image", CAPTURING_TEXT)

        _built(form)
        texts = [node.args[0] for node in _labels(_built(form)) if node.args]

        assert CAPTURING_TEXT in texts

    def test_it_goes_when_the_capture_lands(self):
        form = _form()
        form._render_image_field = lambda _key: None
        form.show_capture_busy("image", CAPTURING_TEXT)

        form.clear_capture_busy("image")

        texts = [node.args[0] for node in _labels(_built(form)) if node.args]
        assert form.capture_busy("image") is None
        assert CAPTURING_TEXT not in texts

    def test_a_refusal_takes_the_line_rather_than_stacking_under_it(self):
        """One line for two states. They answer the same question at different
        moments and are never both true."""
        form = _form()
        form._render_image_field = lambda _key: None
        form.show_capture_error("image", "That camera is gone.")
        form.show_capture_busy("image", CAPTURING_TEXT)

        texts = [node.args[0] for node in _labels(_built(form)) if node.args]

        assert CAPTURING_TEXT in texts
        assert "That camera is gone." not in texts

    def test_the_same_message_twice_does_not_rebuild_the_row(self):
        """Pressing Capture again while one is running should not flicker."""
        redrawn: list = []
        form = _form()
        form._render_image_field = redrawn.append
        form.show_capture_busy("image", CAPTURING_TEXT)
        form.show_capture_busy("image", CAPTURING_TEXT)

        assert redrawn == ["image"]

    def test_two_captures_keep_the_line_until_the_last_one_lands(self):
        """Nothing disables Capture while one is running, and they serialise
        behind one lock, so two clicks is two captures a second or more apart.
        The first to finish must not take the line with it."""
        form = _form()
        form._render_image_field = lambda _key: None
        form.show_capture_busy("image", CAPTURING_TEXT)
        form.show_capture_busy("image", CAPTURING_TEXT)

        form.clear_capture_busy("image")

        assert form.capture_busy("image") == CAPTURING_TEXT

        form.clear_capture_busy("image")

        assert form.capture_busy("image") is None

    def test_clearing_more_often_than_starting_does_not_go_negative(self):
        form = _form()
        form._render_image_field = lambda _key: None
        form.show_capture_busy("image", CAPTURING_TEXT)

        form.clear_capture_busy("image")
        form.clear_capture_busy("image")
        form.show_capture_busy("image", CAPTURING_TEXT)
        form.clear_capture_busy("image")

        assert form.capture_busy("image") is None

    def test_clearing_a_field_that_was_not_busy_does_nothing(self):
        redrawn: list = []
        form = _form()
        form._render_image_field = redrawn.append

        form.clear_capture_busy("image")

        assert redrawn == []


class TestWhenItGoesAway:
    def test_clearing_it_removes_the_line(self):
        form = _form()
        form._render_image_field = lambda _key: None
        form.show_capture_error("image", "That camera is gone.")

        form.clear_capture_error("image")

        assert form.capture_error("image") is None

    def test_choosing_a_different_source_drops_it(self, monkeypatch):
        """A different source is a different question, so the old answer stops
        applying before the new one has even been tried."""
        changed: list = []

        class FakeValueModel:
            as_int = 1

        class FakeModel:
            def get_item_value_model(self):
                return FakeValueModel()

            def add_item_changed_fn(self, fn):
                changed.append(fn)

        class FakeCombo:
            def __init__(self, _index, *_labels, **_kwargs) -> None:
                self.model = FakeModel()

        monkeypatch.setattr(ui, "ComboBox", FakeCombo, raising=False)
        form = _form()
        form._render_image_field = lambda _key: None
        form._capture_sources = lambda: [
            ("Active viewport", None),
            ("Lobby", "/World/Cameras/Lobby"),
        ]
        form.show_capture_error("image", "That camera is gone.")

        form._build_capture_source("image")
        changed[0]()  # what picking a different row runs

        assert form.capture_error("image") is None
        assert form.capture_source("image") == "/World/Cameras/Lobby"

    def test_choosing_a_different_source_rebuilds_the_row_next_frame(
        self, monkeypatch
    ):
        """The whole row, not only the viewfinder. The viewfinder is showing
        the source chosen a moment ago and is now showing the wrong thing, and
        it is no longer the only part of the column that depends on which
        source is chosen: Keep this view as a camera is offered for the active
        viewport and not for a named one, and redrawing only the viewfinder
        left it on screen under a field that had moved to a camera.

        Next frame rather than now: this runs from the ComboBox model's
        item-changed callback, and rebuilding a row clears its container and
        builds into it, which omni.ui refuses during dispatch."""
        changed: list = []
        drawn: list = []

        class FakeValueModel:
            as_int = 1

        class FakeModel:
            def get_item_value_model(self):
                return FakeValueModel()

            def add_item_changed_fn(self, fn):
                changed.append(fn)

        class FakeCombo:
            def __init__(self, _index, *_labels, **_kwargs) -> None:
                self.model = FakeModel()

        monkeypatch.setattr(ui, "ComboBox", FakeCombo, raising=False)
        form = _form()
        form._render_image_field = lambda key: drawn.append(key)
        form._capture_sources = lambda: [
            ("Active viewport", None),
            ("Lobby", "/World/Cameras/Lobby"),
        ]

        form._build_capture_source("image")
        changed[0]()

        assert form._redraws.booked == ["images:image"]
        assert drawn == ["image"]

    def test_a_viewfinder_that_has_gone_is_not_drawn_into(self):
        """A booked redraw can arrive after the row it was booked for has been
        rebuilt for another reason, or after the tool was switched out."""
        drawn: list = []
        form = _form()
        form._on_render_viewfinder = lambda key, _frame: drawn.append(key)

        form._redraw_viewfinder("image")

        assert drawn == []

    def test_the_same_message_twice_does_not_rebuild_the_row(self):
        """Pressing a refused Capture again should not make the row flicker."""
        form = _form()
        redrawn: list = []
        form._render_image_field = redrawn.append

        form.show_capture_error("image", "That camera is gone.")
        form.show_capture_error("image", "That camera is gone.")

        assert redrawn == ["image"]

    def test_clearing_nothing_does_not_rebuild_the_row(self):
        form = _form()
        redrawn: list = []
        form._render_image_field = redrawn.append

        form.clear_capture_error("image")

        assert redrawn == []


class TestThePanelSaysItInOnePlace:
    def _panel(self):
        panel = object.__new__(RunDiffusionPanel)
        panel.status_errors = []
        panel._set_error = panel.status_errors.append
        panel.shown = []
        panel._form = type(
            "F", (), {"show_capture_error": lambda _s, key, msg: panel.shown.append((key, msg))}
        )()
        return panel

    def test_the_frame_gets_it_and_the_status_line_does_not(self):
        """The frame is what someone reads. The same sentence a panel's height
        away, under BILLED TO, was a second copy nobody was looking at."""
        panel = self._panel()

        panel._refuse_capture("image", "That camera is gone.")

        assert panel.shown == [("image", "That camera is gone.")]
        assert panel.status_errors == []


class TestACameraDeletedUnderTheField:
    """Reported from Base Editor, and worse than the bug it followed. A field
    was set to the only camera on the stage and the camera was deleted. The
    source list dropped to one entry, so the dropdown was replaced by a label
    (a dropdown holding one row is a control that looks like a decision and is
    not one), and the field went on remembering the deleted camera with no
    control left to change it. Every capture was refused, permanently.

    The recording of the choice now happens ABOVE that early return."""

    def _form_with(self, sources):
        form = _form()
        form._render_image_field = lambda _key: None
        form._capture_sources = lambda: sources
        return form

    def test_the_only_camera_going_puts_the_field_back_on_the_viewport(self):
        form = self._form_with([("Active viewport", None)])
        form._capture_source["image"] = "/World/Cameras/Lobby"

        _built(form)

        assert form.capture_source("image") is None

    def test_it_says_what_happened(self):
        """Silently changing what a button will do is normally wrong. Here the
        alternative is a field that can never capture again."""
        form = self._form_with([("Active viewport", None)])
        form._capture_source["image"] = "/World/Cameras/Lobby"

        _built(form)

        assert "no longer on the stage" in form.capture_error("image")
        assert "active viewport" in form.capture_error("image")

    def test_a_capture_can_be_taken_again_afterwards(self):
        """The whole point. The field is usable without reopening anything."""
        form = self._form_with([("Active viewport", None)])
        form._capture_source["image"] = "/World/Cameras/Lobby"
        asked: list = []
        form._on_capture = lambda key, source: asked.append((key, source))

        _built(form)
        form._capture_into("image")

        assert asked == [("image", None)]

    def test_it_resets_with_other_cameras_still_on_the_stage_too(self):
        """The dropdown is there in this case, so the field is not stuck. It
        would still be pointed at a camera nothing can capture."""
        form = self._form_with(
            [("Active viewport", None), ("Atrium", "/World/Cameras/Atrium")]
        )
        form._capture_source["image"] = "/World/Cameras/Lobby"

        _built(form)

        assert form.capture_source("image") is None

    def test_a_field_on_a_camera_that_is_still_there_is_left_alone(self):
        form = self._form_with(
            [("Active viewport", None), ("Lobby", "/World/Cameras/Lobby")]
        )
        form._capture_source["image"] = "/World/Cameras/Lobby"

        _built(form)

        assert form.capture_source("image") == "/World/Cameras/Lobby"
        assert form.capture_error("image") is None

    def test_a_field_on_the_active_viewport_is_left_alone(self):
        form = self._form_with([("Active viewport", None)])

        _built(form)

        assert form.capture_source("image") is None
        assert form.capture_error("image") is None
