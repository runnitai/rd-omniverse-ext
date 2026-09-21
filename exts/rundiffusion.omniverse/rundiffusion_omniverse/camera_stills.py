"""One still per named camera, for the viewfinder to show.

The live viewfinder in `viewport_preview` follows the active viewport, which is
exactly right when the active viewport is what will be captured and a lie when
it is not. A field set to capture from `/World/Cameras/Lobby` showing whatever
the user is currently orbiting is worse than showing nothing: it is a picture
that says it is the thing about to be sent, and is not.

So a named camera gets a still instead of a live view, and the difference is
deliberate rather than a shortcut.

**A still, because a live view would cost the viewport.** Previewing a camera
that is not the active one means pointing the viewport at it, waiting for the
renderer, and putting it back, for every refresh. Doing that on a timer would
make the user's own view flicker continuously while they typed a prompt.

**Once per camera, because a camera prim does not move on its own.** The scene
can change under it, so the still is refreshed when the camera is chosen and
again after each capture from it, which are the two moments the user has any
reason to expect it to be current.

Stills are keyed by camera path and outlive a form rebuild, so switching tools
or tabs does not pay for the same capture twice.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .viewport_capture import CaptureError, capture_png

logger = logging.getLogger(__name__)


class CameraStills:
    """A thumbnail per camera path, captured on demand.

    `on_changed` is called whenever any still becomes something different, so
    the panel can redraw the viewfinders in one place rather than each still
    poking at widgets.
    """

    def __init__(self, cache_dir: Path, on_changed=None) -> None:
        self._cache_dir = cache_dir
        self._on_changed = on_changed
        self._paths: dict[str, Path] = {}
        self._messages: dict[str, str] = {}
        #: Cameras with a capture in the air. A viewfinder is redrawn on every
        #: row rebuild and each redraw asks for its still, so without this a
        #: camera would be captured once per rebuild rather than once.
        self._running: set[str] = set()
        self._counter = 0

    # -- what the panel reads ---------------------------------------------

    def path(self, camera_path: str) -> Path | None:
        """The still for this camera, or None if there is not one yet."""
        return self._paths.get(camera_path)

    def message(self, camera_path: str) -> str | None:
        """Why there is no still for this camera, when there is not one."""
        return self._messages.get(camera_path)

    # -- lifecycle ---------------------------------------------------------

    def destroy(self) -> None:
        self._on_changed = None
        for path in list(self._paths.values()):
            self._discard(path)
        self._paths.clear()
        self._messages.clear()

    def forget(self, camera_path: str) -> None:
        """Drop what is held for one camera, so the next ask recaptures it."""
        self._discard(self._paths.pop(camera_path, None))
        self._messages.pop(camera_path, None)

    # -- capturing ---------------------------------------------------------

    def refresh(self, camera_path: str, spawn) -> None:
        """Take a still for `camera_path` unless one is already being taken.

        `spawn` is the panel's task starter rather than a bare
        `ensure_future`, so a still in flight is cancelled with everything else
        when the panel goes away.
        """
        if camera_path in self._running:
            return
        self._running.add(camera_path)
        spawn(self._refresh(camera_path))

    def ensure(self, camera_path: str, spawn) -> None:
        """Take a still only if there is nothing to show for this camera yet.

        What a viewfinder asks on every redraw. Held stills and recorded
        failures both count as something to show: retrying a camera that just
        failed, on every rebuild of a row, would be a capture loop nobody asked
        for.
        """
        if camera_path in self._paths or camera_path in self._messages:
            return
        self.refresh(camera_path, spawn)

    async def _refresh(self, camera_path: str) -> None:
        try:
            png = await capture_png(camera_path)
        except asyncio.CancelledError:
            raise
        except CaptureError as error:
            self._set(camera_path, None, str(error))
            return
        except Exception:  # noqa: BLE001 - a preview never breaks the panel
            logger.exception("RunDiffusion: a camera preview failed.")
            self._set(camera_path, None, "That camera could not be previewed.")
            return
        finally:
            self._running.discard(camera_path)

        # Imported here rather than at module scope: `viewport_preview` imports
        # nothing from this module, and keeping it that way means neither has
        # to care which is loaded first.
        from .viewport_preview import VIEWFINDER_MAX_SIDE, write_thumbnail

        self._counter += 1
        path = self._cache_dir / f"camera-still-{self._counter}.png"
        if not write_thumbnail(png, path, max_side=VIEWFINDER_MAX_SIDE):
            self._set(camera_path, None, "That camera's preview could not be written.")
            return
        self._set(camera_path, path, None)

    def _set(self, camera_path: str, path: Path | None, message: str | None) -> None:
        previous = self._paths.get(camera_path)
        if previous == path and self._messages.get(camera_path) == message:
            return
        if path is None:
            self._paths.pop(camera_path, None)
        else:
            self._paths[camera_path] = path
        if message is None:
            self._messages.pop(camera_path, None)
        else:
            self._messages[camera_path] = message
        if previous is not None and previous != path:
            self._discard(previous)
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:  # noqa: BLE001
                logger.exception("RunDiffusion: a camera preview callback failed.")

    def _discard(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("RunDiffusion: could not remove %s.", path.name)
