"""An image fetch must not paint into a container that has been rebuilt.

Reported from a real session: the tool browser showed one tool's avatar
against a different tool, and which tool it landed on changed between runs.
The cause was not the data. Every avatar the plugin had downloaded was a
distinct file; the picture was being painted into the wrong widget.

`load_image_into` captures a frame and then awaits. Navigating in the browser
clears the body and builds new rows, so by the time the await returns, the
captured frame is one the container has already destroyed and whatever now
occupies its place inherits the picture. Nothing about it is deterministic:
it depends only on which fetches were in the air when the click happened.

So `cancel_pending` marks a generation, and a fetch that comes back into a
different generation drops what it fetched instead of drawing it.
"""

from __future__ import annotations

import asyncio
import types

import omni.ui as ui
import pytest

from rundiffusion_omniverse.asset_grid import AssetGrid


@pytest.fixture(autouse=True)
def drawable_ui(monkeypatch):
    """The conftest stub is bare; drawing needs the two names this path uses."""
    monkeypatch.setattr(ui, "Image", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        ui,
        "FillPolicy",
        types.SimpleNamespace(PRESERVE_ASPECT_FIT=object()),
        raising=False,
    )


class FakeFrame:
    """Enough of `ui.Frame` to record whether anything was drawn into it."""

    def __init__(self) -> None:
        self.cleared = 0

    def clear(self) -> None:
        self.cleared += 1

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


@pytest.fixture
def grid(tmp_path):
    return AssetGrid(tmp_path)


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


class TestGenerationGuard:
    def test_a_fetch_paints_when_nothing_was_rebuilt(self, grid, tmp_path, monkeypatch):
        painted = tmp_path / "avatar.png"
        painted.write_bytes(b"png")
        monkeypatch.setattr(grid, "_fetch", _returns(painted))
        frame = FakeFrame()

        _run(grid._fill_image(frame, "https://example.test/a.png", 48))

        assert frame.cleared == 1, "the image should have been drawn"

    def test_a_fetch_is_dropped_when_the_container_was_rebuilt(
        self, grid, tmp_path, monkeypatch
    ):
        painted = tmp_path / "avatar.png"
        painted.write_bytes(b"png")

        async def _fetch_then_navigate(_url):
            # The navigation lands while this fetch is in flight, which is the
            # whole of the bug: one click, mid-await.
            grid.cancel_pending()
            return painted, False, None

        monkeypatch.setattr(grid, "_fetch", _fetch_then_navigate)
        frame = FakeFrame()

        _run(grid._fill_image(frame, "https://example.test/a.png", 48))

        assert frame.cleared == 0, "a rebuilt frame must not be painted into"

    def test_cancelling_moves_the_generation_on(self, grid):
        before = grid._generation
        grid.cancel_pending()
        assert grid._generation != before

    def test_destroy_still_cancels(self, grid):
        before = grid._generation
        grid.destroy()
        assert grid._generation != before


def _returns(value):
    """`_fetch` returns `(path, retryable, retry_after)`."""

    async def _fetch(_url):
        return value, False, None

    return _fetch


class TestPacing:
    """The rate limiter is the AVATAR endpoint's, which is limited per client
    against a large catalogue. A library thumbnail is a signed object on storage,
    throttled by nothing, and putting a screenful of them through a 1.6/s bucket
    made a page take a quarter of a minute to fill.
    """

    def test_a_grid_cell_does_not_wait_for_a_token(self, grid, monkeypatch, tmp_path):
        waited: list = []

        async def _take_token():
            waited.append(True)

        monkeypatch.setattr(grid, "_take_token", _take_token)
        monkeypatch.setattr(
            "rundiffusion_omniverse.asset_grid.download_async",
            _answering(b"bytes"),
        )
        monkeypatch.setattr(
            "rundiffusion_omniverse.asset_grid._write_as_png", lambda _d, _p: True
        )

        asyncio.new_event_loop().run_until_complete(
            grid._fetch("https://storage/thumb.webp", cache_key="thumb-a", paced=False)
        )

        assert waited == []

    def test_an_avatar_still_waits_for_one(self, grid, monkeypatch):
        """The budget still has to be respected where it applies, or a browse
        through the catalogue ends in a blackout rather than in pacing."""
        waited: list = []

        async def _take_token():
            waited.append(True)

        monkeypatch.setattr(grid, "_take_token", _take_token)
        monkeypatch.setattr(
            "rundiffusion_omniverse.asset_grid.download_async",
            _answering(b"bytes"),
        )
        monkeypatch.setattr(
            "rundiffusion_omniverse.asset_grid._write_as_png", lambda _d, _p: True
        )

        asyncio.new_event_loop().run_until_complete(
            grid._fetch("https://api/avatars/a.png")
        )

        assert waited == [True]


def _answering(data: bytes):
    async def _download(_url):
        return data

    return _download
