"""A cached image is never seen half written.

Seen in a real session: the console logged "Failed to read texture file ...
or file is empty" for a tool avatar while the avatar URL itself served the
whole picture, and the tile stayed blank.

The panel's grids share one cache folder, so two of them can fetch the same
avatar at once. The texture loader reads a file on its own thread after an
image widget names it, and the second fetch used to save over the same name in
place, truncating the file while the loader was still reading the first copy.

So an image is written beside its name and moved into place, and a fetch that
finds the picture already stored by someone else uses that copy.
"""

from __future__ import annotations

import asyncio
import io
import os

import pytest
from PIL import Image

from rundiffusion_omniverse import asset_grid
from rundiffusion_omniverse.asset_grid import AssetGrid, _write_as_png


def _png_bytes(color=(200, 30, 30, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (4, 4), color).save(out, format="PNG")
    return out.getvalue()


def _webp_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 200, 10)).save(out, format="WEBP")
    return out.getvalue()


def _leftovers(folder):
    return sorted(p.name for p in folder.iterdir() if p.suffix == ".part")


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


class TestWrite:
    def test_it_writes_a_png_the_loader_can_read(self, tmp_path):
        target = tmp_path / "img-avatar.png"

        assert _write_as_png(_webp_bytes(), target) is True

        with Image.open(target) as written:
            assert written.format == "PNG"
        assert _leftovers(tmp_path) == []

    def test_the_target_name_is_untouched_until_the_picture_is_whole(
        self, tmp_path, monkeypatch
    ):
        """The bug itself: a reader that opens the name mid-save must not find
        an empty or partial file there."""
        target = tmp_path / "img-avatar.png"
        data = _png_bytes()
        seen_during_save = []
        real_save = Image.Image.save

        def watching_save(image, fp, *args, **kwargs):
            seen_during_save.append(target.exists())
            return real_save(image, fp, *args, **kwargs)

        monkeypatch.setattr(Image.Image, "save", watching_save)

        assert _write_as_png(data, target) is True
        assert seen_during_save == [False]
        assert target.stat().st_size > 0

    def test_a_second_write_replaces_the_file_whole(self, tmp_path):
        target = tmp_path / "img-avatar.png"
        _write_as_png(_png_bytes((1, 1, 1, 255)), target)

        assert _write_as_png(_png_bytes((9, 9, 9, 255)), target) is True

        with Image.open(target) as written:
            assert written.getpixel((0, 0)) == (9, 9, 9, 255)
        assert _leftovers(tmp_path) == []

    def test_a_file_held_open_by_the_loader_keeps_its_copy(self, tmp_path, monkeypatch):
        """Windows refuses to replace a file another handle has open. The copy
        already there is this same picture, so the cell keeps it."""
        target = tmp_path / "img-avatar.png"
        _write_as_png(_png_bytes((1, 1, 1, 255)), target)
        before = target.read_bytes()

        def locked(_src, _dst):
            raise PermissionError(13, "The process cannot access the file")

        monkeypatch.setattr(asset_grid.os, "replace", locked)

        assert _write_as_png(_png_bytes((9, 9, 9, 255)), target) is True
        assert target.read_bytes() == before
        assert _leftovers(tmp_path) == []

    def test_a_good_picture_that_could_not_be_stored_is_worth_asking_again(
        self, tmp_path, monkeypatch
    ):
        """A scanner or indexer can hold a brand-new file for a moment. The
        picture was fine, so the cell is asked for again rather than left blank
        for good, which is what a bad image gets."""
        target = tmp_path / "img-avatar.png"

        def held(_src, _dst):
            raise PermissionError(13, "held by another process")

        monkeypatch.setattr(asset_grid.os, "replace", held)

        assert _write_as_png(_png_bytes(), target) is None
        assert not target.exists()
        assert _leftovers(tmp_path) == []

    def test_the_fetch_reports_that_as_retryable(self, tmp_path, monkeypatch):
        grid = AssetGrid(tmp_path)

        async def download(_url):
            return _png_bytes()

        def held(_src, _dst):
            raise PermissionError(13, "held by another process")

        monkeypatch.setattr(asset_grid, "download_async", download)
        monkeypatch.setattr(asset_grid.os, "replace", held)

        path, retryable, _wait = _run(grid._fetch("https://example.test/a.png", paced=False))

        assert path is None
        assert retryable is True

    def test_bad_bytes_are_a_settled_answer(self, tmp_path, monkeypatch):
        grid = AssetGrid(tmp_path)

        async def download(_url):
            return b"<html>not an image</html>"

        monkeypatch.setattr(asset_grid, "download_async", download)

        path, retryable, _wait = _run(grid._fetch("https://example.test/a.png", paced=False))

        assert path is None
        assert retryable is False

    def test_undecodable_bytes_leave_nothing_behind(self, tmp_path):
        target = tmp_path / "img-avatar.png"

        assert _write_as_png(b"<html>not an image</html>", target) is False
        assert list(tmp_path.iterdir()) == []


class TestRacingFetches:
    def test_a_fetch_that_finishes_second_uses_the_copy_already_stored(
        self, tmp_path, monkeypatch
    ):
        """Two grids asked for the same avatar. The one that lands second
        must not write over the file the first one already handed to the
        loader."""
        grid = AssetGrid(tmp_path)
        url = "https://example.test/avatars/tool-1.png"
        name = asset_grid._safe(url)[-60:]
        stored = tmp_path / f"img-{name}.png"

        async def download_while_another_grid_finishes(_url):
            _write_as_png(_png_bytes((1, 1, 1, 255)), stored)
            return _png_bytes((9, 9, 9, 255))

        monkeypatch.setattr(asset_grid, "download_async", download_while_another_grid_finishes)
        writes = []
        real_write = asset_grid._write_as_png
        monkeypatch.setattr(
            asset_grid,
            "_write_as_png",
            lambda data, path: writes.append(path) or real_write(data, path),
        )

        path, retryable, _wait = _run(grid._fetch(url, paced=False))

        assert path == stored
        assert retryable is False
        assert writes == []
        with Image.open(stored) as kept:
            assert kept.getpixel((0, 0)) == (1, 1, 1, 255)

    def test_a_lone_fetch_still_stores_what_it_downloaded(self, tmp_path, monkeypatch):
        grid = AssetGrid(tmp_path)

        async def download(_url):
            return _webp_bytes()

        monkeypatch.setattr(asset_grid, "download_async", download)

        path, _retryable, _wait = _run(
            grid._fetch("https://example.test/thumb.webp", cache_key="thumb-a:b", paced=False)
        )

        assert path == tmp_path / "img-thumb-a-b.png"
        with Image.open(path) as written:
            assert written.format == "PNG"
        assert _leftovers(tmp_path) == []


@pytest.mark.skipif(os.name != "nt", reason="the sharing rule is a Windows one")
def test_a_real_open_handle_does_not_lose_the_cell(tmp_path):
    """The same as the monkeypatched case, against the real operating system:
    Python opens files without delete sharing, as the loader may."""
    target = tmp_path / "img-avatar.png"
    _write_as_png(_png_bytes((1, 1, 1, 255)), target)

    with open(target, "rb"):
        assert _write_as_png(_png_bytes((9, 9, 9, 255)), target) is True

    # The replace was refused while the handle was open, so the copy already
    # there is the one kept.
    with Image.open(target) as written:
        assert written.getpixel((0, 0)) == (1, 1, 1, 255)
    assert _leftovers(tmp_path) == []
