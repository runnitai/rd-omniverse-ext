"""An empty or finished list does not offer more of itself.

Seen on the Uploads tab of an account with no uploads: "You have no uploads
yet." sat over a footer that still said "Showing 0", offered Load more, and
told the person to click an image. The image picker did the same under "No
images here yet." A disabled button in this toolkit looks like an enabled one,
so Load more read as a promise that there was something to load.
"""

from __future__ import annotations

import types

import omni.ui as ui
import pytest

from rundiffusion_omniverse.panel import SURFACE_LIBRARY, SURFACE_UPLOADS, RunDiffusionPanel


class FakeWidget:
    open_containers: list = []

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.enabled = True
        self.visible = True
        self.children: list = []
        self.parent = self.open_containers[-1] if self.open_containers else None
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


def _walk(widget):
    for child in widget.children:
        yield child
        yield from _walk(child)


def _buttons(widget, text):
    return [w for w in _walk(widget) if getattr(w, "kind", "") == "Button" and w.args[:1] == (text,)]


def _labels(widget):
    return [w.args[0] for w in _walk(widget) if getattr(w, "kind", "") == "Label" and w.args]


@pytest.fixture(autouse=True)
def drawable_ui(monkeypatch):
    FakeWidget.open_containers.clear()
    for name in ("VStack", "HStack", "Frame", "Button", "Label", "Spacer"):
        monkeypatch.setattr(ui, name, _kind(name), raising=False)


def _pager(**kwargs):
    panel = object.__new__(RunDiffusionPanel)
    frame = ui.Frame()
    panel._render_pager(frame, load_more=lambda: None, refresh=lambda: None, **kwargs)
    return frame


class TestLoadMore:
    def test_it_is_gone_once_there_is_nothing_more(self):
        frame = _pager(count=0, has_more=False, loading=False)

        (more,) = _buttons(frame, "Load more")
        assert more.visible is False

    def test_it_is_there_while_the_server_says_there_is_more(self):
        frame = _pager(count=24, has_more=True, loading=False)

        (more,) = _buttons(frame, "Load more")
        assert more.visible is True and more.enabled is True

    def test_it_stays_put_but_refuses_while_a_page_is_in_flight(self):
        frame = _pager(count=24, has_more=True, loading=True)

        (more,) = _buttons(frame, "Loading...")
        assert more.visible is True and more.enabled is False


class TestTheClickHint:
    def _panel(self, *, library=(), uploads=()):
        panel = object.__new__(RunDiffusionPanel)
        panel._library_items = list(library)
        panel._uploads_items = list(uploads)
        panel._selected_asset = {}
        panel._asset_actions_frame = {SURFACE_LIBRARY: ui.Frame(), SURFACE_UPLOADS: ui.Frame()}
        return panel

    def test_an_empty_grid_does_not_ask_for_a_click(self):
        panel = self._panel()

        panel._render_asset_actions_now(SURFACE_UPLOADS)

        assert _labels(panel._asset_actions_frame[SURFACE_UPLOADS]) == []

    def test_a_grid_with_images_does(self):
        panel = self._panel(library=[types.SimpleNamespace(id="a")])

        panel._render_asset_actions_now(SURFACE_LIBRARY)

        assert _labels(panel._asset_actions_frame[SURFACE_LIBRARY]) == [
            "Click an image to use, download or open it."
        ]
