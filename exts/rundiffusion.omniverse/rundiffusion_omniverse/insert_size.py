"""Asking how big a 3D asset should be before it goes onto the stage.

Asked every time rather than set once somewhere in the panel, because the right
answer is a fact about the thing being placed: a chair and a building generated
in the same session want different sizes, and a setting would be right for one
of them and silently wrong for the next. The last answer is offered again, so a
run of similar objects costs one click each.

Modal, because the insertion waits on it, and a size prompt that can be left
open behind the panel is an insertion that looks like it never started.
"""

from __future__ import annotations

import logging
import math
from typing import Callable

import omni.ui as ui

from . import prefs
from .stage import UNITS, InsertSize, is_known_unit
from .theme import (
    DANGER,
    SIZE_META,
    STYLE_CAPS,
    STYLE_HELP,
    STYLE_QUIET_BUTTON,
    STYLE_WEIGHTED_BUTTON,
)

logger = logging.getLogger(__name__)

WINDOW_TITLE = "Add to stage"

#: Offered the first time. A generated model is roughly one unit across in its
#: own file, and a metre is a size a person can picture before editing it.
DEFAULT_SIZE = InsertSize(1.0, "m")


def remembered_size() -> InsertSize:
    """The size last asked for, or the default where none usable was saved.

    Checked rather than trusted: the settings file is the user's to edit, and a
    zero, a negative or a unit this version does not know would otherwise
    prefill a dialog that then refuses its own starting value.
    """
    length = prefs.get_float(prefs.INSERT_LENGTH, DEFAULT_SIZE.length)
    unit = prefs.get_string(prefs.INSERT_UNIT, DEFAULT_SIZE.unit)
    if not (math.isfinite(length) and length > 0):
        length = DEFAULT_SIZE.length
    if not is_known_unit(unit):
        unit = DEFAULT_SIZE.unit
    return InsertSize(length, unit)


def remember_size(size: InsertSize) -> None:
    prefs.set_float(prefs.INSERT_LENGTH, size.length)
    prefs.set_string(prefs.INSERT_UNIT, size.unit)


def usable_length(value: float) -> bool:
    """Whether `value` can be a length at all."""
    return math.isfinite(value) and value > 0


class InsertSizeDialog:
    """The size prompt. One instance per panel, rebuilt on every `show`."""

    def __init__(self) -> None:
        self._window: ui.Window | None = None
        self._field: ui.FloatField | None = None
        self._combo: ui.ComboBox | None = None
        self._error: ui.Label | None = None
        self._on_chosen: Callable[[InsertSize | None], None] | None = None

    def show(self, on_chosen: Callable[[InsertSize | None], None]) -> None:
        """Ask for a size, then call `on_chosen` with it.

        `on_chosen(None)` means keep the size the asset arrives at. Cancelling,
        or closing the window, calls nothing: the user has changed their mind
        about adding it at all.
        """
        self._on_chosen = on_chosen
        start = remembered_size()
        units = [unit for unit, _meters in UNITS]

        if self._window is None:
            self._window = ui.Window(
                WINDOW_TITLE,
                width=340,
                height=150,
                flags=(
                    ui.WINDOW_FLAGS_MODAL
                    | ui.WINDOW_FLAGS_NO_RESIZE
                    | ui.WINDOW_FLAGS_NO_COLLAPSE
                    | ui.WINDOW_FLAGS_NO_DOCKING
                    | ui.WINDOW_FLAGS_NO_SCROLLBAR
                ),
            )

        with self._window.frame:
            with ui.VStack(spacing=8):
                ui.Label("LONGEST SIDE", height=0, style=STYLE_CAPS)
                with ui.HStack(height=24, spacing=6):
                    self._field = ui.FloatField(height=24)
                    self._field.model.set_value(start.length)
                    self._combo = ui.ComboBox(units.index(start.unit), *units, width=70)
                ui.Label(
                    "The model is scaled so its longest side is this long, "
                    "in any unit the stage uses.",
                    word_wrap=True,
                    height=0,
                    style=STYLE_HELP,
                )
                # Built empty and filled in only when a length is refused, so
                # the dialog does not jump between two heights as it is used.
                self._error = ui.Label(
                    "",
                    height=0,
                    style={"font_size": SIZE_META, "color": DANGER},
                )
                ui.Spacer()
                with ui.HStack(height=26, spacing=4):
                    ui.Button(
                        "Keep original size",
                        style=STYLE_QUIET_BUTTON,
                        clicked_fn=self._keep_original,
                    )
                    ui.Button("Cancel", style=STYLE_QUIET_BUTTON, clicked_fn=self.hide)
                    ui.Button(
                        "Add",
                        width=ui.Fraction(0.8),
                        style=STYLE_WEIGHTED_BUTTON,
                        clicked_fn=self._add,
                    )

        self._window.visible = True
        try:
            self._window.focus()
        except Exception:  # noqa: BLE001 - an unfocused modal still works by mouse
            logger.debug("RunDiffusion: could not focus the size dialog.")

    def _chosen_size(self) -> InsertSize | None:
        if self._field is None or self._combo is None:
            return None
        length = self._field.model.get_value_as_float()
        index = self._combo.model.get_item_value_model().as_int
        if not usable_length(length) or not 0 <= index < len(UNITS):
            return None
        return InsertSize(length, UNITS[index][0])

    def _add(self) -> None:
        size = self._chosen_size()
        if size is None:
            # Kept open with the reason, rather than inserting at a size the
            # user did not ask for.
            if self._error is not None:
                self._error.text = "Enter a length greater than zero."
            return
        remember_size(size)
        self._finish(size)

    def _keep_original(self) -> None:
        self._finish(None)

    def _finish(self, size: InsertSize | None) -> None:
        on_chosen, self._on_chosen = self._on_chosen, None
        self.hide()
        if on_chosen is not None:
            on_chosen(size)

    def hide(self) -> None:
        if self._window is not None:
            self._window.visible = False

    def destroy(self) -> None:
        self._on_chosen = None
        self._field = None
        self._combo = None
        self._error = None
        if self._window is not None:
            self._window.destroy()
            self._window = None
