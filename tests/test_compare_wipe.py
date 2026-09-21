"""Moving the seam in the compare window, by slider, handle and arrow key.

Both were reported from Base Editor. Dragging the handle moved the slider and
left the pictures where they were, and the arrow keys the window advertised did
nothing at all. The drawing needs Kit; which control moves what, and how a key
is recognised, does not.
"""

from __future__ import annotations

import sys
import types

import pytest

from rundiffusion_omniverse import compare as compare_module
from rundiffusion_omniverse.compare import (
    AREA_WIDTH,
    HANDLE_WIDTH,
    WIPE_STEP,
    CompareWindow,
    offset_to_wipe,
    wipe_to_offset,
)
from rundiffusion_omniverse.dialogs import ImageViewer

LEFT, RIGHT = 263, 262


class _Pixel:
    def __init__(self, value: float) -> None:
        self.value = value


class _Widget:
    """Records the width or offset it was last given."""

    def __init__(self) -> None:
        self.width = None
        self.offset_x = None


class _SliderModel:
    def __init__(self) -> None:
        self.value = 0.5
        self.changed: list = []

    def set_value(self, value: float) -> None:
        self.value = value
        for fn in self.changed:
            fn(self)

    def get_value_as_float(self) -> float:
        return self.value


@pytest.fixture(autouse=True)
def fake_host(monkeypatch):
    monkeypatch.setattr(
        compare_module, "ui", types.SimpleNamespace(Pixel=_Pixel), raising=False
    )
    carb = types.ModuleType("carb")
    carb_input = types.ModuleType("carb.input")
    # IntEnum-like members: equal to their int only once converted, which is
    # the whole trap. A plain object with __int__ reproduces it.
    carb_input.KeyboardInput = types.SimpleNamespace(
        LEFT=type("K", (), {"__int__": lambda self: LEFT})(),
        RIGHT=type("K", (), {"__int__": lambda self: RIGHT})(),
    )
    carb.input = carb_input
    monkeypatch.setitem(sys.modules, "carb", carb)
    monkeypatch.setitem(sys.modules, "carb.input", carb_input)


def _window() -> CompareWindow:
    window = CompareWindow()
    window._sources = [("Captured view · Lobby", "in.png", "Captured view")]
    window._clip = _Widget()
    window._seam = _Widget()
    window._slider = types.SimpleNamespace(model=_SliderModel())
    window._slider.model.changed.append(window._on_slider_changed)
    return window


class TestTheArithmetic:
    def test_the_ends_are_the_ends(self):
        assert wipe_to_offset(0.0) == 0.0
        assert wipe_to_offset(1.0) == AREA_WIDTH - HANDLE_WIDTH

    def test_there_and_back(self):
        assert offset_to_wipe(wipe_to_offset(0.3)) == pytest.approx(0.3)

    def test_a_drag_past_either_edge_stays_inside(self):
        assert offset_to_wipe(-50) == 0.0
        assert offset_to_wipe(AREA_WIDTH * 2) == 1.0


class TestEveryControlMovesThePictures:
    def test_the_slider_moves_the_curtain_and_the_handle(self):
        window = _window()

        window._slider.model.set_value(0.25)

        assert window._wipe == 0.25
        assert window._clip.width.value == wipe_to_offset(0.25) + HANDLE_WIDTH / 2
        assert window._seam.offset_x.value == wipe_to_offset(0.25)

    def test_dragging_the_handle_moves_the_curtain_and_the_slider(self):
        """The reported fault: the slider followed and the pictures did not."""
        window = _window()

        window._on_seam_dragged(_Pixel(wipe_to_offset(0.8)))

        assert window._wipe == pytest.approx(0.8)
        assert window._clip.width.value == pytest.approx(
            wipe_to_offset(0.8) + HANDLE_WIDTH / 2
        )
        assert window._slider.model.value == pytest.approx(0.8)

    def test_a_drag_past_the_edge_puts_the_handle_back(self):
        window = _window()

        window._on_seam_dragged(_Pixel(-40))

        assert window._wipe == 0.0
        assert window._seam.offset_x.value == 0.0


class TestTheArrowKeys:
    def test_right_steps_the_seam_right(self):
        """The key arrives as an int. Compared with the enum member it never
        matched, which is why the keys did nothing."""
        window = _window()

        window._on_key_pressed(RIGHT, 0, True)

        assert window._wipe == pytest.approx(0.5 + WIPE_STEP)
        assert window._slider.model.value == pytest.approx(0.5 + WIPE_STEP)

    def test_left_steps_it_left(self):
        window = _window()

        window._on_key_pressed(LEFT, 0, True)

        assert window._wipe == pytest.approx(0.5 - WIPE_STEP)

    def test_a_release_does_nothing(self):
        window = _window()

        window._on_key_pressed(RIGHT, 0, False)

        assert window._wipe == 0.5

    def test_the_image_viewer_reads_keys_the_same_way(self):
        viewer = ImageViewer()
        stepped: list = []
        viewer._on_previous = lambda: stepped.append("previous")
        viewer._on_next = lambda: stepped.append("next")

        viewer._on_key_pressed(RIGHT, 0, True)
        viewer._on_key_pressed(LEFT, 0, True)

        assert stepped == ["next", "previous"]
