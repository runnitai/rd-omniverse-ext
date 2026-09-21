"""Looking at a pinned input image before it is sent.

Reported from Base Editor while confirming a path-traced capture: the only
view of a captured image was the 58px thumbnail in the row. That is enough to
tell two images apart and not enough to judge whether a capture came out the
way it was meant to, which is exactly what someone checks straight after
framing a shot.

Clicking a thumbnail already means "select this one", and it has to keep
meaning that: it is what turns the row header into reorder and remove. So View
joins those controls rather than replacing the click.
"""

from __future__ import annotations

import omni.ui as ui
import pytest

from rundiffusion_omniverse.form import ImageSlot, ToolForm
from rundiffusion_omniverse.panel import RunDiffusionPanel


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


def _form(**kwargs) -> ToolForm:
    form = ToolForm(**kwargs)
    form.image_field_keys = ["image"]
    form.image_field_labels = {"image": "Image"}
    form._plural_keys = {"image"}
    form._render_image_field = lambda _key: None
    return form


class TestTheControl:
    def test_it_appears_beside_the_other_controls_for_a_selected_image(self):
        form = _form()
        form.image_slots["image"] = [ImageSlot(1, png=b"png"), ImageSlot(2, png=b"png")]
        form._selected_slot["image"] = 1
        container = ui.Frame()
        form._image_containers["image"] = container

        form._render_image_field_now("image")

        labels = [
            node.args[0]
            for node in _descendants(container)
            if getattr(node, "kind", None) == "Button" and node.args
        ]
        assert "View" in labels
        # The controls it joins rather than replaces.
        assert "Remove" in labels

    def test_nothing_is_selected_so_there_is_nothing_to_view(self):
        """With no selection the header states what will be sent instead."""
        form = _form()
        form.image_slots["image"] = [ImageSlot(1, png=b"png")]
        container = ui.Frame()
        form._image_containers["image"] = container

        form._render_image_field_now("image")

        labels = [
            node.args[0]
            for node in _descendants(container)
            if getattr(node, "kind", None) == "Button" and node.args
        ]
        assert "View" not in labels


def _descendants(widget: FakeWidget):
    for child in widget.children:
        yield child
        yield from _descendants(child)


class TestWhatItHandsOver:
    def test_the_slot_that_was_selected(self):
        viewed: list = []
        form = _form(on_view_image=lambda key, slot: viewed.append((key, slot.id)))
        form.image_slots["image"] = [ImageSlot(1, png=b"a"), ImageSlot(2, png=b"b")]

        form._view_slot("image", 2)

        assert viewed == [("image", 2)]

    def test_a_slot_that_has_gone_asks_for_nothing(self):
        """A stale click: the row is rebuilt whenever an image is removed, but
        the two rules must not have to agree by coincidence."""
        viewed: list = []
        form = _form(on_view_image=lambda key, slot: viewed.append((key, slot.id)))
        form.image_slots["image"] = [ImageSlot(1, png=b"a")]

        form._view_slot("image", 99)

        assert viewed == []

    def test_a_form_with_nowhere_to_show_it_does_nothing(self):
        form = _form()
        form.image_slots["image"] = [ImageSlot(1, png=b"a")]

        form._view_slot("image", 1)  # must not raise


class FakeViewer:
    def __init__(self) -> None:
        self.shown: list = []

    def show(self, path, **kwargs) -> None:
        self.shown.append((path, kwargs))


def _panel(tmp_path) -> RunDiffusionPanel:
    panel = object.__new__(RunDiffusionPanel)
    panel._result_dir = tmp_path
    panel._viewer = FakeViewer()
    panel._chosen_assets = {}
    panel._library_items = []
    panel._uploads_items = []
    panel.errors = []
    panel._set_error = panel.errors.append
    panel.spawned = []

    def spawn(coroutine):
        panel.spawned.append(coroutine)
        # Closed rather than left hanging: nothing here runs a loop, and an
        # un-awaited coroutine warns from the garbage collector two tests
        # later, where it reads as an unrelated failure.
        coroutine.close()

    panel._spawn = spawn
    return panel


class TestShowingACapture:
    def test_the_full_bytes_are_written_and_shown(self, tmp_path):
        """Not the thumbnail. The thumbnail is what was too small to judge."""
        panel = _panel(tmp_path)

        panel._view_image_slot("image", ImageSlot(7, png=b"the-full-capture"))

        path, _kwargs = panel._viewer.shown[0]
        assert path.read_bytes() == b"the-full-capture"

    def test_it_is_titled_with_where_it_came_from(self, tmp_path):
        """A run made from three cameras has three inputs, and `Captured view`
        three times over says nothing about which is which."""
        panel = _panel(tmp_path)

        panel._view_image_slot(
            "image", ImageSlot(7, png=b"png", description="Captured from Lobby")
        )

        _path, kwargs = panel._viewer.shown[0]
        assert kwargs["title"] == "Captured from Lobby"

    def test_a_viewport_capture_falls_back_to_plain_words(self, tmp_path):
        panel = _panel(tmp_path)

        panel._view_image_slot("image", ImageSlot(7, png=b"png"))

        _path, kwargs = panel._viewer.shown[0]
        assert kwargs["title"] == "Captured view"

    def test_a_capture_with_no_bytes_says_so(self, tmp_path):
        panel = _panel(tmp_path)

        panel._view_image_slot("image", ImageSlot(7, png=b""))

        assert panel.errors
        assert panel._viewer.shown == []


class TestShowingAChosenImage:
    def test_it_goes_the_same_way_the_library_sends_it(self, tmp_path):
        """One download and one route, so a library image looks the same here
        as it does there."""
        panel = _panel(tmp_path)
        slot = ImageSlot(3, reference={"ref": "x"}, description="Sending a.png")
        panel._chosen_assets[3] = "the-asset"

        panel._view_image_slot("image", slot)

        assert len(panel.spawned) == 1
        assert panel._viewer.shown == []

    def test_an_asset_that_is_no_longer_held_says_so(self, tmp_path):
        panel = _panel(tmp_path)
        slot = ImageSlot(3, reference={"ref": "x"})

        panel._view_image_slot("image", slot)

        assert panel.errors


class TestTheViewerDoesNotOfferToAddItTwice:
    """The window opened from an image field is looking at something ALREADY
    in that field. Use in Create would add a second slot holding the same
    picture, and the run would send it twice.

    The capture branch settles this by offering no actions at all: every verb
    that belongs to a capture is on the row it came from. A chosen asset still
    wants Save and Open, so it is the one verb that goes rather than all of
    them.
    """

    def _viewed(self, tmp_path, **kwargs):
        panel = _panel(tmp_path)
        panel._download_asset = lambda _asset: None
        panel._open_path = lambda _path: None
        panel._asset_detail = lambda _asset: "a-render.png"
        panel._dismissing = lambda actions: actions
        panel._view_asset(_FakeAsset(), tmp_path / "a.png", **kwargs)
        _path, shown = panel._viewer.shown[0]
        return [label for label, _action in shown["actions"]]

    def test_opening_it_from_the_field_offers_no_use(self, tmp_path):
        assert "Use in Create" not in self._viewed(tmp_path, offer_use=False)

    def test_it_can_still_be_saved_and_opened(self, tmp_path):
        """The verb that duplicates goes; the ones that do not, stay."""
        labels = self._viewed(tmp_path, offer_use=False)

        assert "Save to computer" in labels
        assert "Open in system viewer" in labels

    def test_opening_it_from_a_grid_still_offers_use(self, tmp_path):
        """There it is the whole point: the image is not in a field yet."""
        assert "Use in Create" in self._viewed(tmp_path)

    def test_the_field_route_asks_for_it(self, tmp_path):
        """`_view_image_slot` is the caller that knows the image is already
        pinned, so it is the one that has to say so."""
        panel = _panel(tmp_path)
        asked: list = []
        panel._open_asset = lambda asset, **kwargs: asked.append(kwargs) or _closed()
        panel._chosen_assets[3] = _FakeAsset()

        panel._view_image_slot("image", ImageSlot(3, reference={"ref": "x"}))

        assert asked == [{"offer_use": False}]


class _FakeAsset:
    is_image = True
    kind = "LIBRARY_REF"
    name = "a-render.png"


async def _closed() -> None:
    return None


class _GridAsset:
    kind = "LIBRARY_REF"

    def __init__(self, asset_id: str, is_image: bool = True) -> None:
        self.id = asset_id
        self.is_image = is_image
        self.name = f"{asset_id}.png"


class TestSteppingThroughTheLibrary:
    """Reported from Base Editor: opening a library image and wanting the next
    one meant closing the window and finding it in the grid behind it."""

    def _shown(self, tmp_path, asset, items, **kwargs):
        panel = _panel(tmp_path)
        panel._library_items = items
        panel._asset_detail = lambda _asset: ""
        panel._dismissing = lambda actions: actions
        panel._view_asset(asset, tmp_path / "a.png", **kwargs)
        return panel, panel._viewer.shown[0][1]

    def test_it_says_where_in_the_library_the_picture_is(self, tmp_path):
        items = [_GridAsset("a"), _GridAsset("b"), _GridAsset("c")]

        _panel_, shown = self._shown(tmp_path, items[1], items)

        assert shown["position"] == "2 of 3"
        assert shown["on_previous"] is not None
        assert shown["on_next"] is not None

    def test_the_ends_do_not_wrap_round(self, tmp_path):
        items = [_GridAsset("a"), _GridAsset("b")]

        _p, first = self._shown(tmp_path, items[0], items)
        _p, last = self._shown(tmp_path, items[1], items)

        assert first["on_previous"] is None
        assert last["on_next"] is None

    def test_a_video_is_stepped_over(self, tmp_path):
        """The viewer draws pictures; a video in the sequence would step out of
        the window into the system player."""
        items = [_GridAsset("a"), _GridAsset("clip", is_image=False), _GridAsset("c")]

        _p, shown = self._shown(tmp_path, items[0], items)

        assert shown["position"] == "1 of 2"

    def test_next_opens_the_next_picture(self, tmp_path):
        items = [_GridAsset("a"), _GridAsset("b")]
        panel, shown = self._shown(tmp_path, items[0], items)

        shown["on_next"]()

        assert panel.spawned[0].cr_code.co_name == "_open_asset"

    def test_an_image_opened_from_a_field_does_not_step(self, tmp_path):
        """That is one input being checked, not a place in a list."""
        items = [_GridAsset("a"), _GridAsset("b")]

        _p, shown = self._shown(tmp_path, items[0], items, offer_use=False)

        assert shown["position"] == ""
        assert shown["on_next"] is None
