"""Actions on a library cell, revealed while the pointer is on it.

Acting on a library item used to cost two clicks before the
action itself: one to select the cell, which filled a row under the grid, and
one on the button in that row. The row is still there and still works. This is
the same actions without the first click.

Two things about it are easy to get wrong and invisible when they are.

**A hidden widget is never hovered**, so a bar listening for its own hover
would never appear. The listener belongs on the cell, which is also what keeps
the bar up while the pointer travels onto a button.

**omni.ui hit-tests in the order widgets are issued, not in Z order.** The
first draft laid the bar over the cell-wide invisible button inside the
ZStack, and the button underneath claimed every press: the actions looked live
and were dead, and clicking one opened the selection row instead. The fix is
that they no longer overlap at all.
"""

from __future__ import annotations

import types

import omni.ui as ui
import pytest

from rundiffusion_omniverse.asset_grid import AssetGrid, _reveal_on_hover


class FakeWidget:
    """Enough of a ui.Widget to be shown, hidden, hovered, and nested."""

    #: The container currently being built into, so a widget can record what it
    #: was made inside. Overlap is a question about nesting, and nesting is the
    #: only place a fake can answer it.
    open_containers: list = []

    def __init__(self, *_args, **kwargs) -> None:
        self.visible = kwargs.get("visible", True)
        self.hovered_fn = None
        self.parent = self.open_containers[-1] if self.open_containers else None

    def __enter__(self):
        FakeWidget.open_containers.append(self)
        return self

    def __exit__(self, *_exc) -> None:
        FakeWidget.open_containers.pop()

    def set_mouse_hovered_fn(self, fn) -> None:
        self.hovered_fn = fn


class FakeVStack(FakeWidget):
    pass


class FakeInvisibleButton(FakeWidget):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.clicked_fn = kwargs.get("clicked_fn")


@pytest.fixture(autouse=True)
def drawable_ui(monkeypatch):
    """The conftest stub is bare; the click layer needs what it is made of."""
    FakeWidget.open_containers.clear()
    made: dict[str, list] = {"Button": [], "InvisibleButton": []}

    def button(label, **kwargs):
        widget = FakeWidget(label, **kwargs)
        widget.label = label
        widget.clicked_fn = kwargs.get("clicked_fn")
        made["Button"].append(widget)
        return widget

    def invisible_button(**kwargs):
        widget = FakeInvisibleButton(**kwargs)
        made["InvisibleButton"].append(widget)
        return widget

    monkeypatch.setattr(ui, "VStack", FakeVStack, raising=False)
    monkeypatch.setattr(ui, "HStack", FakeWidget, raising=False)
    monkeypatch.setattr(ui, "Button", button, raising=False)
    monkeypatch.setattr(ui, "InvisibleButton", invisible_button, raising=False)
    return types.SimpleNamespace(
        buttons=made["Button"], invisible=made["InvisibleButton"]
    )


class FakeAsset:
    id = "asset-1"


def _select(_asset) -> None:
    """Stands in for the panel's select-the-cell callback."""


@pytest.fixture
def grid(tmp_path):
    return AssetGrid(tmp_path)


class TestACellWithNoActions:
    def test_it_is_one_invisible_button_and_no_bar(self, grid, drawable_ui):
        """What the image picker wants, where a click means pick and nothing
        else."""
        assert grid._build_click_layer(FakeAsset(), _select, None) is None
        assert len(drawable_ui.invisible) == 1

    def test_an_asset_offered_no_actions_is_the_same(self, grid):
        assert grid._build_click_layer(FakeAsset(), _select, lambda _a: []) is None


class TestACellWithActions:
    def test_the_bar_starts_hidden(self, grid):
        """A bar that flashed on every redraw of a grid rebuilt on every tab
        switch would be worse than no bar."""
        bar = grid._build_click_layer(
            FakeAsset(), _select, lambda _a: [("Use", lambda _a: None)]
        )

        assert bar is not None
        assert bar.visible is False

    def test_every_action_becomes_a_button(self, grid, drawable_ui):
        actions = [("Use", lambda _a: None), ("Save", lambda _a: None)]

        grid._build_click_layer(FakeAsset(), _select, lambda _a: actions)

        assert [button.label for button in drawable_ui.buttons] == ["Use", "Save"]

    def test_a_button_is_given_the_asset_of_the_cell_it_is_in(self, grid, drawable_ui):
        """Not one closed over at build time. A rebuilt cell must not act on
        the asset that used to be in its place."""
        called: list = []
        asset = FakeAsset()

        grid._build_click_layer(asset, _select, lambda _a: [("Use", called.append)])
        drawable_ui.buttons[0].clicked_fn()

        assert called == [asset]

    def test_the_select_target_is_beside_the_bar_and_not_behind_it(
        self, grid, drawable_ui
    ):
        """The regression. omni.ui hit-tests in issue order rather than in Z
        order, so a cell-wide invisible button UNDER the bar takes every press
        and the actions are dead while looking live. Sharing one column is what
        makes overlapping impossible rather than merely unlikely."""
        bar = grid._build_click_layer(
            FakeAsset(), _select, lambda _a: [("Use", lambda _a: None)]
        )
        select = drawable_ui.invisible[0]

        assert isinstance(select.parent, FakeVStack)
        assert select.parent is bar.parent

    def test_the_cell_still_selects(self, grid, drawable_ui):
        """The bar is an addition. Clicking the picture itself must still open
        the row under the grid, which is where the preview and the full labels
        live."""
        picked: list = []
        asset = FakeAsset()

        grid._build_click_layer(
            asset, picked.append, lambda _a: [("Use", lambda _a: None)]
        )
        drawable_ui.invisible[0].clicked_fn()

        assert picked == [asset]


class TestRevealingTheBar:
    def test_the_pointer_arriving_shows_it(self):
        cell, bar = FakeWidget(), FakeWidget()
        bar.visible = False

        _reveal_on_hover(cell, bar)
        cell.hovered_fn(True)

        assert bar.visible is True

    def test_the_pointer_leaving_hides_it_again(self):
        cell, bar = FakeWidget(), FakeWidget()

        _reveal_on_hover(cell, bar)
        cell.hovered_fn(True)
        cell.hovered_fn(False)

        assert bar.visible is False

    def test_the_listener_goes_on_the_cell_and_not_on_the_bar(self):
        """The bar starts hidden and a hidden widget is never hovered, so a bar
        listening for its own hover would never appear."""
        cell, bar = FakeWidget(), FakeWidget()

        _reveal_on_hover(cell, bar)

        assert cell.hovered_fn is not None
        assert bar.hovered_fn is None

    def test_a_cell_destroyed_under_the_pointer_costs_nothing(self):
        """A rebuild can destroy a cell while the pointer is still on it, and a
        destroyed widget still answers."""

        class Destroyed(FakeWidget):
            def __init__(self) -> None:
                # Deliberately not FakeWidget's, which would set `visible` and
                # trip the raise below before the test has begun.
                self.hovered_fn = None

            @property
            def visible(self):
                raise RuntimeError("this widget is gone")

            @visible.setter
            def visible(self, _value):
                raise RuntimeError("this widget is gone")

        cell = FakeWidget()
        _reveal_on_hover(cell, Destroyed())

        cell.hovered_fn(True)  # must not raise
