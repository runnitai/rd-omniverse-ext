"""How an image field lays out when it is holding more than a few images.

Reported from Base Editor: a field can hold twelve images and they were all
being put in one row, which ran off the edge of a docked panel into a
horizontal scroll.

They wrap now, and the mechanism is worth pinning rather than eyeballing. The
thumbnails go in a grid told its COLUMN WIDTH rather than its column count, so
the number per row follows the panel instead of being decided here. That only
works if something beside the grid is the part with a fixed width, which is the
panel's width itself: the capture controls moved out of the row into a zone of
their own at the top of the tab, so the grid now has the row to itself.
"""

from __future__ import annotations

import omni.ui as ui
import pytest

from rundiffusion_omniverse.form import (
    ADD_TILE,
    PREVIEW_GAP,
    PREVIEW_SIZE,
    ToolForm,
)


class FakeWidget:
    """Records what it was made with, and what it was made inside."""

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
    """A widget class that remembers which omni.ui name it stands for."""
    return type(name, (FakeWidget,), {"kind": name})


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
        "VStack",
        "HStack",
        "ZStack",
        "VGrid",
        "Frame",
        "Button",
        "Label",
        "Spacer",
        "InvisibleButton",
        "Rectangle",
    ):
        monkeypatch.setattr(ui, name, _kind(name), raising=False)
    monkeypatch.setattr(ui, "CheckBox", FakeCheckBox, raising=False)


def _add_tiles(row: FakeWidget) -> list:
    return [node for node in _of_kind(row, "ZStack") if node.kwargs.get("width") == ADD_TILE]


def _thumbnails(row: FakeWidget) -> list:
    return [
        node for node in _of_kind(row, "ZStack") if node.kwargs.get("width") == PREVIEW_SIZE
    ]


def _row(images: int, plural: bool = True) -> FakeWidget:
    """Build one image field holding `images` pinned captures, and return it."""
    form = ToolForm()
    form.image_field_keys = ["image"]
    form.image_field_labels = {"image": "Image"}
    form._plural_keys = {"image"} if plural else set()
    for _ in range(images):
        form.image_slots.setdefault("image", []).append(_slot(form))
    container = ui.Frame()
    form._image_containers["image"] = container
    form._render_image_field_now("image")
    return container


def _slot(form: ToolForm):
    from rundiffusion_omniverse.form import ImageSlot

    return ImageSlot(form._next_slot_id(), png=b"png")


def _descendants(widget: FakeWidget):
    for child in widget.children:
        yield child
        yield from _descendants(child)


def _of_kind(widget: FakeWidget, kind: str) -> list:
    return [node for node in _descendants(widget) if getattr(node, "kind", None) == kind]


class TestTheThumbnailsWrap:
    def test_they_go_in_a_grid(self):
        row = _row(images=12)

        assert _of_kind(row, "VGrid")

    def test_the_grid_is_told_a_column_width_and_not_a_column_count(self):
        """A count would fix the number per row here, which is the one place
        that cannot know how wide the panel has been pulled."""
        grid = _of_kind(_row(images=3), "VGrid")[0]

        assert grid.kwargs.get("column_width") == PREVIEW_SIZE + PREVIEW_GAP
        assert "column_count" not in grid.kwargs

    def test_the_cell_carries_the_gutter(self):
        """A grid sizes its own cells, so a spacing on top of that is what
        pushes the last column off the edge."""
        grid = _of_kind(_row(images=3), "VGrid")[0]

        assert grid.kwargs.get("row_height") == PREVIEW_SIZE + PREVIEW_GAP

    def test_every_thumbnail_is_inside_it(self):
        row = _row(images=12)
        grid = _of_kind(row, "VGrid")[0]

        tiles = _thumbnails(row)
        assert len(tiles) == 12
        assert all(tile.parent is grid for tile in tiles)

    def test_the_add_tile_wraps_with_them(self):
        """Rather than being stranded at the end of the last row."""
        row = _row(images=12)
        grid = _of_kind(row, "VGrid")[0]

        add = _add_tiles(row)
        assert len(add) == 1
        assert add[0].parent is grid

    def test_an_empty_field_still_offers_the_add_tile(self):
        row = _row(images=0)

        assert len(_add_tiles(row)) == 1

    def test_an_empty_field_says_how_it_fills_and_a_full_one_does_not(self):
        """The help is for the empty state only. Left on screen after it has
        been read, it is the permanent prose the layout exists to remove."""
        help_line = "Captures land here in send order."
        empty = [node.args[0] for node in _of_kind(_row(images=0), "Label") if node.args]
        full = [node.args[0] for node in _of_kind(_row(images=2), "Label") if node.args]

        assert help_line in empty
        assert help_line not in full

    def test_a_singular_field_that_is_full_does_not(self):
        """Adding to a singular field replaces, so a second tile would be
        offering something that is not on offer."""
        row = _row(images=1, plural=False)

        assert _add_tiles(row) == []


class TestTheRowIsOnlyAboutWhatIsSent:
    def test_there_is_no_viewfinder_in_it(self):
        """Taking a view has its own zone at the top of the tab now. A second
        live frame in the row would be two pictures claiming to be the view."""
        form = ToolForm()
        form.image_field_keys = ["image"]
        form.image_field_labels = {"image": "Image"}
        form._plural_keys = {"image"}
        container = ui.Frame()
        form._image_containers["image"] = container

        form._render_image_field_now("image")

        assert form._viewfinders == {}
        assert not any(
            node.args[:1] and str(node.args[0]).startswith("Capture view")
            for node in _of_kind(container, "Button")
        )

    def test_the_header_states_the_ceiling_up_front(self):
        """Counted across the whole run, because that is what the server
        counts, and said before the thirteenth image rather than at it."""
        texts = [node.args[0] for node in _of_kind(_row(images=3), "Label") if node.args]

        assert "3 of 12" in texts
        assert "IMAGE · OPTIONAL" in texts

    def test_the_row_holding_the_images_can_grow(self):
        """It used to be pinned to one thumbnail tall, which is the same thing
        as saying the images may not wrap."""
        row = _row(images=12)
        grid = _of_kind(row, "VGrid")[0]

        assert grid.parent.kwargs.get("height") == 0
