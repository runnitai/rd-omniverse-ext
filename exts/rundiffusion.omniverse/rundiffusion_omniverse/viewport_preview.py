"""A live thumbnail of the active viewport, so a field can show what it will send.

An image field that takes the viewport used to say so in words. Words are enough
right up until the user has orbited away from what they framed, because the run
captures at SUBMIT time and there is nothing on screen tying the two together.
This module keeps a small PNG of the current view on disk and tells the panel
when it changes, so every field pointing at the viewport shows the frame it would
actually send.

Two decisions carry the design.

**It refreshes when the camera SETTLES, not while it moves.** A capture is a full
readback and orbiting emits one signature change per frame, so refreshing on
every change would fire sixty captures for one drag. The camera signature is the
view and projection matrices, the same pair `compare.py` watches and for the same
reason: orbiting moves the view while a focal-length or aspect change moves only
the projection. When it stops changing for `SETTLE_SECONDS`, one capture runs.

**Every refresh writes a NEW file.** omni.ui.Image caches by path, so rewriting
the same filename leaves the old texture on screen: the preview would look
frozen while the file underneath it changed. A counter in the name sidesteps the
cache, and the previous file is deleted once the new one is in place.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from pathlib import Path

from .viewport_capture import CaptureError, capture_active_viewport_png

logger = logging.getLogger(__name__)

#: How long the camera must hold still before a refresh runs.
SETTLE_SECONDS = 0.4

#: Longest edge of a thumbnail pinned into an image field, which is drawn at
#: 58px; 256 keeps it sharp without carrying a 4K readback around.
MAX_SIDE = 256

#: Longest edge of the live viewfinder and of a named camera's still. They are
#: drawn across the full width of the panel, a few hundred pixels, and a 256px
#: picture stretched to that reads as out of focus: the one thing a frame for
#: judging a view must not be.
VIEWFINDER_MAX_SIDE = 640


def write_thumbnail(png: bytes, path: Path, max_side: int = MAX_SIDE) -> bool:
    """Downscale PNG bytes into `path`. True on success.

    Pillow is prebundled in omni.kit.pip_archive, so this costs no wheel.
    """
    from PIL import Image

    try:
        with Image.open(io.BytesIO(png)) as image:
            thumbnail = image.convert("RGBA")
            thumbnail.thumbnail((max_side, max_side))
            thumbnail.save(path, format="PNG")
        return True
    except Exception:  # noqa: BLE001 - a preview is never worth failing a run over
        logger.debug("RunDiffusion: could not write a viewport thumbnail.")
        return False


def crop_box(width: int, height: int, aspect: float) -> tuple[int, int, int, int]:
    """The centred (left, top, right, bottom) of `aspect` inside a picture.

    Cropped rather than padded. The output size a person picked is the shape
    of the picture they want, and bars of nothing sent to the model would be
    generated from as though they were part of the scene.
    """
    if width <= 0 or height <= 0 or aspect <= 0:
        return 0, 0, max(width, 0), max(height, 0)
    if width / height > aspect:
        new_width = max(1, round(height * aspect))
        left = (width - new_width) // 2
        return left, 0, left + new_width, height
    new_height = max(1, round(width / aspect))
    top = (height - new_height) // 2
    return 0, top, width, top + new_height


def crop_png(png: bytes, aspect: float | None) -> bytes:
    """`png` cropped to `aspect` about its centre, or `png` unchanged.

    Unchanged when there is no shape to crop to, when the picture is already
    that shape, or when it cannot be decoded: a capture is never lost to its
    crop.
    """
    if not aspect or aspect <= 0:
        return png
    from PIL import Image

    try:
        with Image.open(io.BytesIO(png)) as image:
            box = crop_box(image.width, image.height, aspect)
            if box == (0, 0, image.width, image.height):
                return png
            out = io.BytesIO()
            image.crop(box).save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001 - an uncropped capture beats no capture
        logger.exception("RunDiffusion: could not crop a capture.")
        return png


def write_cropped(source: Path, target: Path, aspect: float) -> bool:
    """Write `source` cropped to `aspect` into `target`. True on success."""
    try:
        target.write_bytes(crop_png(source.read_bytes(), aspect))
        return True
    except OSError:
        logger.debug("RunDiffusion: could not write a cropped preview.")
        return False


class ViewportPreview:
    """Keeps a current thumbnail of the active viewport on disk.

    `on_changed` is called whenever `path` becomes something different, including
    when it becomes None because every viewport was closed.
    """

    def __init__(self, cache_dir: Path, on_changed=None) -> None:
        self._cache_dir = cache_dir
        self._on_changed = on_changed
        self._path: Path | None = None
        self._counter = 0
        self._subscription = None
        self._signature: tuple | None = None
        self._changed_at: float | None = None
        self._task: asyncio.Task | None = None
        self._message: str | None = None

    # -- what the panel reads ---------------------------------------------

    @property
    def path(self) -> Path | None:
        """The current thumbnail, or None if there is nothing to show yet."""
        return self._path

    @property
    def message(self) -> str | None:
        """Why there is no thumbnail, when there is not one."""
        return self._message

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Watch the camera and keep the thumbnail current.

        Idempotent, and cheap to call again: the Create tab is rebuilt on every
        tab switch and each rebuild asks for this.
        """
        if self._subscription is None:
            import omni.kit.app

            self._subscription = (
                omni.kit.app.get_app()
                .get_update_event_stream()
                .create_subscription_to_pop(
                    self._on_update, name="rundiffusion.viewport-preview"
                )
            )
        # Nothing on disk yet means the first render has nothing to draw, so
        # take one now rather than waiting for the user to move the camera.
        if self._path is None:
            self.refresh_now()

    def stop(self) -> None:
        """Stop watching. Called when no field is using the viewport.

        A tool whose every image field points at the library should not be
        paying for a readback each time the user looks around.
        """
        self._subscription = None
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def destroy(self) -> None:
        self.stop()
        self._on_changed = None
        self._discard(self._path)
        self._path = None

    # -- the watch ---------------------------------------------------------

    def _on_update(self, _event) -> None:
        signature = self._camera_signature()
        now = time.monotonic()

        if signature != self._signature:
            self._signature = signature
            self._changed_at = now
            return

        if self._changed_at is None:
            return
        if now - self._changed_at < SETTLE_SECONDS:
            return

        # Settled. Consume the pending refresh before starting it, so a capture
        # that takes longer than SETTLE_SECONDS cannot queue a second one.
        self._changed_at = None
        self.refresh_now()

    def _camera_signature(self) -> tuple | None:
        from omni.kit.viewport.utility import get_active_viewport

        viewport = get_active_viewport()
        if viewport is None:
            return None
        try:
            return (tuple(viewport.view), tuple(viewport.projection))
        except Exception:  # noqa: BLE001 - a viewport mid-teardown has neither
            return None

    def refresh_now(self) -> None:
        """Capture and rewrite the thumbnail, unless one is already running."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.ensure_future(self._refresh())

    async def _refresh(self) -> None:
        try:
            png = await capture_active_viewport_png()
        except asyncio.CancelledError:
            raise
        except CaptureError as error:
            # A real state, not a defect: a Kit app can run with every viewport
            # closed. Say so instead of leaving the last view on screen, which
            # would be showing something that no longer exists.
            self._set(None, str(error))
            return
        except Exception:  # noqa: BLE001 - a preview never breaks the panel
            logger.exception("RunDiffusion: viewport preview failed.")
            self._set(None, "The viewport preview is unavailable.")
            return

        self._counter += 1
        path = self._cache_dir / f"viewport-preview-{self._counter}.png"
        if not write_thumbnail(png, path, max_side=VIEWFINDER_MAX_SIDE):
            self._set(None, "The viewport preview could not be written.")
            return
        self._set(path, None)

    def _set(self, path: Path | None, message: str | None) -> None:
        previous = self._path
        if previous == path and self._message == message:
            return
        self._path = path
        self._message = message
        if previous is not None and previous != path:
            self._discard(previous)
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:  # noqa: BLE001
                logger.exception("RunDiffusion: viewport preview callback failed.")

    def _discard(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("RunDiffusion: could not remove %s.", path.name)
