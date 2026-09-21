"""A rebuild waits for the next frame, and happens once.

omni.ui refuses a container clear while an event is being dispatched. Caught in
Kit 110.1.2 on the Library tab's filter panel, where Apply and Clear both
rebuilt the frame their own button was sitting in:

    [Error] [omni.ui] Container::clear was called during an event or draw,
    this is not supported
      self._apply_filters()
      self._render_filters()
      frame.clear()
    [Warning] [omni.ui] Container::addChild attempting to add a child during a
    draw callback

The message is about WHEN, not about which widget: any container clear during
dispatch, not just the clicked one's. So the fix is to wait a frame, and the
part that needs testing is the bookkeeping around that wait.
"""

from __future__ import annotations

import asyncio

import pytest

from rundiffusion_omniverse.redraw import Redraws


class Frames:
    """A frame boundary a test can step over on demand."""

    def __init__(self) -> None:
        self.waiting = 0
        self._gates: list[asyncio.Future] = []

    async def wait(self) -> None:
        self.waiting += 1
        gate: asyncio.Future = asyncio.get_event_loop().create_future()
        self._gates.append(gate)
        await gate

    def tick(self) -> None:
        """Release everything waiting on the frame that just ended."""
        for gate in self._gates:
            if not gate.done():
                gate.set_result(None)
        self._gates.clear()


@pytest.fixture
def loop():
    made = asyncio.new_event_loop()
    yield made
    # Drain before closing. A task still waiting on a frame that will never
    # come is the normal end state of these tests, and closing the loop under
    # one prints "Task was destroyed but it is pending" over the results.
    pending = asyncio.all_tasks(made)
    for task in pending:
        task.cancel()
    if pending:
        made.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    made.close()


def _settle(loop) -> None:
    """Let every ready callback run, without advancing a frame."""
    loop.run_until_complete(asyncio.sleep(0))
    loop.run_until_complete(asyncio.sleep(0))


class TestDeferring:
    def test_nothing_is_drawn_during_the_click(self, loop):
        """The whole point: the render must not run inside the event that
        asked for it."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("filters", lambda: drawn.append("filters"))
        _settle(loop)

        assert drawn == []

    def test_it_is_drawn_on_the_next_frame(self, loop):
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("filters", lambda: drawn.append("filters"))
        _settle(loop)
        frames.tick()
        _settle(loop)

        assert drawn == ["filters"]


class TestCoalescing:
    def test_two_requests_for_one_thing_draw_it_once(self, loop):
        """Running a rebuild twice would clear a container the second pass is
        midway through filling."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("filters", lambda: drawn.append("first"))
        redraws.request("filters", lambda: drawn.append("second"))
        _settle(loop)
        frames.tick()
        _settle(loop)

        assert drawn == ["second"]
        assert frames.waiting == 1

    def test_the_newest_render_is_the_one_that_runs(self, loop):
        """An older closure was made against state that has since moved on."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("filters", lambda: drawn.append("stale"))
        redraws.request("filters", lambda: drawn.append("current"))
        _settle(loop)
        frames.tick()
        _settle(loop)

        assert drawn == ["current"]

    def test_different_things_are_both_drawn(self, loop):
        """The key names WHAT is being rebuilt, so two of them are two
        rebuilds, not one."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("filters", lambda: drawn.append("filters"))
        redraws.request("jobs", lambda: drawn.append("jobs"))
        _settle(loop)
        frames.tick()
        _settle(loop)

        assert sorted(drawn) == ["filters", "jobs"]

    def test_a_later_request_books_a_new_frame(self, loop):
        """Coalescing is per pending rebuild, not for the life of the object.
        A key that has already been drawn can be asked for again."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("filters", lambda: drawn.append("once"))
        _settle(loop)
        frames.tick()
        _settle(loop)
        redraws.request("filters", lambda: drawn.append("twice"))
        _settle(loop)
        frames.tick()
        _settle(loop)

        assert drawn == ["once", "twice"]


class TestGivingUp:
    def test_a_cancelled_rebuild_never_draws(self, loop):
        """Booked against a frame the tab switch is about to destroy."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("filters", lambda: drawn.append("filters"))
        _settle(loop)
        redraws.cancel()
        frames.tick()
        _settle(loop)

        assert drawn == []

    def test_a_request_after_cancelling_still_works(self, loop):
        """`cancel` runs on every tab switch, and the tab it switches TO books
        its own rebuilds immediately afterwards."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("filters", lambda: drawn.append("old"))
        _settle(loop)
        redraws.cancel()
        redraws.request("filters", lambda: drawn.append("new"))
        _settle(loop)
        frames.tick()
        _settle(loop)

        assert drawn == ["new"]

    def test_one_failing_render_does_not_take_the_rest(self, loop):
        """These run in a task nobody awaits, so an exception here is silent
        and takes the queue with it."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        def _boom() -> None:
            raise RuntimeError("no")

        redraws.request("filters", _boom)
        redraws.request("jobs", lambda: drawn.append("jobs"))
        _settle(loop)
        frames.tick()
        _settle(loop)

        assert drawn == ["jobs"]
        redraws.request("filters", lambda: drawn.append("again"))
        _settle(loop)
        frames.tick()
        _settle(loop)
        assert drawn == ["jobs", "again"]



class TestARenderThatCancels:
    """`_show_tab` calls `cancel`, and is itself reached from renders booked
    here (`tab`, `<surface>:layout`). Without letting go of the task first, the
    running one cancels itself and CPython turns its ordinary return into a
    CancelledError."""

    def test_the_task_finishes_rather_than_being_cancelled(self, loop):
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        ran: list = []

        def render_that_cancels():
            ran.append("ran")
            redraws.cancel()

        redraws.request("tab", render_that_cancels)
        task = redraws._tasks["tab"]
        _settle(loop)
        frames.tick()
        _settle(loop)

        assert ran == ["ran"]
        assert not task.cancelled(), "the render cancelled its own task"

    def test_work_booked_by_that_render_survives(self, loop):
        """A tab switch cancels and then immediately books the new tab's
        rebuilds, so this is the common path rather than a corner."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        def render_that_cancels():
            redraws.cancel()
            redraws.request("results", lambda: drawn.append("results"))

        redraws.request("tab", render_that_cancels)
        _settle(loop)
        frames.tick()
        _settle(loop)
        frames.tick()
        _settle(loop)

        assert drawn == ["results"]


class TestSparingOneGroup:
    def test_several_prefixes_are_spared_at_once(self, loop):
        """The library and the uploads can each be in a window of their own,
        and they are independent: docking one must not start cancelling the
        other's redraws."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("library:filters", lambda: drawn.append("library"))
        redraws.request("uploads:actions", lambda: drawn.append("uploads"))
        redraws.request("results", lambda: drawn.append("results"))
        _settle(loop)
        redraws.cancel(keep_prefixes=("library:", "uploads:"))
        frames.tick()
        _settle(loop)

        assert sorted(drawn) == ["library", "uploads"]

    def test_no_prefixes_spares_nothing(self, loop):
        """The default, and what a tab switch passes when everything is
        docked."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("library:filters", lambda: drawn.append("library"))
        _settle(loop)
        redraws.cancel()
        frames.tick()
        _settle(loop)

        assert drawn == []

    """A tab switch destroys the tab body and cancels the rebuilds booked
    against it. The library can be torn out into a window that the switch does
    not touch, so its rebuilds have to survive one."""

    def test_a_kept_prefix_is_still_drawn(self, loop):
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("library:filters", lambda: drawn.append("filters"))
        redraws.request("results", lambda: drawn.append("results"))
        _settle(loop)
        redraws.cancel(keep_prefixes=("library:",))
        frames.tick()
        _settle(loop)

        assert drawn == ["filters"]

    def test_everything_outside_it_is_dropped(self, loop):
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("library:session", lambda: drawn.append("session"))
        redraws.request("jobs", lambda: drawn.append("jobs"))
        redraws.request("results", lambda: drawn.append("results"))
        _settle(loop)
        redraws.cancel(keep_prefixes=("library:",))
        frames.tick()
        _settle(loop)

        assert drawn == ["session"]

    def test_no_prefix_still_cancels_everything(self, loop):
        """The ordinary tab switch, with the library docked. Nothing changes
        for it."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("library:filters", lambda: drawn.append("filters"))
        redraws.request("results", lambda: drawn.append("results"))
        _settle(loop)
        redraws.cancel()
        frames.tick()
        _settle(loop)

        assert drawn == []

    def test_a_kept_key_can_still_be_asked_for_again(self, loop):
        """Its frame was never cancelled, so a second request must not book a
        duplicate that draws it twice."""
        asyncio.set_event_loop(loop)
        frames = Frames()
        redraws = Redraws(frames.wait)
        drawn: list = []

        redraws.request("library:filters", lambda: drawn.append("first"))
        _settle(loop)
        redraws.cancel(keep_prefixes=("library:",))
        redraws.request("library:filters", lambda: drawn.append("second"))
        _settle(loop)
        frames.tick()
        _settle(loop)

        assert drawn == ["second"]
