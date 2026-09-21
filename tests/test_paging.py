"""Going deeper into a history, rather than looking at the first page of it.

The library and uploads endpoints are cursor-paginated: each page hands out a
cursor for the NEXT one and nothing at all for the previous one, and neither
publishes a total. An earlier draft turned that into Back and More buttons that
REPLACED the grid, which is technically a way through a history and practically
not one: the panel showed two dozen images, and looking at the two dozen before
them meant losing the two dozen you had.

So paging accumulates. `Load more` appends the next page under what is on
screen, the items are held on the panel so a tab switch does not undo it, and
the grid appends into the widget it already built rather than being rebuilt.

These cover the two halves of that: the merge that decides what is new, and the
grid's append.
"""

from __future__ import annotations

import asyncio
import types

import omni.ui as ui
import pytest

from rundiffusion_omniverse.api.assets import Asset, LibraryFilters
from rundiffusion_omniverse.api.transport import ApiError
from rundiffusion_omniverse.asset_grid import AssetGrid
from rundiffusion_omniverse.panel import RunDiffusionPanel


def _asset(identifier: str) -> Asset:
    """An asset with no picture, so building a cell spawns no fetch."""
    return Asset(id=identifier, kind="LIBRARY_REF", url=None, thumb_url=None)


class TestMergingAPage:
    def test_an_arriving_page_goes_under_what_is_held(self):
        merged = RunDiffusionPanel._merge_page([_asset("a")], [_asset("b")])

        assert [item.id for item in merged] == ["a", "b"]

    def test_a_repeated_row_is_not_shown_twice(self):
        """Keyset pages can overlap: two results generated in the same instant
        order arbitrarily against each other, so the last row of one page can
        also be the first row of the next."""
        merged = RunDiffusionPanel._merge_page(
            [_asset("a"), _asset("b")], [_asset("b"), _asset("c")]
        )

        assert [item.id for item in merged] == ["a", "b", "c"]

    def test_a_page_of_nothing_new_leaves_the_grid_alone(self):
        held = [_asset("a")]

        merged = RunDiffusionPanel._merge_page(held, [_asset("a")])

        assert [item.id for item in merged] == ["a"]

    def test_the_held_list_is_not_mutated(self):
        """The caller compares lengths before and after to know what arrived,
        which only works if the old list is still the old list."""
        held = [_asset("a")]

        RunDiffusionPanel._merge_page(held, [_asset("b")])

        assert [item.id for item in held] == ["a"]


class FakeContainer:
    """Enough of an omni.ui container to record what was put in it."""

    def __init__(self) -> None:
        self.cleared = 0
        self.entered = 0

    def clear(self) -> None:
        self.cleared += 1

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *_exc) -> None:
        return None


@pytest.fixture(autouse=True)
def drawable_ui(monkeypatch):
    """The conftest stub is bare; a grid needs the names this path builds with."""
    grids: list[FakeContainer] = []

    def _grid(*_args, **_kwargs) -> FakeContainer:
        made = FakeContainer()
        grids.append(made)
        return made

    monkeypatch.setattr(ui, "VGrid", _grid, raising=False)
    monkeypatch.setattr(ui, "VStack", lambda *a, **k: FakeContainer(), raising=False)
    monkeypatch.setattr(ui, "Frame", lambda *a, **k: FakeContainer(), raising=False)
    monkeypatch.setattr(ui, "Label", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        ui,
        "Alignment",
        types.SimpleNamespace(CENTER=object()),
        raising=False,
    )
    return grids


@pytest.fixture
def grid(tmp_path):
    return AssetGrid(tmp_path)


class TestAppending:
    def test_a_first_build_clears_the_container(self, grid):
        container = FakeContainer()

        grid.build(container, [_asset("a")])

        assert container.cleared == 1

    def test_appending_leaves_what_is_on_screen(self, grid, drawable_ui):
        """The point of Load more: the page you were looking at stays put, and
        with it the scroll position that got you there."""
        container = FakeContainer()
        grid.build(container, [_asset("a")])

        grid.build(container, [_asset("b")], append=True)

        assert container.cleared == 1
        # Both pages went into the one grid widget the first build made.
        assert len(drawable_ui) == 1
        assert drawable_ui[0].entered == 2

    def test_appending_with_nothing_built_yet_builds(self, grid):
        """A Load more that somehow arrives first must still draw something,
        rather than silently dropping the page it was handed."""
        container = FakeContainer()

        grid.build(container, [_asset("a")], append=True)

        assert container.cleared == 1

    def test_a_rebuild_after_cancelling_does_not_append_into_a_dead_grid(
        self, grid, drawable_ui
    ):
        """`cancel_pending` runs when the widgets are being thrown away, so the
        grid it was filling is gone and a later append must start a fresh one
        rather than write into a destroyed widget."""
        container = FakeContainer()
        grid.build(container, [_asset("a")])

        grid.cancel_pending()
        grid.build(container, [_asset("b")], append=True)

        assert len(drawable_ui) == 2

    def test_an_empty_first_page_says_so(self, grid, monkeypatch):
        container = FakeContainer()
        said: list = []
        monkeypatch.setattr(
            ui, "Label", lambda text, **_k: said.append(text), raising=False
        )

        grid.build(container, [], empty_message="Nothing matches those filters.")

        assert said == ["Nothing matches those filters."]

    def test_an_empty_page_appended_leaves_the_grid_alone(self, grid, monkeypatch):
        """The server can answer a cursor with an empty page. Saying "nothing
        here" over a grid holding six pages would be a lie about all six."""
        container = FakeContainer()
        grid.build(container, [_asset("a")])
        said: list = []
        monkeypatch.setattr(
            ui, "Label", lambda text, **_k: said.append(text), raising=False
        )

        grid.build(container, [], empty_message="Nothing here yet.", append=True)

        assert said == []
        assert container.cleared == 1


class FakePage:
    def __init__(self, items, next_cursor=None, has_more=False) -> None:
        self.items = items
        self.next_cursor = next_cursor
        self.has_more = has_more


class _Panel:
    """A panel with only what the two load methods touch.

    Built by hand rather than constructed: `RunDiffusionPanel.__init__` makes a
    window, a job queue and three dialogs, none of which paging has an opinion
    about.
    """

    def __init__(self) -> None:
        panel = object.__new__(RunDiffusionPanel)
        panel._library_items = []
        panel._library_cursor = None
        panel._library_has_more = False
        panel._library_loading = False
        panel._library_generation = 0
        panel._library_pager = None
        panel._library_frame = None
        panel._library_filters = LibraryFilters()
        # Keyed by surface since the library can be torn out into its own
        # window and the two grids each hold their own selection.
        panel._selected_asset = {}
        panel._asset_actions_frame = {}
        panel._session = object()
        # Takes the surface it is redrawing now that there are two of them.
        panel._render_asset_actions = lambda _surface: None
        panel._render_pager = lambda *a, **k: None
        panel._render_library_error = lambda message: self.errors.append(message)
        panel._show_library_items = lambda assets, append: self.shown.append(
            ([asset.id for asset in assets], append)
        )
        self.errors: list = []
        self.shown: list = []
        self.panel = panel


@pytest.fixture
def pages(monkeypatch):
    """`assets_api.list_library` answering from a queue, so a test can control
    the order two in-flight loads come back in."""
    from rundiffusion_omniverse import panel as panel_module

    answers: list = []

    async def _list_library(_session, _limit=24, *, cursor=None, filters=None):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(panel_module.assets_api, "list_library", _list_library)
    return answers


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


class TestLoadingAPage:
    def test_the_first_page_replaces_and_the_next_appends(self, pages):
        harness = _Panel()
        pages.append(FakePage([_asset("a")], next_cursor="c2", has_more=True))
        pages.append(FakePage([_asset("b")], next_cursor=None, has_more=False))

        _run(harness.panel._load_library(reset=True))
        _run(harness.panel._load_library(reset=False))

        assert harness.shown == [(["a"], False), (["b"], True)]
        assert [item.id for item in harness.panel._library_items] == ["a", "b"]

    def test_a_repeated_row_is_not_appended_twice(self, pages):
        harness = _Panel()
        pages.append(FakePage([_asset("a")], next_cursor="c2", has_more=True))
        pages.append(FakePage([_asset("a"), _asset("b")], has_more=False))

        _run(harness.panel._load_library(reset=True))
        _run(harness.panel._load_library(reset=False))

        assert harness.shown[-1] == (["b"], True)

    def test_more_is_offered_only_when_there_is_a_cursor_to_follow(self, pages):
        """`has_more` without a cursor is a page that cannot be asked for, and
        an enabled button that cannot act is worse than a disabled one."""
        harness = _Panel()
        pages.append(FakePage([_asset("a")], next_cursor=None, has_more=True))

        _run(harness.panel._load_library(reset=True))

        assert harness.panel._library_has_more is False

    def test_a_second_load_more_during_one_is_ignored(self, pages):
        """Two clicks would spend the same cursor twice and land the same page
        twice."""
        harness = _Panel()
        harness.panel._library_loading = True

        _run(harness.panel._load_library(reset=False))

        assert harness.shown == []

    def test_a_reset_is_never_dropped_for_a_load_in_flight(self, pages):
        """Applying a filter while the first page is still loading has to
        start the new query. Refusing it is how Apply came to do nothing when
        it was pressed early."""
        harness = _Panel()
        harness.panel._library_loading = True
        pages.append(FakePage([_asset("a")]))

        _run(harness.panel._load_library(reset=True))

        assert harness.shown == [(["a"], False)]

    def test_a_page_from_a_superseded_query_does_not_land(self, pages, monkeypatch):
        """The old query's answer arrives after a new one has started. Landing
        it would put the unfiltered results under the filtered ones."""
        from rundiffusion_omniverse import panel as panel_module

        harness = _Panel()

        async def _answer_late(_session, _limit=24, *, cursor=None, filters=None):
            # Apply was pressed while this request was in the air.
            harness.panel._library_generation += 1
            return FakePage([_asset("old")])

        monkeypatch.setattr(panel_module.assets_api, "list_library", _answer_late)

        _run(harness.panel._load_library(reset=True))

        assert harness.shown == []
        assert harness.panel._library_items == []

    def test_a_failed_first_page_says_so(self, pages):
        harness = _Panel()
        pages.append(ApiError(503, message="Service unavailable."))

        _run(harness.panel._load_library(reset=True))

        assert harness.errors == ["Could not load your library. Service unavailable."]

    def test_a_failed_later_page_keeps_what_is_on_screen(self, pages):
        harness = _Panel()
        pages.append(FakePage([_asset("a")], next_cursor="c2", has_more=True))
        pages.append(ApiError(503, message="Service unavailable."))

        _run(harness.panel._load_library(reset=True))
        _run(harness.panel._load_library(reset=False))

        assert [item.id for item in harness.panel._library_items] == ["a"]
        assert harness.errors == ["Could not load your library. Service unavailable."]


class TestWhatThePagerIsTold:
    """The pager is rendered from the panel's state, not from the caller's idea
    of it. The tab is rebuilt from held items whenever it is shown, which can
    happen while a page is still in the air, and a Load more that looks enabled
    but is dropped on click is worse than one that is visibly waiting."""

    class _Grid:
        def build(self, *_args, **_kwargs) -> None:
            return None

        def cancel_pending(self) -> None:
            return None

    def _panel_with_recording_pager(self):
        harness = _Panel()
        told: list = []
        harness.panel._render_pager = lambda *_a, **kwargs: told.append(kwargs)
        # The real renderer, so what it is told comes from the panel's state
        # rather than from the test.
        del harness.panel._show_library_items
        harness.panel._library_frame = FakeContainer()
        # Each browsing surface has its own grid, so that opening one cannot
        # cancel the other's half-loaded thumbnails while it sits in a window.
        harness.panel._surface_grids = {
            "library": self._Grid(),
            "uploads": self._Grid(),
        }
        return harness, told

    def test_a_redraw_during_a_load_reports_it(self):
        harness, told = self._panel_with_recording_pager()
        harness.panel._library_loading = True

        harness.panel._show_library_items([], append=False)

        assert told[-1]["loading"] is True

    def test_a_redraw_with_nothing_running_reports_that(self):
        harness, told = self._panel_with_recording_pager()
        harness.panel._library_loading = False

        harness.panel._show_library_items([], append=False)

        assert told[-1]["loading"] is False
