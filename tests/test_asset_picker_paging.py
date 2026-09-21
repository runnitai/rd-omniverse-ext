"""The image picker can reach past its first page, and can be reopened.

The picker behind "Add image..." showed the newest two dozen images and offered
no way past them, so choosing anything older meant leaving the panel for the
browser. It now pages like the tabs do.

The subtlety is the guard. Refusing a second Load more while one is in flight is
right, because two clicks spend the same cursor twice. Refusing a RESTART is
not: the window can be closed and reopened mid-request, and the source can be
switched before the first page lands, and both of those left it saying "Loading
your library..." forever.
"""

from __future__ import annotations

import asyncio

import pytest

from rundiffusion_omniverse import dialogs
from rundiffusion_omniverse.api.assets import Asset, AssetPage
from rundiffusion_omniverse.dialogs import AssetPicker


class FakeFrame:
    def clear(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


class FakeGrid:
    def __init__(self) -> None:
        self.builds: list = []
        self.cancels = 0

    def build(self, _body, assets, *, on_pick=None, empty_message="", append=False):
        self.builds.append(([asset.id for asset in assets], append))

    def cancel_pending(self) -> None:
        self.cancels += 1


def _asset(identifier: str, type_name: str = "IMG") -> Asset:
    return Asset(
        id=identifier, kind="LIBRARY_REF", url=None, thumb_url=None, type=type_name
    )


@pytest.fixture
def picker(monkeypatch):
    made = object.__new__(AssetPicker)
    made._session = object()
    made._grid = FakeGrid()
    made._source = "library"
    made._cursor = None
    made._has_more = False
    made._loading = False
    made._shown = 0
    made._generation = 0
    made._more_button = None
    made._status = None
    made._window = None
    monkeypatch.setattr(dialogs.ui, "Label", lambda *a, **k: None, raising=False)
    return made


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


def _page(items, next_cursor=None, has_more=False) -> AssetPage:
    return AssetPage(items=items, next_cursor=next_cursor, has_more=has_more)


class TestPagingThePicker:
    def test_load_more_appends_the_next_page(self, picker, monkeypatch):
        pages = [
            _page([_asset("a")], next_cursor="c2", has_more=True),
            _page([_asset("b")]),
        ]

        async def _list(_session, cursor=None):
            return pages.pop(0)

        monkeypatch.setattr(dialogs.assets_api, "list_library", _list)
        body = FakeFrame()

        _run(picker._load(body, "library", lambda _a: None))
        _run(picker._load(body, "library", lambda _a: None, more=True))

        assert picker._grid.builds == [(["a"], False), (["b"], True)]

    def test_a_second_load_more_during_one_is_ignored(self, picker):
        """Two clicks would spend the same cursor twice."""
        picker._loading = True

        _run(picker._load(FakeFrame(), "library", lambda _a: None, more=True))

        assert picker._grid.builds == []

    def test_switching_source_during_a_load_is_not_dropped(self, picker, monkeypatch):
        """From uploads pressed before the library landed. Refusing it left the
        window saying "Loading..." with nothing on the way."""
        picker._loading = True

        async def _list(_session, cursor=None):
            return _page([_asset("u")])

        monkeypatch.setattr(dialogs.assets_api, "list_uploads", _list)

        _run(picker._load(FakeFrame(), "uploads", lambda _a: None))

        assert picker._grid.builds == [(["u"], False)]
        assert picker._source == "uploads"

    def test_switching_source_starts_the_cursor_again(self, picker, monkeypatch):
        """A cursor is issued against one listing and means nothing against the
        other."""
        picker._cursor = "library-cursor"
        asked: list = []

        async def _list(_session, cursor=None):
            asked.append(cursor)
            return _page([_asset("u")])

        monkeypatch.setattr(dialogs.assets_api, "list_uploads", _list)

        _run(picker._load(FakeFrame(), "uploads", lambda _a: None))

        assert asked == [None]

    def test_a_page_for_the_old_source_does_not_land(self, picker, monkeypatch):
        """The library answer arrives after the user asked for uploads."""

        async def _late(_session, cursor=None):
            picker._generation += 1  # the source was switched mid-request
            return _page([_asset("stale")])

        monkeypatch.setattr(dialogs.assets_api, "list_library", _late)

        _run(picker._load(FakeFrame(), "library", lambda _a: None))

        assert picker._grid.builds == []

    def test_more_is_offered_only_when_there_is_a_cursor_to_follow(
        self, picker, monkeypatch
    ):
        async def _list(_session, cursor=None):
            return _page([_asset("a")], next_cursor=None, has_more=True)

        monkeypatch.setattr(dialogs.assets_api, "list_library", _list)

        _run(picker._load(FakeFrame(), "library", lambda _a: None))

        assert picker._has_more is False


class TestWhatThePickerOffers:
    """This window fills an image FIELD, which takes an IMG reference. A
    library video here is a cell whose only outcome is a rejected run."""

    def test_a_video_is_not_offered(self, picker, monkeypatch):
        async def _list(_session, cursor=None):
            return _page([_asset("a"), _asset("v", "VID")])

        monkeypatch.setattr(dialogs.assets_api, "list_library", _list)

        _run(picker._load(FakeFrame(), "library", lambda _a: None))

        assert picker._grid.builds == [(["a"], False)]

    def test_a_page_with_nothing_usable_still_offers_more(self, picker, monkeypatch):
        """Filtered client-side, because `/library` has no result type to ask
        with, so a page can arrive with nothing in it for this window. Ending
        the listing there would hide every image behind it."""

        async def _list(_session, cursor=None):
            return _page([_asset("v", "VID")], next_cursor="c2", has_more=True)

        monkeypatch.setattr(dialogs.assets_api, "list_library", _list)

        _run(picker._load(FakeFrame(), "library", lambda _a: None))

        assert picker._grid.builds == [([], False)]
        assert picker._has_more is True

    def test_a_page_with_nothing_usable_says_so(self, picker, monkeypatch):
        """Otherwise Load more reads as a button that does nothing: the grid
        does not change, the count does not change, and the only evidence
        anything happened is the button briefly greying out."""
        said: list = []
        picker._set_status = said.append

        async def _list(_session, cursor=None):
            return _page([_asset("v", "VID")], next_cursor="c2", has_more=True)

        monkeypatch.setattr(dialogs.assets_api, "list_library", _list)

        _run(picker._load(FakeFrame(), "library", lambda _a: None))

        assert said == ["No images on that page. Load more to keep looking."]

    def test_the_count_is_of_what_is_actually_shown(self, picker, monkeypatch):
        """Not of what the page held. The pages are filtered, so the two
        numbers are different on any library with a video in it."""
        said: list = []
        picker._set_status = said.append

        async def _list(_session, cursor=None):
            return _page([_asset("a"), _asset("v", "VID"), _asset("b")])

        monkeypatch.setattr(dialogs.assets_api, "list_library", _list)

        _run(picker._load(FakeFrame(), "library", lambda _a: None))

        assert said == ["Showing 2 images."]

    def test_switching_source_starts_the_count_again(self, picker, monkeypatch):
        said: list = []
        picker._set_status = said.append

        async def _library(_session, cursor=None):
            return _page([_asset("a"), _asset("b")])

        async def _uploads(_session, cursor=None):
            return _page([_asset("u")])

        monkeypatch.setattr(dialogs.assets_api, "list_library", _library)
        monkeypatch.setattr(dialogs.assets_api, "list_uploads", _uploads)
        body = FakeFrame()

        _run(picker._load(body, "library", lambda _a: None))
        _run(picker._load(body, "uploads", lambda _a: None))

        assert said[-1] == "Showing 1 image."
