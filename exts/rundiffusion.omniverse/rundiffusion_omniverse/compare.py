"""Compare a render against the image the run was given.

This was one of the two open questions the extension was first built to
answer, and the answer moved once it was used.

**What was built first.** `ViewportWindow.get_frame(name)` puts a `ui.Frame`
into the viewport's own z-stack, so a render CAN be drawn over the live viewport
rather than beside it, and the panel does not have to own those pixels. The
original idea of a "camera-lock" turned out to describe the wrong remedy:
locking fights the host for control of its own viewport, so the overlay watched
`ViewportAPI`'s `view` and `projection` matrices instead and cleared itself the
moment either changed. Both halves worked. That finding stands and is worth
keeping, because anything else this plugin ever wants to draw over the viewport
is built the same way.

**Why it is not what ships.** A wipe against the LIVE viewport compares a render
to wherever the camera happens to be, so it is only a comparison for as long as
nobody touches the mouse, and the overlay spent most of its life dismissing
itself. The thing a person actually wants to see is the render against the image
it was made FROM: the captured view, or the library image that was fed in. That
image does not move, it is still on disk, and there can be several of them, so
it is a choice rather than an assumption.

So the comparison is a window: two images, a wipe between them, and a picker for
which input to wipe against. Nothing to invalidate, nothing to dismiss, and it
still works with every viewport in the app closed.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import omni.ui as ui

from .theme import (
    AMETHYST,
    CELL_EMPTY,
    CHIP_PLATE,
    SIZE_BODY,
    STYLE_CAPS,
    STYLE_META,
    STYLE_QUIET_BUTTON,
    STYLE_SECONDARY,
    STYLE_WEIGHTED_BUTTON,
    TEXT_EMPHASIS,
)

logger = logging.getLogger(__name__)

#: The comparison area, in pixels. Fixed rather than fluid because the wipe is a
#: clipping frame measured in pixels, and a clip that has to be recomputed on
#: every window resize is a lot of machinery for a window nobody resizes.
AREA_WIDTH = 660
AREA_HEIGHT = 340

#: The seam between the two images. Amethyst, so it reads as this plugin's line
#: rather than as an artefact in either picture.
SEAM_COLOR = AMETHYST
SEAM_WIDTH = 2

#: The grab handle on the seam. Big enough to be seen as something to take hold
#: of, which is what makes the wipe discoverable without reading a sentence
#: about it.
HANDLE_WIDTH = 16
HANDLE_HEIGHT = 34

#: How far one press of an arrow key moves the seam, as a fraction of the width.
WIPE_STEP = 0.05

WINDOW_TITLE = "Compare"

#: A name that is an identifier rather than a name. Library items and uploads
#: often carry a generated one, and a dropdown row reading "91BDB9F7-A40B..."
#: identifies nothing to a person.
_GENERATED_NAME = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-", re.ASCII)


class CompareSource:
    """One image a run was given, named for the field and position it sat in."""

    def __init__(
        self,
        label: str,
        slot_id: int,
        *,
        is_capture: bool,
        png: bytes | None = None,
        thumbnail: Any = None,
    ) -> None:
        self.label = label
        self.slot_id = slot_id
        self.is_capture = is_capture
        #: A capture's bytes, which are the full-size image rather than the
        #: thumbnail the form row draws.
        self.png = png
        self.thumbnail = thumbnail

    @property
    def side_label(self) -> str:
        """What the chip over the input's half of the picture calls it."""
        return "Captured view" if self.is_capture else "Chosen image"

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"CompareSource({self.label!r}, slot={self.slot_id})"


def _field_name(key: str) -> str:
    return key.replace("_", " ").strip().upper() or "IMAGE"


def _capture_name(description: str) -> str:
    """Where a captured view was taken from, given whatever the slot recorded.

    A capture from a named camera says which one, and that is the entire reason
    the camera is recorded: a run made from three cameras is three rows here,
    and the same words three times over tells the user nothing about which is
    which. A capture from the active viewport records nothing more specific,
    and says so.
    """
    text = (description or "").strip()
    if text.lower().startswith("captured from "):
        text = text[len("captured from "):]
    return text.strip() or "Active viewport"


def _chosen_name(description: str) -> str:
    """The asset's name out of the sentence the form row shows.

    The form writes "Sending kitchen.png." because that reads as a statement in
    a row; here it is an entry in a dropdown, where the sentence is noise and
    the filename is the whole point. A generated identifier is not a name, so
    it is not shown as one.
    """
    name = (description or "").strip()
    if name.lower().startswith("sending "):
        name = name[len("sending "):]
    name = name.rstrip(".").strip()
    if not name or _GENERATED_NAME.match(name) or name.lower().startswith("an image from"):
        return "chosen image"
    return name


def input_source_name(reference: Any, description: str) -> str:
    """One input image's source, as a person would name it."""
    if reference is None:
        return _capture_name(description)
    return _chosen_name(description)


def result_source_name(image_state: dict[str, Any] | None) -> str:
    """Where a run's FIRST input came from, or "" for a run given no image.

    The first because it is the one a run is mostly about, and the caption it
    goes in has room for one.
    """
    for slots in (image_state or {}).values():
        for slot in slots or []:
            _slot_id, reference, description, *_rest = slot
            return input_source_name(reference, description)
    return ""


def compare_sources(image_state: dict[str, Any] | None) -> list[CompareSource]:
    """The inputs of one run, in the order they were sent.

    Reads the same exported image state a run was submitted with, so what the
    picker offers is exactly what went up, including which position each image
    took. A run made an hour ago is still described correctly after the form has
    moved on, because the state travelled with the result.

    Each is named by what it is and where it came from: "Captured view · Lobby",
    "Chosen image · kitchen.png". The field and position lead only where the
    run carried more than one image, which is the only time they tell two rows
    apart.
    """
    entries = []
    for key, slots in (image_state or {}).items():
        for position, slot in enumerate(slots or [], start=1):
            entries.append((key, position, len(slots), slot))

    sources: list[CompareSource] = []
    for key, position, count, slot in entries:
        slot_id, reference, description, png, thumbnail, _camera_path = slot
        is_capture = reference is None
        kind = "Captured view" if is_capture else "Chosen image"
        source = input_source_name(reference, description)
        # A chosen image with no usable name has nothing to add to its kind.
        label = kind if source.lower() == kind.lower() else f"{kind} · {source}"
        if len(entries) > 1:
            prefix = _field_name(key)
            if count > 1:
                prefix = f"{prefix} {position}"
            label = f"{prefix} · {label}"
        sources.append(
            CompareSource(
                label,
                slot_id,
                is_capture=is_capture,
                png=png,
                thumbnail=thumbnail,
            )
        )
    return sources


def wipe_to_offset(fraction: float) -> float:
    """The handle's left edge, in pixels, for a wipe `fraction` of 0..1."""
    return max(0.0, min(1.0, fraction)) * (AREA_WIDTH - HANDLE_WIDTH)


def offset_to_wipe(pixels: float) -> float:
    """The wipe fraction for a handle whose left edge is at `pixels`."""
    return max(0.0, min(1.0, pixels / (AREA_WIDTH - HANDLE_WIDTH)))


class CompareWindow:
    """A render, one of its inputs, and a curtain between them.

    It can also finish the job. Judging a render is the moment someone decides
    to keep it, so Save, Send and Use in Create are here rather than back in a
    panel behind this window.
    """

    def __init__(self) -> None:
        self._window: ui.Window | None = None
        self._area: ui.Frame | None = None
        self._clip: ui.Frame | None = None
        self._slider: Any = None
        #: The draggable handle on the seam.
        self._seam: Any = None
        #: True while the handle is the thing moving the curtain, so its offset
        #: is not written back under the pointer.
        self._dragging = False
        #: True while this class is moving the slider, so the slider's own
        #: change callback does not move the curtain a second time.
        self._syncing_slider = False
        self._left_chip: ui.Label | None = None
        self._result_path: str | None = None
        #: (label, path, what to call that side of the picture)
        self._sources: list[tuple[str, Path, str]] = []
        self._index = 0
        self._wipe = 0.5

    # -- lifecycle ---------------------------------------------------------

    def show(
        self,
        result_path: Path | str,
        sources: list[tuple[str, Path, str]],
        *,
        title: str = "",
        actions: list[tuple[str, Any]] | None = None,
    ) -> None:
        """Open on `result_path`, wipeable against each source.

        `sources` are (label, path, side label): the label for the AGAINST
        dropdown, the file, and what the chip over the input's half calls it.
        `actions` are (label, callback) pairs for the row under the wipe; Save
        is drawn with weight wherever it appears, and Close is always last.
        """
        self._result_path = str(result_path)
        self._sources = list(sources)
        self._index = 0
        self._wipe = 0.5

        if self._window is None:
            self._window = ui.Window(
                WINDOW_TITLE, width=AREA_WIDTH + 40, height=AREA_HEIGHT + 140
            )
            # Arrow keys step the seam, which is what the hint beside the
            # slider promises. On the window rather than the slider, so it
            # works without first clicking into the slider.
            self._window.set_key_pressed_fn(self._on_key_pressed)
        self._window.title = f"{WINDOW_TITLE} · {title}" if title else WINDOW_TITLE

        with self._window.frame:
            with ui.VStack(spacing=8):
                with ui.VStack(height=0, spacing=6):
                    if self._sources:
                        with ui.HStack(height=24, spacing=6):
                            ui.Label("AGAINST", width=60, style=STYLE_CAPS)
                            if len(self._sources) > 1:
                                picker = ui.ComboBox(
                                    0, *[label for label, _path, _side in self._sources]
                                )
                                picker.model.add_item_changed_fn(self._on_source_changed)
                            else:
                                ui.Label(
                                    self._sources[0][0],
                                    style={"font_size": SIZE_BODY, "color": TEXT_EMPHASIS},
                                )
                    else:
                        # Said once, plainly. The window still opens, because a
                        # person who pressed Compare wants to see the render.
                        ui.Label(
                            "This run had no input images, so there is nothing "
                            "to wipe against. Here is the render on its own.",
                            word_wrap=True,
                            height=0,
                            style=STYLE_SECONDARY,
                        )

                with ui.HStack(height=AREA_HEIGHT):
                    ui.Spacer()
                    self._area = ui.Frame(width=AREA_WIDTH, height=AREA_HEIGHT)
                    ui.Spacer()
                self._render_area()

                with ui.VStack(height=0, spacing=6):
                    if self._sources:
                        with ui.HStack(height=24, spacing=6):
                            ui.Label("Wipe", width=40, style=STYLE_SECONDARY)
                            self._slider = ui.FloatSlider(min=0.0, max=1.0)
                            self._slider.model.set_value(self._wipe)
                            self._slider.model.add_value_changed_fn(self._on_slider_changed)
                            ui.Label("Arrow keys step", width=0, style=STYLE_META)
                    with ui.HStack(height=26, spacing=4):
                        for label, callback in actions or []:
                            weighted = label == "Save"
                            ui.Button(
                                label,
                                width=ui.Fraction(1.4 if weighted else 1.0),
                                style=STYLE_WEIGHTED_BUTTON if weighted else STYLE_QUIET_BUTTON,
                                clicked_fn=lambda c=callback: c(),
                            )
                        ui.Button("Close", style=STYLE_QUIET_BUTTON, clicked_fn=self.hide)

        self._window.visible = True
        # Keys go to the focused window only, and opening one does not focus
        # it. Without this the arrow keys reached whatever had focus before.
        try:
            self._window.focus()
        except Exception:  # noqa: BLE001 - an unfocused window still works by mouse
            logger.debug("RunDiffusion: could not focus the compare window.")

    def hide(self) -> None:
        if self._window is not None:
            self._window.visible = False

    @property
    def is_showing(self) -> bool:
        return self._window is not None and self._window.visible

    def destroy(self) -> None:
        self._area = None
        self._clip = None
        self._slider = None
        self._seam = None
        self._left_chip = None
        if self._window is not None:
            self._window.set_key_pressed_fn(None)
            self._window.destroy()
            self._window = None

    # -- the two images ----------------------------------------------------

    def _render_area(self) -> None:
        """The render underneath, the chosen input clipped over it from the left.

        The images are drawn at the SAME fixed size and both preserve their
        aspect, so they land on the same pixels and the curtain crosses one
        picture rather than two differently-placed ones.

        Which side is which is said ON the picture, in a chip in each top
        corner, rather than in a sentence under the slider.

        The handle is a draggable `ui.Placer`, the widget Kit's own splitters
        are built from. It was a mouse-move callback on the picture first, and
        in Kit that moved the slider without moving the curtain. Reported from
        Base Editor.
        """
        if self._area is None or self._result_path is None:
            return
        self._area.clear()
        self._clip = None
        self._seam = None
        self._left_chip = None
        source = self._current_source()
        with self._area:
            with ui.ZStack():
                ui.Rectangle(style={"background_color": CELL_EMPTY})
                ui.Image(
                    self._result_path,
                    width=AREA_WIDTH,
                    height=AREA_HEIGHT,
                    fill_policy=ui.FillPolicy.PRESERVE_ASPECT_FIT,
                )
                if source is not None:
                    with ui.HStack():
                        self._clip = ui.Frame(
                            width=ui.Pixel(self._clip_width()), horizontal_clipping=True
                        )
                        with self._clip:
                            ui.Image(
                                str(source[1]),
                                width=AREA_WIDTH,
                                height=AREA_HEIGHT,
                                fill_policy=ui.FillPolicy.PRESERVE_ASPECT_FIT,
                            )
                        ui.Spacer()
                with ui.VStack():
                    ui.Spacer(height=8)
                    with ui.HStack(height=22):
                        ui.Spacer(width=8)
                        if source is not None:
                            self._left_chip = self._chip(source[2])
                        ui.Spacer()
                        self._chip("This render")
                        ui.Spacer(width=8)
                    ui.Spacer()
                if source is not None:
                    # Last, so it is the topmost thing under the pointer.
                    self._seam = ui.Placer(
                        offset_x=ui.Pixel(self._seam_offset()),
                        draggable=True,
                        drag_axis=ui.Axis.X,
                    )
                    with self._seam:
                        self._build_handle()
                    self._seam.set_offset_x_changed_fn(self._on_seam_dragged)

    def _build_handle(self) -> None:
        """The seam, with a grab handle centred on it."""
        with ui.ZStack(width=HANDLE_WIDTH, height=AREA_HEIGHT):
            with ui.HStack():
                ui.Spacer()
                ui.Rectangle(width=SEAM_WIDTH, style={"background_color": SEAM_COLOR})
                ui.Spacer()
            with ui.VStack():
                ui.Spacer()
                ui.Rectangle(
                    height=HANDLE_HEIGHT,
                    style={"background_color": SEAM_COLOR, "border_radius": 3},
                )
                ui.Spacer()

    @staticmethod
    def _chip(text: str) -> Any:
        with ui.ZStack(width=0, height=22):
            ui.Rectangle(style={"background_color": CHIP_PLATE, "border_radius": 3})
            with ui.HStack(width=0):
                ui.Spacer(width=8)
                label = ui.Label(
                    text, width=0, style={"font_size": SIZE_BODY, "color": TEXT_EMPHASIS}
                )
                ui.Spacer(width=8)
        return label

    def _current_source(self) -> tuple[str, Path, str] | None:
        if not self._sources:
            return None
        index = min(max(self._index, 0), len(self._sources) - 1)
        return self._sources[index]

    def _seam_offset(self) -> float:
        """Where the handle's left edge sits, in pixels from the left."""
        return wipe_to_offset(self._wipe)

    def _clip_width(self) -> float:
        """How much of the input shows: up to the middle of the handle."""
        return self._seam_offset() + HANDLE_WIDTH / 2

    def set_wipe(self, fraction: float) -> None:
        """Move the curtain and the handle. `fraction` is 0..1 of the width.

        The one place both are moved, whichever control asked: the slider, the
        handle, or the arrow keys. The slider is kept in step too, with a guard
        so its own change callback does not come back through here.
        """
        self._wipe = max(0.0, min(1.0, fraction))
        if self._clip is not None:
            self._clip.width = ui.Pixel(self._clip_width())
        if self._seam is not None and not self._dragging:
            self._seam.offset_x = ui.Pixel(self._seam_offset())
        if self._slider is not None and not self._syncing_slider:
            self._syncing_slider = True
            try:
                self._slider.model.set_value(self._wipe)
            finally:
                self._syncing_slider = False

    def _on_slider_changed(self, model) -> None:
        if self._syncing_slider:
            return
        self.set_wipe(model.get_value_as_float())

    def _on_seam_dragged(self, offset) -> None:
        """The handle was dragged. Move the curtain to where it is now.

        The Placer is not told where to go while it is being dragged, because
        it already is there, and writing its offset back mid-drag fights the
        pointer. Past either end it is put back inside the picture.
        """
        pixels = float(getattr(offset, "value", offset))
        fraction = offset_to_wipe(pixels)
        self._dragging = True
        try:
            self.set_wipe(fraction)
        finally:
            self._dragging = False
        if self._seam is not None and abs(pixels - self._seam_offset()) > 0.5:
            self._seam.offset_x = ui.Pixel(self._seam_offset())

    def _move_seam(self, fraction: float) -> None:
        """Move the seam from a key or a caller: the same as the slider moving it."""
        self.set_wipe(fraction)

    def _on_key_pressed(self, key, _modifiers, pressed) -> None:
        """Left and right step the seam.

        Compared as integers, the way Kit's own windows compare them: the key
        arrives as an int and the enum member is not equal to one. Comparing
        against the enum directly is why the arrow keys did nothing. Reported
        from Base Editor.
        """
        if not pressed or not self._sources:
            return
        try:
            import carb.input

            left = int(carb.input.KeyboardInput.LEFT)
            right = int(carb.input.KeyboardInput.RIGHT)
        except Exception:  # noqa: BLE001 - no carb means no keys to read
            return
        if key == left:
            self._move_seam(self._wipe - WIPE_STEP)
        elif key == right:
            self._move_seam(self._wipe + WIPE_STEP)

    def _on_source_changed(self, model, *_args) -> None:
        try:
            index = model.get_item_value_model().as_int
        except Exception:  # noqa: BLE001 - a combo mid-teardown answers oddly
            return
        if index == self._index:
            return
        self._index = index
        self._render_area()
