"""Only the tool avatars near the visible area get fetched.

The catalogue holds hundreds of tools, so a browser that fetched every avatar
as it built the grid made hundreds of requests to show about a dozen pictures.
Cards now register their avatar and this decides which are worth asking for.

The window is deliberately generous: two rows' worth above and below, so a
normal scroll lands on pictures that are already there. Being wrong in that
direction costs a few requests; being wrong the other way shows blanks.
"""

from __future__ import annotations

import pytest

from rundiffusion_omniverse.dialogs import ToolBrowser


class FakeFrame:
    def __init__(self, y: float) -> None:
        self.screen_position_y = y


class FakeScroll:
    """A scroller occupying screen rows 100..500."""

    screen_position_y = 100.0
    computed_height = 400.0


class RecordingGrid:
    def __init__(self) -> None:
        self.loaded: list[str] = []
        self.on_done_callbacks: list = []

    def load_image_into(self, _frame, url, size=None, on_done=None) -> None:
        self.loaded.append(url)
        self.on_done_callbacks.append(on_done)


def _browser(frames_at, scroll=None) -> ToolBrowser:
    """A browser with only the fields the visibility check reads."""
    browser = object.__new__(ToolBrowser)
    browser._scroll = FakeScroll() if scroll is None else scroll
    browser._grid = RecordingGrid()
    # Empty bands: the placeholder animation is covered separately, and an
    # empty list is the no-picture case anyway.
    browser._deferred_avatars = [(FakeFrame(y), f"url-{y}", []) for y in frames_at]
    browser._pulsing = []
    browser._pulse_subscription = None
    browser._cooldown_until = 0.0
    return browser


class TestWhatGetsFetched:
    def test_a_card_on_screen_is_fetched(self):
        browser = _browser([200])
        browser._load_visible_avatars()
        assert browser._grid.loaded == ["url-200"]

    def test_a_card_far_below_is_left_alone(self):
        # The bottom of the catalogue is a very long way down; this is the case
        # that mattered.
        browser = _browser([9000])
        browser._load_visible_avatars()
        assert browser._grid.loaded == []

    def test_a_card_far_above_is_left_alone(self):
        browser = _browser([-9000])
        browser._load_visible_avatars()
        assert browser._grid.loaded == []

    def test_a_card_just_out_of_sight_is_fetched_anyway(self):
        """The prefetch margin: scrolling should land on loaded pictures."""
        browser = _browser([560])  # below the 500 edge, inside the margin
        browser._load_visible_avatars()
        assert browser._grid.loaded == ["url-560"]

    def test_only_the_near_ones_are_fetched(self):
        browser = _browser([200, 300, 9000, 12000])
        browser._load_visible_avatars()
        assert browser._grid.loaded == ["url-200", "url-300"]


class TestWhatStaysOutstanding:
    def test_a_fetched_avatar_is_not_fetched_again(self):
        browser = _browser([200])
        browser._load_visible_avatars()
        browser._load_visible_avatars()
        assert browser._grid.loaded == ["url-200"]

    def test_an_unfetched_avatar_stays_for_the_next_scroll(self):
        browser = _browser([9000])
        browser._load_visible_avatars()
        assert len(browser._deferred_avatars) == 1


class TestBeforeLayout:
    """Positions are meaningless until omni.ui has laid the widgets out."""

    @pytest.mark.parametrize(
        "scroll",
        [
            pytest.param(type("Unlaid", (), {"screen_position_y": 0, "computed_height": 0})(), id="no-geometry"),
            pytest.param(type("NoHeight", (), {"screen_position_y": 100, "computed_height": 0})(), id="no-height"),
        ],
    )
    def test_nothing_is_fetched_without_geometry(self, scroll):
        # Nothing rather than everything: guessing here would cost exactly the
        # whole-catalogue fetch this is here to avoid.
        browser = _browser([200], scroll=scroll)
        browser._load_visible_avatars()
        assert browser._grid.loaded == []
        assert len(browser._deferred_avatars) == 1

    def test_a_missing_scroller_is_not_an_error(self):
        browser = _browser([200], scroll=None)
        browser._scroll = None
        browser._load_visible_avatars()
        assert browser._grid.loaded == []


class TestTheBreathingPlaceholder:
    """The squares that stand in while an avatar is on its way.

    The animation runs off a per-frame subscription, so the thing that matters
    is that it is only alive while something is actually waiting for a picture.
    """

    def _browser_with_bands(self, bands):
        browser = object.__new__(ToolBrowser)
        browser._pulsing = list(bands)
        browser._pulse_phase = 0.0
        browser._pulse_subscription = "subscribed"
        return browser

    def test_a_finished_avatar_stops_its_own_bands(self):
        mine = [(object(), 0.0), (object(), 0.5)]
        theirs = [(object(), 0.0)]
        browser = self._browser_with_bands(mine + theirs)

        browser._stop_pulsing(mine)

        assert browser._pulsing == theirs

    def test_the_subscription_ends_with_the_last_band(self):
        bands = [(object(), 0.0)]
        browser = self._browser_with_bands(bands)

        browser._stop_pulsing(bands)

        assert browser._pulsing == []
        assert browser._pulse_subscription is None

    def test_the_subscription_survives_while_others_wait(self):
        mine = [(object(), 0.0)]
        browser = self._browser_with_bands(mine + [(object(), 0.5)])

        browser._stop_pulsing(mine)

        assert browser._pulse_subscription == "subscribed"

    def test_an_avatar_with_no_bands_is_harmless(self):
        """A tool with no avatar_url never built any."""
        browser = self._browser_with_bands([(object(), 0.0)])
        browser._stop_pulsing([])
        assert browser._pulse_subscription == "subscribed"

    def test_a_tick_with_nothing_waiting_drops_the_subscription(self):
        browser = self._browser_with_bands([])
        browser._on_pulse(_Event(dt=0.016))
        assert browser._pulse_subscription is None


class TestPulseColour:
    def test_a_grey_is_opaque(self):
        from rundiffusion_omniverse.dialogs import _grey

        assert _grey(0x2A) == 0xFF2A2A2A

    @pytest.mark.parametrize("value,expected", [(-50, 0xFF000000), (999, 0xFFFFFFFF)])
    def test_a_grey_cannot_leave_the_channel(self, value, expected):
        from rundiffusion_omniverse.dialogs import _grey

        assert _grey(value) == expected


class _Event:
    def __init__(self, dt: float) -> None:
        self.payload = {"dt": dt}


class TestTheScrollSettle:
    """A drag through the list must not spend the request budget on it.

    Scrolling the catalogue from top to bottom passes every card on the way.
    The first version fetched each one as it flew by, which spent the
    endpoint's rate limit on pictures that were on screen for a few frames.
    Scrolling now only restarts a timer; the fetch happens when the list stops.
    """

    def _browser(self):
        browser = object.__new__(ToolBrowser)
        browser._settle_task = None
        browser._tasks = set()
        browser._booked: list = []

        class _Task:
            def __init__(self) -> None:
                self.cancelled = False

            def cancel(self) -> None:
                self.cancelled = True

            def add_done_callback(self, _fn) -> None:
                pass

        browser._Task = _Task
        return browser

    def test_scrolling_does_not_fetch(self, monkeypatch):
        import rundiffusion_omniverse.dialogs as dialogs

        browser = self._browser()
        browser._grid = RecordingGrid()
        booked = []
        monkeypatch.setattr(
            dialogs.asyncio, "ensure_future", lambda coro: (coro.close(), booked.append(1), browser._Task())[-1]
        )

        browser._on_scrolled(0.0)

        # Nothing fetched; a settle was booked instead.
        assert browser._grid.loaded == []
        assert booked == [1]

    def test_a_second_scroll_cancels_the_first_settle(self, monkeypatch):
        import rundiffusion_omniverse.dialogs as dialogs

        browser = self._browser()
        made = []

        def _fake(coro):
            coro.close()
            task = browser._Task()
            made.append(task)
            return task

        monkeypatch.setattr(dialogs.asyncio, "ensure_future", _fake)

        browser._on_scrolled(0.0)
        browser._on_scrolled(10.0)

        # The first settle was abandoned, so a continuous drag never fires one.
        assert made[0].cancelled is True
        assert made[1].cancelled is False


class TestUndrawnCards:
    """A card omni.ui has not laid out yet must not read as visible.

    Reported twice from real sessions, as "the last third never load". The
    visible band is the scroller's top minus a prefetch margin, which on a
    window near the top of the screen is NEGATIVE. An undrawn card reports
    `screen_position_y` of 0, and 0 sits inside a band that starts below zero,
    so on the first pass, when none of them are drawn, every card in the
    catalogue looked like it was on screen and was requested at once.
    Everything past the endpoint's rate limit came back 429.

    The lazy loading was never lazy on first render. It only looked that way
    because the first hundred-odd requests succeeded.
    """

    def test_an_undrawn_card_is_not_fetched(self):
        browser = _browser([0])
        browser._load_visible_avatars()
        assert browser._grid.loaded == []

    def test_an_undrawn_card_stays_queued_for_when_it_is_drawn(self):
        browser = _browser([0])
        browser._load_visible_avatars()
        assert len(browser._deferred_avatars) == 1

    def test_a_whole_undrawn_catalogue_costs_nothing(self):
        # The exact shape of the bug: a whole catalogue of cards, none drawn,
        # one pass.
        browser = _browser([0] * 300)
        browser._load_visible_avatars()
        assert browser._grid.loaded == []

    def test_the_drawn_ones_are_still_fetched_alongside(self):
        browser = _browser([0, 200, 0, 300])
        browser._load_visible_avatars()
        assert browser._grid.loaded == ["url-200", "url-300"]

    def test_a_negative_position_is_not_fetched(self):
        """Scrolled off the top far enough to go negative is not 'undrawn',
        but it is not on screen either."""
        browser = _browser([-5000])
        browser._load_visible_avatars()
        assert browser._grid.loaded == []
