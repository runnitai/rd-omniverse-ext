"""A browsing surface in its own window rather than in the tab strip.

Customer feedback asked for it for the library: choosing an image and using it were
two tabs apart, and a wide monitor had no way to put them side by side. Uploads
is the same sentence about the same problem, so both can be torn out and the
machinery is keyed by surface rather than written twice.

The panel was built on the assumption that exactly one tab is alive at a time.
Tearing a surface out inverts that, and the places it bites are what this file
is about.

**A tab switch destroys the tab body.** While a surface is in a window, that
switch must not drop the frames the window is still drawing into, nor cancel
the rebuilds booked against them. It must still drop the ones that ARE dying.

**One AssetGrid drives one live grid.** It remembers the grid widget the last
build made so Load more can append to it, and cancelling abandons every
thumbnail in the air. Sharing one between two surfaces on screen together would
mean opening one emptied the other.

**A selection belongs to a surface.** Clicking an upload while the library
window is open must not redraw the library's action row with an upload in it.

**The windows outlive the panel's own screens.** Signing out or changing
account replaces what is in the panel and reaches nothing in a separate window,
so both have to say what happens to every window explicitly.
"""

from __future__ import annotations

import asyncio

import pytest

from rundiffusion_omniverse import prefs
from rundiffusion_omniverse.panel import (
    POPPABLE_SURFACES,
    SURFACE_LIBRARY,
    SURFACE_TABS,
    SURFACE_UPLOADS,
    TAB_CREATE,
    TAB_LIBRARY,
    TAB_UPLOADS,
    RunDiffusionPanel,
)

#: Run the shared behaviour against both, so neither drifts into being the one
#: that works.
BOTH = pytest.mark.parametrize("surface", POPPABLE_SURFACES)


class FakeWindow:
    def __init__(self) -> None:
        self.visible = False
        self.destroyed = 0
        self.visibility_fn = None

    def set_visibility_changed_fn(self, fn) -> None:
        self.visibility_fn = fn

    def destroy(self) -> None:
        self.destroyed += 1


class FakeGrid:
    def __init__(self) -> None:
        self.cancelled = 0

    def cancel_pending(self) -> None:
        self.cancelled += 1


class RecordingRedraws:
    """Redraws with the frame taken out, so a test needs no event loop.

    `booked` is the point of it: what proves a move was deferred out of the
    click or the visibility callback that asked for it.
    """

    def __init__(self) -> None:
        self.booked: list[str] = []

    def request(self, key: str, render) -> None:
        self.booked.append(key)
        render()


class _FakeSession:
    async def sign_out(self) -> None:
        return None


@pytest.fixture(autouse=True)
def clean_prefs(monkeypatch):
    """The fallback store, so a test never writes a real Kit setting."""
    monkeypatch.setattr(prefs, "_settings", lambda: None)
    prefs._FALLBACK.clear()
    yield
    prefs._FALLBACK.clear()


def _panel(torn_out: dict | None = None) -> RunDiffusionPanel:
    panel = object.__new__(RunDiffusionPanel)
    panel._torn_out = dict(torn_out or {s: False for s in POPPABLE_SURFACES})
    panel._windows = {}
    panel._surface_grids = {s: FakeGrid() for s in POPPABLE_SURFACES}
    panel._grid = FakeGrid()
    panel._redraws = RecordingRedraws()
    panel._selected_asset = {}
    panel._asset_actions_frame = {}
    panel._session_frame = object()
    panel._session_body = object()
    panel._library_frame = object()
    panel._library_pager = object()
    panel._filters_frame = object()
    panel._filters_body = object()
    panel._uploads_frame = object()
    panel._uploads_pager = object()
    panel._current_tab = TAB_LIBRARY
    panel.shown_tabs = []
    panel._show_tab = panel.shown_tabs.append
    panel.opened = []
    panel._open_window = panel.opened.append
    return panel


def _only(surface: str) -> dict:
    """Torn out, and the other surface left docked."""
    return {s: s == surface for s in POPPABLE_SURFACES}


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


class TestWhichSurfacesTheTabBodyOwns:
    def test_docked_they_both_are(self):
        panel = _panel()

        assert set(panel._docked_surfaces()) == {SURFACE_LIBRARY, SURFACE_UPLOADS}

    @BOTH
    def test_a_torn_out_one_is_not(self, surface):
        """It survives the tab switch, and so does what is selected in it."""
        panel = _panel(_only(surface))

        assert surface not in panel._docked_surfaces()

    @BOTH
    def test_the_other_one_still_is(self, surface):
        """They are independent. Popping the library out must not stop the tab
        switch from tidying up after uploads."""
        panel = _panel(_only(surface))
        other = next(s for s in POPPABLE_SURFACES if s != surface)

        assert other in panel._docked_surfaces()

    def test_both_out_leaves_the_tab_body_owning_nothing(self):
        panel = _panel({s: True for s in POPPABLE_SURFACES})

        assert panel._docked_surfaces() == ()


class TestWhichRedrawsATabSwitchSpares:
    def test_a_layout_move_is_always_spared(self):
        """One of them is the request to tear a surface OUT, booked while it is
        still docked. A tab switch landing in that one frame used to drop it,
        and the Pop out button simply did nothing: no window, no message, no
        preference written."""
        spared = _panel()._torn_out_prefixes()

        assert set(spared) == {"library:layout", "uploads:layout"}

    @BOTH
    def test_a_torn_out_surface_keeps_its_own(self, surface):
        assert f"{surface}:" in _panel(_only(surface))._torn_out_prefixes()

    @BOTH
    def test_a_docked_surface_does_not(self, surface):
        """Its widgets die with the tab body like everything else."""
        other = next(s for s in POPPABLE_SURFACES if s != surface)

        assert f"{other}:" not in _panel(_only(surface))._torn_out_prefixes()

    def test_both_out_keeps_both(self):
        panel = _panel({s: True for s in POPPABLE_SURFACES})

        assert {"library:", "uploads:"} <= set(panel._torn_out_prefixes())


class TestTearingItOut:
    @BOTH
    def test_it_is_remembered(self, surface):
        """A statement about how someone works rather than a per-session
        choice. Being handed the tabbed layout again each morning is the same
        as not having the feature."""
        panel = _panel()

        panel._tear_out(surface)

        assert prefs.get_bool(prefs.torn_out_key(surface)) is True
        assert panel._torn_out[surface] is True

    @BOTH
    def test_the_window_is_opened(self, surface):
        panel = _panel()

        panel._tear_out(surface)

        assert panel.opened == [surface]

    @BOTH
    def test_the_tab_lets_go_of_the_widgets_it_had(self, surface):
        """They are going away whether or not it is the tab on screen, and the
        window is about to build a second set."""
        panel = _panel()

        panel._tear_out(surface)

        assert panel._surface_grids[surface].cancelled == 1

    def test_the_library_lets_go_of_its_own_frames(self):
        panel = _panel()

        panel._tear_out(SURFACE_LIBRARY)

        assert panel._library_frame is None
        assert panel._session_body is None
        assert panel._uploads_frame is not None

    def test_uploads_lets_go_of_its_own_and_leaves_the_librarys_alone(self):
        panel = _panel()

        panel._tear_out(SURFACE_UPLOADS)

        assert panel._uploads_frame is None
        assert panel._uploads_pager is None
        assert panel._library_frame is not None

    @BOTH
    def test_its_tab_is_rebuilt_when_that_is_the_one_showing(self, surface):
        """Into the note saying where it went. Left alone, it would keep
        showing widgets that have just been let go of."""
        panel = _panel()
        panel._current_tab = SURFACE_TABS[surface]

        panel._tear_out(surface)

        assert panel.shown_tabs == [SURFACE_TABS[surface]]

    @BOTH
    def test_another_tab_is_left_where_it_is(self, surface):
        """Popping a surface out is not a request to stop looking at the
        Create form."""
        panel = _panel()
        panel._current_tab = TAB_CREATE

        panel._tear_out(surface)

        assert panel.shown_tabs == []

    def test_popping_one_out_does_not_rebuild_the_other_tab(self):
        panel = _panel()
        panel._current_tab = TAB_UPLOADS

        panel._tear_out(SURFACE_LIBRARY)

        assert panel.shown_tabs == []


class TestPuttingItBack:
    @BOTH
    def test_it_is_remembered(self, surface):
        panel = _panel(_only(surface))
        prefs.set_bool(prefs.torn_out_key(surface), True)
        panel._close_window = lambda _s: None

        panel._dock(surface)

        assert prefs.get_bool(prefs.torn_out_key(surface)) is False

    @BOTH
    def test_the_window_is_closed(self, surface):
        panel = _panel(_only(surface))
        closed: list = []
        panel._close_window = closed.append

        panel._dock(surface)

        assert closed == [surface]

    @BOTH
    def test_its_tab_is_shown(self, surface):
        """Docking is a request to look at it, and the tab is where it now
        is."""
        panel = _panel(_only(surface))
        panel._close_window = lambda _s: None

        panel._dock(surface)

        assert panel.shown_tabs == [SURFACE_TABS[surface]]


class TestPuttingItBackWithoutBeingTakenToIt:
    """Docking is two different requests depending on who asked.

    Pressing "Bring it back into this tab" IS a request to look at the surface.
    Closing the window with its title-bar X is a request to get it off the
    screen, and answering that by jumping someone off the Create tab they were
    typing in onto the tab they had just tried to be rid of is the opposite of
    what they asked for.
    """

    @BOTH
    def test_the_x_leaves_another_tab_where_it_is(self, surface):
        panel = _panel(_only(surface))
        panel._current_tab = TAB_CREATE
        panel._close_window = lambda _s: None

        panel._dock(surface, show_it=False)

        assert panel.shown_tabs == []

    @BOTH
    def test_the_x_still_rebuilds_its_tab_when_that_is_the_one_showing(
        self, surface
    ):
        """It is showing the note saying the surface is in its own window, and
        that note has just stopped being true."""
        panel = _panel(_only(surface))
        panel._current_tab = SURFACE_TABS[surface]
        panel._close_window = lambda _s: None

        panel._dock(surface, show_it=False)

        assert panel.shown_tabs == [SURFACE_TABS[surface]]

    @BOTH
    def test_the_x_still_docks_it(self, surface):
        panel = _panel(_only(surface))
        prefs.set_bool(prefs.torn_out_key(surface), True)
        panel._current_tab = TAB_CREATE
        panel._close_window = lambda _s: None

        panel._dock(surface, show_it=False)

        assert panel._torn_out[surface] is False
        assert prefs.get_bool(prefs.torn_out_key(surface)) is False


class TestClosingTheWindowWithItsOwnX:
    @BOTH
    def test_it_docks_that_surface(self, surface):
        """The same request as pressing Bring it back, minus being taken to the
        tab. A window the user has shut must not leave the tab claiming the
        surface is somewhere else."""
        panel = _panel(_only(surface))
        docked: list = []
        panel._dock = lambda s, **kwargs: docked.append((s, kwargs))

        panel._on_window_visibility(surface, False)

        assert docked == [(surface, {"show_it": False})]

    @BOTH
    def test_it_waits_for_the_next_frame(self, surface):
        """This IS the window's own visibility callback, so docking inline
        would call destroy() on the window whose dispatch is still on the
        stack, and rebuild a tab body underneath it."""
        panel = _panel(_only(surface))
        panel._dock = lambda _s, **_kwargs: None

        panel._on_window_visibility(surface, False)

        assert panel._redraws.booked == [f"{surface}:layout"]

    @BOTH
    def test_becoming_visible_does_nothing(self, surface):
        panel = _panel(_only(surface))
        docked: list = []
        panel._dock = lambda s, **kwargs: docked.append(s)

        panel._on_window_visibility(surface, True)

        assert docked == []

    @BOTH
    def test_a_window_going_away_after_docking_does_not_dock_twice(self, surface):
        """`_dock` closes the window, which reports a visibility change on the
        way out."""
        panel = _panel()
        docked: list = []
        panel._dock = lambda s, **kwargs: docked.append(s)

        panel._on_window_visibility(surface, False)

        assert docked == []


class TestLettingGoOfTheWindow:
    @BOTH
    def test_closing_it_drops_the_frames_and_destroys_it(self, surface):
        panel = _panel(_only(surface))
        window = FakeWindow()
        panel._windows[surface] = window

        panel._close_window(surface)

        assert window.destroyed == 1
        assert panel._windows.get(surface) is None
        assert panel._surface_grids[surface].cancelled == 1

    @BOTH
    def test_it_stops_reporting_visibility_before_being_destroyed(self, surface):
        """Destroying a visible window reports a visibility change, and
        answering it would dock the surface a second time."""
        panel = _panel(_only(surface))
        window = FakeWindow()
        window.set_visibility_changed_fn(lambda _visible: None)
        panel._windows[surface] = window

        panel._close_window(surface)

        assert window.visibility_fn is None

    @BOTH
    def test_closing_one_that_has_no_window_is_harmless(self, surface):
        panel = _panel()

        panel._close_window(surface)

        assert panel._windows.get(surface) is None

    def test_closing_one_leaves_the_other_window_alone(self):
        panel = _panel({s: True for s in POPPABLE_SURFACES})
        windows = {s: FakeWindow() for s in POPPABLE_SURFACES}
        panel._windows.update(windows)

        panel._close_window(SURFACE_LIBRARY)

        assert windows[SURFACE_UPLOADS].destroyed == 0
        assert panel._windows.get(SURFACE_UPLOADS) is windows[SURFACE_UPLOADS]


class TestTheGridsStayApart:
    @BOTH
    def test_dropping_one_cancels_only_its_own(self, surface):
        """Opening Uploads must not empty a library that is sitting in its own
        window with a screenful of thumbnails still arriving."""
        panel = _panel()
        other = next(s for s in POPPABLE_SURFACES if s != surface)

        panel._drop_surface_frames(surface)

        assert panel._surface_grids[surface].cancelled == 1
        assert panel._surface_grids[other].cancelled == 0

    @BOTH
    def test_a_surface_draws_through_its_own_grid(self, surface):
        panel = _panel()

        assert panel._grid_for(surface) is panel._surface_grids[surface]

    def test_each_surface_has_a_grid_of_its_own(self):
        panel = _panel()

        assert panel._grid_for(SURFACE_LIBRARY) is not panel._grid_for(SURFACE_UPLOADS)

    @BOTH
    def test_none_of_them_is_the_panels_general_fetcher(self, surface):
        """`_grid` serves the Create tab's chosen-image previews, which die
        with the tab body. That is exactly the wrong lifetime for a surface
        sitting in a window of its own."""
        panel = _panel()

        assert panel._grid_for(surface) is not panel._grid


class TestASelectionBelongsToOneSurface:
    def test_clicking_in_one_does_not_disturb_the_other(self):
        panel = _panel(_only(SURFACE_LIBRARY))
        redrawn: list = []
        panel._render_asset_actions = redrawn.append

        panel._on_asset_clicked("library-image", SURFACE_LIBRARY)
        panel._on_asset_clicked("an-upload", SURFACE_UPLOADS)

        assert panel._selected_asset[SURFACE_LIBRARY] == "library-image"
        assert panel._selected_asset[SURFACE_UPLOADS] == "an-upload"
        assert redrawn == [SURFACE_LIBRARY, SURFACE_UPLOADS]


class TestShowingASurfaceAgainWhereverItLives:
    """Dropping what a surface HOLDS redraws nothing by itself. Changing
    account, and sending a render to uploads, both drop held pages, and both
    have to reach the surface in whichever place it is."""

    def _panel_for(self, torn_out: dict, *, current_tab: int):
        panel = _panel(torn_out)
        panel._current_tab = current_tab
        for surface, out in torn_out.items():
            if out:
                panel._windows[surface] = FakeWindow()
        return panel

    @BOTH
    def test_a_torn_out_surface_is_rebuilt_into_its_window(self, surface):
        panel = self._panel_for(_only(surface), current_tab=TAB_CREATE)

        panel._rebuild_surface(surface)

        assert panel.opened == [surface]
        assert panel.shown_tabs == []

    @BOTH
    def test_a_docked_surface_on_screen_is_rebuilt_by_its_tab(self, surface):
        """The case that was missed. A user sitting on the Library tab when the
        account changed kept the old account's grid, with in-cell Save and Open
        still live on it."""
        panel = self._panel_for(
            {s: False for s in POPPABLE_SURFACES}, current_tab=SURFACE_TABS[surface]
        )

        panel._rebuild_surface(surface)

        assert panel.shown_tabs == [SURFACE_TABS[surface]]

    @BOTH
    def test_a_docked_surface_on_another_tab_is_left_alone(self, surface):
        """Its body is built fresh on the next visit anyway."""
        panel = self._panel_for(
            {s: False for s in POPPABLE_SURFACES}, current_tab=TAB_CREATE
        )

        panel._rebuild_surface(surface)

        assert panel.shown_tabs == []
        assert panel.opened == []

    @BOTH
    def test_a_surface_torn_out_with_no_window_yet_is_left_alone(self, surface):
        """Torn out is remembered across sessions, so the preference can say
        yes before the workbench has built anything."""
        panel = _panel(_only(surface))
        panel._current_tab = TAB_CREATE

        panel._rebuild_surface(surface)

        assert panel.opened == []


class TestSigningOut:
    """The sign-in screen owns the panel's own window and reaches nothing in a
    window a surface was torn out into. Left open, that window goes on showing
    the grid it was built with, and its Use, Save and Open buttons go on
    working against signed URLs that outlive the session: the person who just
    signed out is still browsing the account they left.
    """

    def _signed_out_panel(self, torn_out: dict):
        panel = _panel(torn_out)
        for surface, out in torn_out.items():
            if out:
                panel._windows[surface] = FakeWindow()
        panel._session = _FakeSession()
        panel._tool_tags = {}
        panel._form_values = {}
        panel._image_state = {}
        panel._capture_state = {}
        panel._library_generation = 0
        panel._library_items = []
        panel._library_cursor = None
        panel._library_has_more = False
        panel._uploads_generation = 0
        panel._uploads_items = []
        panel._uploads_cursor = None
        panel._uploads_has_more = False
        panel._tools = []
        panel._kits = []
        panel._selected_tool_id = None
        panel._tool_detail = None
        panel.rendered = []
        panel._render_signed_out = lambda: panel.rendered.append("signed-out")
        panel.stopped: list = []
        panel._viewport_preview = type(
            "P", (), {"stop": lambda _s: panel.stopped.append("preview")}
        )()
        panel._camera_watch = type(
            "W", (), {"stop": lambda _s: panel.stopped.append("cameras")}
        )()
        return panel

    @BOTH
    def test_the_window_is_closed(self, surface):
        panel = self._signed_out_panel(_only(surface))
        window = panel._windows[surface]

        _run(panel._sign_out())

        assert window.destroyed == 1
        assert panel._windows.get(surface) is None

    def test_both_windows_are_closed(self):
        panel = self._signed_out_panel({s: True for s in POPPABLE_SURFACES})
        windows = dict(panel._windows)

        _run(panel._sign_out())

        assert [w.destroyed for w in windows.values()] == [1, 1]

    @BOTH
    def test_it_does_not_read_as_the_user_docking_it(self, surface):
        """Destroying a visible window reports a visibility change. Answering
        it would forget that the surface was torn out, and signing back in
        would hand them the tabbed layout they had moved away from."""
        panel = self._signed_out_panel(_only(surface))
        prefs.set_bool(prefs.torn_out_key(surface), True)

        _run(panel._sign_out())

        assert prefs.get_bool(prefs.torn_out_key(surface)) is True
        assert panel._torn_out[surface] is True

    def test_the_sign_in_screen_is_still_shown(self):
        panel = self._signed_out_panel(_only(SURFACE_LIBRARY))

        _run(panel._sign_out())

        assert panel.rendered == ["signed-out"]

    def test_it_stops_watching_the_stage_and_the_viewport(self):
        """The sign-in screen replaces the whole root, so nothing that would
        normally turn these off ever runs: no tab switch, no viewfinder render.
        Left alone, a globally registered USD listener goes on walking the stage
        on every prim edit for the rest of the session, redrawing image rows
        that were destroyed with the workbench."""
        panel = self._signed_out_panel(_only(SURFACE_LIBRARY))

        _run(panel._sign_out())

        assert sorted(panel.stopped) == ["cameras", "preview"]
