"""Rebuilding a container from inside the click that asked for it.

omni.ui refuses it. Clearing a container while an event is being dispatched
logs

    Container::clear was called during an event or draw, this is not supported
    Container::addChild attempting to add a child during a draw callback

and then carries on in a state it does not define. Reproduced in Kit 110.1.2
against the Library tab's filter panel, where Apply and Clear both rebuilt the
frame their own button was sitting in.

Note what the message actually says. It is not about clearing the container the
CLICKED widget belongs to; it is about clearing ANY container during dispatch.
So the rule is about WHEN a rebuild happens rather than about which widget it
touches, and the fix is to wait one frame. That costs nothing a person can
perceive and puts the work where the toolkit expects it.

`ToolBrowser._render` predates this module and does the same thing inline; it
is left alone because it works and because the browser is the one surface that
cannot be exercised without a running Kit. Everything else goes through here.

Coalescing matters as much as deferring. Several requests can land before the
frame that serves them, and running a rebuild twice would clear a container the
second pass is midway through filling.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

logger = logging.getLogger(__name__)


async def _next_frame() -> None:
    """One turn of the app loop. Imported late so this module loads in tests."""
    import omni.kit.app

    await omni.kit.app.get_app().next_update_async()


class Redraws:
    """Rebuilds waiting for the next frame, at most one per key.

    The key names WHAT is being rebuilt, not who asked. Two requests to redraw
    the filter panel are one redraw; a request for the filters and one for the
    job list are two.
    """

    def __init__(self, wait_for_frame: Callable[[], object] = _next_frame) -> None:
        self._wait_for_frame = wait_for_frame
        #: The newest render queued per key. Newest wins: an older closure was
        #: made against state that has since moved on.
        self._pending: dict[str, Callable[[], None]] = {}
        #: The frame booked for each key. Keyed rather than a bare set, so a
        #: cancel can spare one group of keys and take the rest: the library in
        #: its own window survives a tab switch that destroys everything else.
        self._tasks: dict[str, asyncio.Task] = {}

    def request(self, key: str, render: Callable[[], None]) -> None:
        """Redraw `key` on the next frame."""
        queued = key in self._pending
        self._pending[key] = render
        if queued:
            # A frame is already booked for this key and will pick up the
            # render just stored. Booking a second would run it twice.
            return
        task = asyncio.ensure_future(self._run(key))
        # Keep the reference: an unreferenced task can be collected before it
        # runs, which here would look like a button that does nothing.
        self._tasks[key] = task
        task.add_done_callback(lambda finished, k=key: self._forget(k, finished))

    def _forget(self, key: str, finished: asyncio.Task) -> None:
        # Only if it is still the task booked for this key. A cancel followed
        # by a fresh request stores a new one, and the old one's callback
        # arrives afterwards.
        if self._tasks.get(key) is finished:
            self._tasks.pop(key, None)

    def cancel(self, keep_prefixes: tuple[str, ...] = ()) -> None:
        """Drop pending rebuilds, because their target is going away.

        Called where the frames themselves are destroyed. A render that arrives
        afterwards would be drawing into widgets nobody is looking at. The
        renderers guard for that too, but not booking the frame at all is
        cheaper and clearer than relying on both.

        `keep_prefixes` spares the keys starting with any of them. A tab switch
        destroys the tab body and everything in it, but a browsing surface can
        be torn out into a window that the tab switch does not touch, and
        cancelling its pending rebuilds would leave it showing whatever it had
        before the click. Several rather than one, because the library and the
        uploads can each be in a window and they are independent: docking one
        must not start cancelling the other's redraws.
        """

        def spared(key: str) -> bool:
            return any(key.startswith(prefix) for prefix in keep_prefixes)

        for key in list(self._pending):
            if spared(key):
                continue
            self._pending.pop(key, None)
        for key in list(self._tasks):
            if spared(key):
                continue
            self._tasks.pop(key).cancel()

    async def _run(self, key: str) -> None:
        # Nothing is popped on cancellation. `cancel` has already emptied
        # `_pending`, and the cancellation is DELIVERED later: by then a new
        # request may have stored a render under this key, and popping it here
        # would throw away the one thing that is still wanted. That is a tab
        # switch, which cancels and then immediately books the new tab's
        # rebuilds, so it is the common path rather than a corner.
        await self._wait_for_frame()
        render = self._pending.pop(key, None)
        if render is None:
            return
        # Let go of this task BEFORE running the render, because the render is
        # allowed to call `cancel`. A tab switch does exactly that, and it is
        # reached from renders booked here (`tab`, `<surface>:layout`), so
        # without this the running task cancels itself: CPython turns the
        # coroutine's ordinary return into a CancelledError. Harmless only for
        # as long as nothing follows `render()` below, which is not a property
        # worth depending on.
        if self._tasks.get(key) is asyncio.current_task():
            self._tasks.pop(key, None)
        try:
            render()
        except Exception:  # noqa: BLE001 - one dead redraw must not kill the rest
            logger.exception("RunDiffusion: could not redraw %s.", key)
