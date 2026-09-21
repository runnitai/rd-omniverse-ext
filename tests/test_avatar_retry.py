"""A refused avatar has to come back and ask again.

Reported from a real session: scrolling the tool grid to the bottom left the
last third of the avatars blank with their placeholders frozen, and typing in
the search box and clearing it made them appear. That last part was the tell.
Clearing a search rebuilds every card, and a rebuilt card re-registers its
avatar, so the only thing wrong with those tools was that nothing was ever
going to ask for them a second time.

The cause was the endpoint's throttle. `/api/v2/avatars/{id}.png` is rate
limited per client, and the catalogue is larger than the limit. Asking for an
avatar removed it
from the pending list, so a 429 was indistinguishable from a deleted image and
equally final.
"""

from __future__ import annotations

import pytest

from rundiffusion_omniverse.asset_grid import _is_retryable, _retry_after_seconds
from rundiffusion_omniverse.dialogs import _AVATAR_MAX_ATTEMPTS, ToolBrowser


class FakeError(Exception):
    def __init__(self, code=None, headers=None) -> None:
        if code is not None:
            self.code = code
        if headers is not None:
            self.headers = headers


class TestWhatIsWorthAskingAgain:
    def test_the_throttle_is_retryable(self):
        """The one that actually happened."""
        assert _is_retryable(FakeError(code=429)) is True

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_the_server_having_a_moment_is_retryable(self, code):
        assert _is_retryable(FakeError(code=code)) is True

    def test_a_missing_avatar_is_not(self):
        # Retrying a settled answer forever is a busy loop, not resilience.
        assert _is_retryable(FakeError(code=404)) is False

    def test_a_forbidden_avatar_is_not(self):
        assert _is_retryable(FakeError(code=403)) is False

    def test_a_transport_failure_is_retryable(self):
        assert _is_retryable(OSError("connection reset")) is True


class TestRetryAfter:
    def test_the_servers_own_answer_is_used(self):
        assert _retry_after_seconds(FakeError(headers={"Retry-After": "30"})) == 30.0

    @pytest.mark.parametrize("value", ["soon", None, ""])
    def test_an_unusable_value_is_ignored(self, value):
        assert _retry_after_seconds(FakeError(headers={"Retry-After": value})) is None

    def test_no_headers_at_all_is_ignored(self):
        assert _retry_after_seconds(FakeError(code=429)) is None


class FakeBands:
    """Bands that record being settled, without needing omni.ui."""

    def __init__(self) -> None:
        self.styled: list = []

    def set_style(self, style) -> None:
        self.styled.append(style)


def _browser(cooldown_until=0.0) -> ToolBrowser:
    browser = object.__new__(ToolBrowser)
    browser._pulsing = []
    browser._pulse_subscription = None
    browser._deferred_avatars = []
    browser._avatar_attempts = {}
    browser._cooldown_until = cooldown_until
    browser._cooldown_streak = 0
    browser._retry_task = None
    browser._settle_task = None
    browser._scroll = None
    browser._sweep_task = None
    browser._cooldowns: list = []
    browser._enter_cooldown = lambda wait: browser._cooldowns.append(wait)
    # The sweeper needs a running loop; re-queueing is what is under test.
    browser._ensure_sweeping = lambda: None
    return browser


class TestWhatHappensAfterAFetch:
    def test_a_loaded_avatar_is_not_queued_again(self):
        browser = _browser()
        browser._avatar_finished("frame", "url", [], True, False, None)
        assert browser._deferred_avatars == []
        assert browser._cooldowns == []

    def test_a_throttled_avatar_goes_back_in_the_queue(self):
        browser = _browser()
        browser._avatar_finished("frame", "url", [], False, True, None)
        assert browser._deferred_avatars == [("frame", "url", [])]
        assert browser._cooldowns == [None]

    def test_a_missing_avatar_does_not(self):
        browser = _browser()
        browser._avatar_finished("frame", "url", [], False, False, None)
        assert browser._deferred_avatars == []
        assert browser._cooldowns == []

    def test_retrying_gives_up_eventually(self):
        browser = _browser()
        for _ in range(_AVATAR_MAX_ATTEMPTS + 2):
            browser._deferred_avatars = []
            browser._avatar_finished("frame", "url", [], False, True, None)
        assert browser._deferred_avatars == []

    def test_a_success_clears_the_history(self):
        browser = _browser()
        browser._avatar_finished("frame", "url", [], False, True, None)
        browser._avatar_finished("frame", "url", [], True, False, None)
        assert "url" not in browser._avatar_attempts

    def test_a_success_clears_the_cooldown_streak(self):
        browser = _browser()
        browser._cooldown_streak = 3
        browser._avatar_finished("frame", "url", [], True, False, None)
        assert browser._cooldown_streak == 0

    def test_the_servers_wait_is_passed_along(self):
        browser = _browser()
        browser._avatar_finished("frame", "url", [], False, True, 30.0)
        assert browser._cooldowns == [30.0]


class TestTheSharedCooldown:
    """A 429 is news about the connection, not about one picture.

    The refusals arrive in a clump. Treating each as its own problem is what
    produced hundreds of failed requests that never converged, so the first
    refusal stops everything and the rest are ignored.
    """

    def _real_cooldown_browser(self):
        browser = _browser()
        del browser._enter_cooldown  # use the real one
        # The sweep needs a running loop; the decision under test does not.
        browser._booked: list = []
        browser._book_resume = lambda delay: browser._booked.append(delay)
        return browser

    def test_the_first_refusal_starts_a_pause(self):
        import time

        browser = self._real_cooldown_browser()
        browser._enter_cooldown(None)
        assert browser._cooldown_until > time.monotonic()
        assert browser._cooldown_streak == 1

    def test_the_rest_of_the_clump_does_not_extend_it(self):
        browser = self._real_cooldown_browser()
        browser._enter_cooldown(None)
        first = browser._cooldown_until
        for _ in range(50):
            browser._enter_cooldown(None)
        assert browser._cooldown_until == first
        assert browser._cooldown_streak == 1

    def test_nothing_is_fetched_while_cooling(self):
        import time

        browser = _browser(cooldown_until=time.monotonic() + 60)
        browser._scroll = type("S", (), {"screen_position_y": 100, "computed_height": 400})()
        browser._grid = type("G", (), {"load_image_into": lambda *a, **k: pytest.fail("asked while cooling")})()
        browser._deferred_avatars = [("frame", "url", [])]

        browser._load_visible_avatars()

        assert browser._deferred_avatars == [("frame", "url", [])]


class TestPacing:
    """The fetcher keeps itself under the endpoint's budget.

    A catalogue larger than the endpoint's per-client limit means a thorough
    browse cannot fit however carefully the requests are gated. Sprinting until
    refused produced dozens of rejections and close to a minute of blackout;
    pacing means the pictures arrive steadily and none are refused at all.
    """

    def _grid(self, tmp_path):
        from rundiffusion_omniverse.asset_grid import AssetGrid

        return AssetGrid(tmp_path)

    def test_the_first_screenful_is_free(self, tmp_path):
        import asyncio

        from rundiffusion_omniverse import asset_grid

        grid = self._grid(tmp_path)

        async def _drain():
            for _ in range(asset_grid._FETCH_BURST):
                await grid._take_token()

        # A full bucket covers a screenful without ever sleeping, so opening
        # the browser is not made to feel slow by the rate limit.
        asyncio.new_event_loop().run_until_complete(
            asyncio.wait_for(_drain(), timeout=1.0)
        )

    def test_the_bucket_is_spent_after_the_burst(self, tmp_path):
        import asyncio

        from rundiffusion_omniverse import asset_grid

        grid = self._grid(tmp_path)

        async def _drain():
            for _ in range(asset_grid._FETCH_BURST):
                await grid._take_token()

        asyncio.new_event_loop().run_until_complete(_drain())
        assert grid._tokens < 1.0

    def test_the_rate_is_not_raised_casually(self):
        """The rate is chosen to sit under the avatar endpoint's per-client
        limit, with room for the panel's own avatar traffic. Raising it means
        checking that limit first, so it fails here rather than in a scroll."""
        from rundiffusion_omniverse import asset_grid

        assert asset_grid._FETCH_RATE_PER_SECOND <= 1.6
