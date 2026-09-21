"""In-memory capture of a viewport, encoded to PNG bytes.

The main technical risk in building this extension: whether the host can hand
us pixels without a temp-file round trip. Kit can. `capture_viewport_to_buffer`
schedules a `ByteCapture` and delivers the raw AOV buffer to a callback,
verified against
omni.kit.viewport.utility 2.0.1 in the 110.1.2 SDK:

    capture_viewport_to_buffer(viewport_api, on_capture_fn, is_hdr=False)
    on_capture_fn(buffer, buffer_size, width, height, byte_format)

Two things about that contract are worth knowing before reading the code.

The callback arrives on a render thread some frames later, not inline, so this
module hands back an asyncio Future rather than bytes and the caller awaits it.
Every UI mutation must happen back on the caller's side after the await.

`buffer` is a PyCapsule wrapping a raw pointer, not a Python buffer. The capsule
unwrap below is the pattern from NVIDIA's own kit-extension-sample-apiconnect.
It was read as a pattern before it was ever run; it has since run in Kit Base
Editor throughout development, so it is proved rather than assumed.

**Which camera is captured is a choice, not a given.** `capture_png` takes a
camera prim path and borrows the active viewport to look through it, which is
what makes a framing reproducible instead of being whatever the orbit ended up
at. See that function for why it borrows rather than making a viewport of its
own, and for why the restore is in a `finally`.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import io
import logging

logger = logging.getLogger(__name__)

#: How long to wait for a scheduled capture before calling it failed.
CAPTURE_TIMEOUT_SECONDS = 30

#: Frames to let the renderer draw after the camera is moved, before the buffer
#: is read.
#:
#: This is the number the first prototype could not put a value on. A readback
#: taken on the frame a camera moves is whatever the renderer had got to, which
#: in RTX Real-Time is a frame or two of noise and in Path Tracing is the first
#: few samples of an image that needs hundreds. `next_viewport_frame_async` is
#: what makes waiting exact rather than a sleep guessed at in wall-clock, and
#: it ships in `omni.kit.viewport.utility` in this SDK.
#:
#: Sixty is chosen to be visibly enough for Real-Time and usefully into a Path
#: Tracing accumulation, at about a second on a viewport running at frame rate.
#: It is deliberately one named constant: it is the first thing to raise if a
#: path-traced capture still looks grainy, and the first thing to lower if
#: capturing feels slow in a Real-Time scene.
CAMERA_SETTLE_FRAMES = 60

#: How long to let that settle run before giving up on it and reading anyway.
#:
#: `next_viewport_frame_async` counts DELIVERED frames, so a viewport that
#: stops delivering them never finishes counting. Minimising Kit, hiding the
#: viewport, a renderer that stalls: each of those stops delivery without
#: stopping the await, and none of them is an error anything reports.
#:
#: What makes an unbounded wait worse here than a slow capture is what is being
#: held while it waits. The settle runs inside `_without_capture_furniture` and
#: inside the capture lock, so a wait that never ends leaves
#: `createCameraModelRep` off for the rest of the session (camera prims
#: invisible on the user's stage, the same fault the lock note below records
#: from Base Editor), leaves the viewport borrowed on a camera the user did not
#: choose, and blocks every later capture and every viewfinder behind the lock.
#:
#: Deliberately generous rather than tight. Sixty frames of Path Tracing on a
#: heavy stage is legitimately slow, and this is a backstop against NEVER, not
#: a budget for slow. Shorter than CAPTURE_TIMEOUT_SECONDS all the same: the
#: readback that follows needs its own share of a person's patience.
CAMERA_SETTLE_TIMEOUT_SECONDS = 20

#: App settings forced for the duration of a capture and put back afterwards.
#: See `_without_capture_furniture` for why this one, and why a setting rather
#: than a change to the stage.
CAPTURE_SETTINGS = {
    # Kit builds a small camera MESH for every camera prim on the stage. The
    # renderer draws it like any other geometry, so it lands in the AOV and
    # from there in the picture sent to be generated from.
    "/app/viewport/createCameraModelRep": False,
}


#: The eight bytes every PNG starts with, and the four naming its header
#: chunk. A PNG's dimensions are the first thing after them, at a fixed offset,
#: which is why reading them costs no decode.
_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
_PNG_HEADER_CHUNK = b"IHDR"


def png_size(data: bytes) -> tuple[int, int] | None:
    """The pixel dimensions of a PNG, read from its header.

    What this is FOR is the shape rather than the size: a capture comes back
    at whatever the viewport was rendering at, and that is the aspect ratio
    the generated image should be asked for. Reading it off the bytes that
    were captured, rather than asking the viewport afterwards, means the
    answer is the one that was actually photographed. The viewport can be
    resized, re-pointed or closed between the capture and the question, and
    the pixels in hand cannot.

    It also means the plugin never has to care whether the viewport is filling
    its frame or sitting letterboxed inside it. Either way the buffer that
    came back is the picture, so its shape is the picture's shape, and there
    is no setting for anyone to have to turn off first.

    None for anything that is not a PNG with a readable header. The caller
    treats that as "no opinion about the size" rather than as a failure: the
    capture itself is fine, and a run at the tool's default size is the
    behaviour that existed before this did.
    """
    if (
        len(data) < 24
        or not data.startswith(_PNG_SIGNATURE)
        or data[12:16] != _PNG_HEADER_CHUNK
    ):
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def png_aspect(data: bytes) -> float | None:
    """The width-over-height ratio of a PNG, or None when it has no readable one."""
    size = png_size(data)
    if size is None:
        return None
    width, height = size
    return width / height


def viewport_aspect() -> float | None:
    """The shape the active viewport is RENDERING at, or None when it cannot say.

    Asked before the captured bytes are, and the reason is a report from Base
    Editor: with Fill Viewport off and the render resolution set to 1024x1024,
    the buffer that came back was 1920x1080. The AOV is not reliably the
    resolution the viewport says it is rendering at, so a shape read off the
    buffer is a shape the user did not choose.

    `resolution` is Kit's own answer to this question rather than ours.
    `omni.kit.viewport.utility` computes the aspect ratio it frames prims with
    as `resolution[0] / resolution[1]`, and the viewport keeps that property
    equal to the canvas size while Fill Viewport is on and equal to the
    configured render resolution while it is off. So one property covers both
    halves of the original request: it follows the frame when the user is
    letting it, and follows the number they typed when they are not.

    None when there is no viewport, or when it will not answer. The caller
    falls back to the captured bytes, which is a worse answer than this and a
    much better one than nothing.
    """
    try:
        from omni.kit.viewport.utility import get_active_viewport
    except ImportError:
        # No Kit, which is the state in a test. Nothing to ask, and nothing
        # worth a traceback about it.
        return None

    try:
        viewport = get_active_viewport()
        if viewport is None:
            return None
        resolution = viewport.resolution
    except Exception:  # noqa: BLE001 - a viewport that cannot say is not a failure
        logger.exception("RunDiffusion: could not read the viewport resolution.")
        return None

    if not resolution or len(resolution) < 2:
        return None
    width, height = float(resolution[0]), float(resolution[1])
    if width <= 0 or height <= 0:
        return None
    return width / height


def captured_aspect(data: bytes) -> float | None:
    """The shape a run made from this capture should be asked for.

    The viewport's own resolution first, the captured pixels second. See
    `viewport_aspect` for why they can disagree and why the viewport is the
    one to believe: it is the number the user set, where the buffer is
    whatever the renderer handed back.
    """
    return viewport_aspect() or png_aspect(data)


class CaptureError(RuntimeError):
    """Raised when there is no viewport to capture or the buffer is unusable."""


#: One capture at a time, across the whole extension.
#:
#: Not a nicety. A capture BORROWS things: the viewport's camera, and the
#: setting that draws camera meshes. Two overlapping captures each save what
#: they found and each put back what they saved, and what the second one finds
#: is what the first one had already changed. Reported from Base Editor after a
#: few captures in a row: camera prims stopped being drawn in the viewport at
#: all and stayed that way, because the second capture recorded "meshes already
#: off" as the state to return to and then restored it after the first capture
#: had restored the truth.
#:
#: Overlap is ordinary here rather than exotic. A capture holds the viewport for
#: the length of its settle, about a second, and a camera still is taken
#: whenever a viewfinder has nothing to show. The lock also settles the worse
#: version of the same race, where two captures fight over the camera path and
#: one of them reads the other one's framing.
#:
#: Made lazily, and remade when the loop changes. A lock binds itself to the
#: loop it first waits on and refuses to be waited on from any other, and this
#: module is imported long before Kit's loop exists. Remaking it also covers
#: the extension being disabled and re-enabled while the module stays imported,
#: which is what a developer does all day: the module-level lock would still be
#: holding the loop from the previous session, and the first two captures to
#: overlap would raise instead of queueing.
_capture_lock: asyncio.Lock | None = None
_capture_lock_loop = None


def _lock() -> asyncio.Lock:
    global _capture_lock, _capture_lock_loop
    loop = asyncio.get_event_loop()
    if _capture_lock is None or _capture_lock_loop is not loop:
        _capture_lock = asyncio.Lock()
        _capture_lock_loop = loop
    return _capture_lock


def _capsule_to_bytes(buffer, buffer_size: int) -> bytes:
    """Copy a PyCapsule-wrapped render buffer into owned Python bytes.

    The copy is not incidental. The pointer is only valid for the duration of
    the callback, so anything that outlives it (the PNG encode, the HTTP body)
    has to own its own memory.
    """
    ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
    ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    pointer = ctypes.pythonapi.PyCapsule_GetPointer(buffer, None)
    if not pointer:
        raise CaptureError("The viewport capture returned an empty buffer.")
    return bytes((ctypes.c_byte * buffer_size).from_address(pointer))


def _encode_png(raw: bytes, width: int, height: int) -> bytes:
    """Encode an RGBA8 buffer to PNG.

    Pillow rather than a hand-rolled encoder because Kit prebundles it: PIL
    12.2.0 cp312 ships inside omni.kit.pip_archive in both the stock SDK and the
    packaged Composer host, so this costs no vendored wheel and no pip at load.
    """
    from PIL import Image  # imported lazily so a UI import never pulls PIL

    image = Image.frombytes("RGBA", (width, height), raw)
    sink = io.BytesIO()
    image.save(sink, format="PNG")
    return sink.getvalue()


def _active_viewport():
    """The viewport to read, or a CaptureError saying there is not one.

    Not having one is a real state rather than a defect: a Kit app can be
    running with every viewport closed.
    """
    from omni.kit.viewport.utility import get_active_viewport

    viewport = get_active_viewport()
    if viewport is None:
        raise CaptureError("There is no active viewport to capture.")
    return viewport


async def capture_active_viewport_png() -> bytes:
    """Capture whatever the active viewport is pointing at, as PNG bytes.

    The live viewfinder's route, and deliberately the cheap one: no settle, and
    no furniture hidden. It shows the viewport as the user sees it, and a
    preview that flickered the camera meshes off twice a second while they
    orbited would be worse than one that shows what is actually there.

    It still takes the lock. A preview read landing in the middle of a real
    capture would be reading a viewport pointed at somebody else's camera.
    """
    viewport = _active_viewport()
    async with _lock():
        return await _capture_viewport_png(viewport)


async def capture_png(camera_path: str | None = None) -> bytes:
    """Capture from `camera_path`, or from the active viewport when it is None.

    Two things are borrowed and given back, and both restores are in a
    `finally` for the same reason: what is being borrowed is the scene the
    user is in the middle of working on.

    **The viewport, for a named camera.** Point it at the camera, let the
    renderer catch up, read the buffer, put it back. Borrowing is visible, and
    that is the trade. The alternative is `create_viewport_window(camera_path=
    ...)`, which does not disturb what is on screen but does put a second
    viewport window into a Kit app for the duration, and a viewport that is not
    visible is not guaranteed to render at all. Borrowing has no such doubt.

    **The camera meshes, for every capture.** See `_without_capture_furniture`.

    Anything that goes wrong in between (a camera prim deleted mid-capture, a
    timed-out readback, the app shutting down) must leave both exactly as they
    were found.
    """
    viewport = _active_viewport()

    async with _lock():
        return await _capture_locked(viewport, camera_path)


async def _capture_locked(viewport, camera_path: str | None) -> bytes:
    """The borrowing itself. Only ever entered by one caller at a time."""
    previous_camera = None
    if camera_path is not None:
        # Asked BEFORE anything is borrowed, because a camera that is not there
        # is not an error Kit reports. Assigning a path that no longer resolves
        # leaves the viewport on whatever it was already showing, so without
        # this the capture succeeds, returns a picture of somewhere else, and
        # is labelled with the camera that was asked for. Reported from Base
        # Editor after deleting a camera with the panel open: it quietly
        # captured the perspective view instead and said nothing.
        from . import cameras

        if not cameras.exists(camera_path):
            # Just the fact. Telling someone to choose another camera was
            # advice that stopped being true: the field puts itself back on
            # the active viewport when the row next redraws, and says so.
            raise CaptureError(f"{camera_path} is no longer on the stage.")
        try:
            previous_camera = viewport.camera_path
        except Exception as error:  # noqa: BLE001 - a viewport mid-teardown has none
            raise CaptureError("The viewport could not be read.") from error

    with _without_capture_furniture():
        try:
            if camera_path is not None:
                viewport.camera_path = camera_path
                # Belt and braces after the check above. The prim can be there
                # and the viewport still refuse it, and the failure mode is the
                # same silent one: a picture of the wrong place, labelled with
                # the right name.
                if not _points_at(viewport, camera_path):
                    raise CaptureError(
                        f"The viewport would not look through {camera_path}."
                    )
            # Awaited for EVERY capture now, not only a camera change. Hiding
            # the camera meshes is a change to what is being rendered just as
            # much as moving the camera is, and reading the buffer on the frame
            # it is asked for would read the frame that still had them in it.
            await _settle(viewport)
            return await _capture_viewport_png(viewport)
        finally:
            if camera_path is not None:
                try:
                    viewport.camera_path = previous_camera
                except Exception:  # noqa: BLE001 - nothing here can help the user
                    logger.exception(
                        "RunDiffusion: could not restore the viewport camera."
                    )


@contextlib.contextmanager
def _without_capture_furniture():
    """Take the app's own scene furniture out of shot, then put it back.

    Reported from Base Editor: a capture of the active viewport had a camera in
    it. Not a gizmo drawn over the picture but a MODEL, because Kit builds a
    small camera mesh for every camera prim on the stage and the renderer draws
    it like anything else. It is in the AOV, so it is in the capture, and from
    there it is in the image the model is asked to work from.

    `/app/viewport/createCameraModelRep` is the lever Kit uses for this itself:
    `omni.kit.viewport.actions` documents `removeCameraMeshes` as deciding
    "whether camera visibility should remove the child-camera mesh or toggle
    the parent camera's visibility", and takes this route by default. That
    distinction is the whole reason this setting is the one used here. The
    other route writes USD `visibility` attributes onto the user's camera
    prims, which is an edit to their stage, would land in their undo stack, and
    would be a scandalous thing for a screenshot to do.

    A setting is app state rather than scene state, so this is borrowed and
    returned like the camera path above it.

    Deliberately not conditional on anything. A camera model in frame is never
    what someone wanted to send to an image model, so there is no preference to
    offer and nothing to decide. Lights and the grid are the same lever if they
    ever need to come out too: one more entry each.
    """
    try:
        import carb.settings
    except ImportError:
        # No carb means no Kit, and no Kit means nothing drew a camera mesh.
        yield
        return

    settings = carb.settings.get_settings()
    restore: list[tuple[str, object]] = []
    try:
        for key, value in CAPTURE_SETTINGS.items():
            try:
                restore.append((key, settings.get(key)))
                settings.set(key, value)
            except Exception:  # noqa: BLE001 - a setting is not worth a capture
                logger.exception("RunDiffusion: could not set %s.", key)
        yield
    finally:
        for key, value in restore:
            try:
                if value is None:
                    # Never set, so there is nothing to set it back TO. Writing
                    # None would leave a null where Kit expects either its own
                    # default or nothing at all, which is not the state this
                    # found and not a state anything else knows how to read.
                    settings.destroy_item(key)
                else:
                    settings.set(key, value)
            except Exception:  # noqa: BLE001 - nothing here can help the user
                logger.exception("RunDiffusion: could not restore %s.", key)


def _points_at(viewport, camera_path: str) -> bool:
    """Whether the viewport actually took the camera it was given.

    Compared as text because the property hands back an `Sdf.Path` while the
    caller holds a string, and `Sdf.Path("/World/Cam") != "/World/Cam"`.

    A viewport that cannot say is taken at its word. Refusing every capture
    because one getter moved would be a worse failure than the one this
    guards, and the check above has already established that the camera is
    real.
    """
    try:
        return str(viewport.camera_path) == str(camera_path)
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("RunDiffusion: could not confirm the viewport camera.")
        return True


async def _settle(viewport) -> None:
    """Let the renderer draw into the new camera before the buffer is read.

    Frames rather than seconds. A capture taken on the frame the camera moves
    is whatever the renderer had reached, which in Path Tracing is the first
    few samples of an image that needs far more, and `next_viewport_frame_async`
    counts delivered frames instead of guessing at wall-clock.

    A failure here is not fatal. A capture that is noisier than it should be is
    worth more than no capture at all, and the alternative is a button that
    stops working because one optional utility moved. Running out of patience
    is treated the same way and for the same reason: see
    CAMERA_SETTLE_TIMEOUT_SECONDS for why waiting forever is the one outcome
    this must not have.
    """
    try:
        from omni.kit.viewport.utility import next_viewport_frame_async

        await asyncio.wait_for(
            next_viewport_frame_async(viewport, CAMERA_SETTLE_FRAMES),
            timeout=CAMERA_SETTLE_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        # Logged as a warning rather than an exception: there is no traceback
        # worth printing, and the caller carries on to read the buffer.
        logger.warning(
            "RunDiffusion: the viewport did not deliver %s frames within %s "
            "seconds; capturing what is there.",
            CAMERA_SETTLE_FRAMES,
            CAMERA_SETTLE_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("RunDiffusion: could not wait for the camera to settle.")


async def _capture_viewport_png(viewport) -> bytes:
    """Read `viewport`'s buffer and encode it. The mechanics, with no policy."""
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()

    def on_capture(buffer, buffer_size, width, height, byte_format):
        # Runs on a render thread. Do the pointer copy here, where the pointer
        # is still valid, and let the encode happen on the awaiting side.
        if future.done():
            return
        try:
            raw = _capsule_to_bytes(buffer, buffer_size)
            loop.call_soon_threadsafe(future.set_result, (raw, width, height))
        except Exception as error:  # noqa: BLE001 - the future carries it across the thread
            logger.exception("RunDiffusion: viewport capture failed.")
            loop.call_soon_threadsafe(future.set_exception, error)

    from omni.kit.viewport.utility import capture_viewport_to_buffer

    # The return value is not optional. `capture_viewport_to_buffer` hands back
    # a future-like capture object, and the callback only runs once that object
    # is awaited: `wait_for_result` awaits the internal future and then pumps a
    # couple of completion frames. Dropping it, as the first draft of this file
    # did, means the callback never fires and the await below hangs forever with
    # no error. A headless probe caught exactly that on 2026-08-17.
    capture = capture_viewport_to_buffer(viewport, on_capture)

    # Bounded, because an unbounded await here is indistinguishable from a
    # frozen panel: the status line would read "Capturing viewport..." forever
    # with nothing logged. A capture that has not landed in this long is not
    # going to, and a message naming the timeout is diagnosable where a hang is
    # not. Observed on 2026-08-17 in a headless probe app, where the capture
    # never scheduled at all.
    try:
        await asyncio.wait_for(capture.wait_for_result(), timeout=CAPTURE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise CaptureError(
            f"The viewport capture did not complete within {CAPTURE_TIMEOUT_SECONDS} seconds."
        ) from None

    # Belt and braces after the hang above. If the capture completed without
    # ever delivering a buffer, fail loudly rather than awaiting a future that
    # nothing will ever resolve.
    if not future.done():
        raise CaptureError("The viewport capture completed without returning an image.")

    raw, width, height = await future
    if not raw or width <= 0 or height <= 0:
        raise CaptureError("The viewport capture produced no pixels.")

    return _encode_png(raw, width, height)
