"""Borrowing the viewport to look through a named camera, and giving it back.

The capture itself needs a renderer and is not tested here. The
borrowing is, because it touches something the user owns: their viewport, in a
scene they are in the middle of working on. Getting it back is not a nicety,
and the failure mode is not subtle.

The order matters too. Point, WAIT, read: a readback on the frame the camera
moves is whatever the renderer had reached, which in Path Tracing is the first
few samples of an image that needs far more.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from rundiffusion_omniverse import cameras as viewport_capture_cameras
from rundiffusion_omniverse import viewport_capture
from rundiffusion_omniverse.viewport_capture import (
    CAMERA_SETTLE_FRAMES,
    CAPTURE_SETTINGS,
    CaptureError,
    capture_active_viewport_png,
    capture_png,
)

VIEWPORT_CAMERA = "/OmniverseKit_Persp"
CAMERA_MESHES = "/app/viewport/createCameraModelRep"


class FakeViewport:
    """A viewport that records everything done to its camera."""

    def __init__(self, events: list) -> None:
        self.events = events
        self._camera = VIEWPORT_CAMERA

    @property
    def camera_path(self) -> str:
        return self._camera

    @camera_path.setter
    def camera_path(self, value: str) -> None:
        self._camera = value
        self.events.append(("point", value))


class FakeSettings:
    """Enough of carb.settings to be borrowed from and given back to."""

    def __init__(self, events: list) -> None:
        self.events = events
        # True is the state a Kit app is in by default, and the state the user
        # is entitled to find it in again afterwards.
        self.values = {CAMERA_MESHES: True}

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value) -> None:
        self.values[key] = value
        self.events.append(("setting", key, value))


@pytest.fixture
def kit(monkeypatch):
    """Stand in for omni.kit.viewport.utility and carb, which need a running app."""
    events: list = []
    viewport = FakeViewport(events)
    settings = FakeSettings(events)
    state = types.SimpleNamespace(
        viewport=viewport,
        events=events,
        fail=None,
        settings=settings,
        on_stage={"/World/Cameras/Lobby", "/World/Cameras/Atrium"},
    )

    carb_settings = types.ModuleType("carb.settings")
    carb_settings.get_settings = lambda: state.settings
    carb_package = types.ModuleType("carb")
    carb_package.settings = carb_settings
    monkeypatch.setitem(sys.modules, "carb", carb_package)
    monkeypatch.setitem(sys.modules, "carb.settings", carb_settings)

    async def next_viewport_frame_async(_viewport, n_frames: int = 0) -> None:
        events.append(("wait", n_frames))
        # A real settle takes about a second of frames, so it is the window in
        # which a second capture arrives. Yielding here is what lets the
        # concurrency tests below be about the code rather than about luck.
        await asyncio.sleep(0)

    module = types.ModuleType("omni.kit.viewport.utility")
    module.get_active_viewport = lambda *_a, **_k: state.viewport
    module.next_viewport_frame_async = next_viewport_frame_async

    package = types.ModuleType("omni.kit")
    viewport_package = types.ModuleType("omni.kit.viewport")
    viewport_package.utility = module
    package.viewport = viewport_package
    monkeypatch.setitem(sys.modules, "omni.kit", package)
    monkeypatch.setitem(sys.modules, "omni.kit.viewport", viewport_package)
    monkeypatch.setitem(sys.modules, "omni.kit.viewport.utility", module)

    async def fake_capture(viewport_arg) -> bytes:
        # The camera in force AT THE MOMENT OF THE READ, which is the whole
        # question this module exists to answer, and whether the camera meshes
        # were out of shot when it was taken.
        events.append(("read", viewport_arg.camera_path))
        events.append(("meshes-at-read", state.settings.get(CAMERA_MESHES)))
        if state.fail is not None:
            raise state.fail
        return b"png-bytes"

    monkeypatch.setattr(viewport_capture, "_capture_viewport_png", fake_capture)
    # Every camera is on the stage unless a test says otherwise. The real one
    # reads USD, which needs a running Kit.
    monkeypatch.setattr(
        viewport_capture_cameras, "exists", lambda path: path in state.on_stage
    )
    return state


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


class TestTheActiveViewport:
    def test_it_is_never_pointed_anywhere(self, kit):
        """What every capture was before this. Pointing a viewport at the
        camera it is already using would still make it flicker."""
        _run(capture_png(None))

        assert not [event for event in kit.events if event[0] == "point"]

    def test_it_is_still_waited_on_before_being_read(self, kit):
        """Not because the camera moved, but because the camera meshes were
        just taken out of shot and the renderer has to draw a frame without
        them before there is anything worth reading."""
        _run(capture_png(None))

        assert ("wait", CAMERA_SETTLE_FRAMES) in kit.events
        assert kit.events.index(("wait", CAMERA_SETTLE_FRAMES)) < kit.events.index(
            ("read", VIEWPORT_CAMERA)
        )

    def test_no_viewport_at_all_says_so(self, kit):
        """A real state rather than a defect: a Kit app can run with every
        viewport closed."""
        kit.viewport = None

        with pytest.raises(CaptureError):
            _run(capture_png(None))

    def test_a_named_camera_with_no_viewport_says_so_too(self, kit):
        """And says it before anything has been pointed anywhere."""
        kit.viewport = None

        with pytest.raises(CaptureError):
            _run(capture_png("/World/Cameras/Lobby"))

        assert kit.events == []


class TestANamedCamera:
    def test_the_viewport_is_pointed_waited_on_and_read_in_that_order(self, kit):
        _run(capture_png("/World/Cameras/Lobby"))

        assert [event for event in kit.events if event[0] != "setting"][:3] == [
            ("point", "/World/Cameras/Lobby"),
            ("wait", CAMERA_SETTLE_FRAMES),
            ("read", "/World/Cameras/Lobby"),
        ]

    def test_the_user_gets_their_view_back(self, kit):
        _run(capture_png("/World/Cameras/Lobby"))

        points = [event for event in kit.events if event[0] == "point"]
        assert points[-1] == ("point", VIEWPORT_CAMERA)
        assert kit.viewport.camera_path == VIEWPORT_CAMERA

    def test_they_get_it_back_even_when_the_capture_fails(self, kit):
        """The point of the finally. A camera prim deleted mid-capture must not
        leave someone looking through a camera they did not choose, in a scene
        they were in the middle of working on."""
        kit.fail = CaptureError("the capture produced no pixels")

        with pytest.raises(CaptureError):
            _run(capture_png("/World/Cameras/Lobby"))

        assert kit.viewport.camera_path == VIEWPORT_CAMERA

    def test_a_renderer_that_cannot_be_waited_on_still_captures(self, kit, monkeypatch):
        """A capture noisier than it should be is worth more than a button that
        stopped working because one optional utility moved."""

        async def missing(*_args, **_kwargs):
            raise RuntimeError("next_viewport_frame_async is gone")

        monkeypatch.setattr(
            sys.modules["omni.kit.viewport.utility"],
            "next_viewport_frame_async",
            missing,
        )

        assert _run(capture_png("/World/Cameras/Lobby")) == b"png-bytes"
        assert kit.viewport.camera_path == VIEWPORT_CAMERA

    def test_the_bytes_come_back(self, kit):
        assert _run(capture_png("/World/Cameras/Lobby")) == b"png-bytes"


class TestTakingTheCameraMeshesOutOfShot:
    """Reported from Base Editor: a capture of the active viewport had a camera
    in it. Kit builds a small camera MODEL for every camera prim and the
    renderer draws it like any other geometry, so it is in the AOV and from
    there in the picture the model is asked to work from."""

    def test_they_are_hidden_before_the_buffer_is_read(self, kit):
        _run(capture_png(None))

        assert ("meshes-at-read", False) in kit.events

    def test_they_are_hidden_for_a_named_camera_too(self, kit):
        """Other cameras are in the shot a named camera sees, just as they are
        in the shot the viewport sees."""
        _run(capture_png("/World/Cameras/Lobby"))

        assert ("meshes-at-read", False) in kit.events

    def test_the_setting_is_put_back_afterwards(self, kit):
        """A setting is app state rather than scene state, but it is still not
        this plugin's to keep."""
        _run(capture_png(None))

        assert kit.settings.get(CAMERA_MESHES) is True

    def test_it_is_put_back_even_when_the_capture_fails(self, kit):
        kit.fail = CaptureError("the capture produced no pixels")

        with pytest.raises(CaptureError):
            _run(capture_png(None))

        assert kit.settings.get(CAMERA_MESHES) is True

    def test_it_is_put_back_to_what_it_was_rather_than_to_true(self, kit):
        """Someone who had already turned camera meshes off is entitled to find
        them off."""
        kit.settings.values[CAMERA_MESHES] = False

        _run(capture_png(None))

        assert kit.settings.get(CAMERA_MESHES) is False

    def test_every_setting_in_the_table_is_covered(self, kit):
        """The table is meant to grow: lights and the grid are the same lever
        if they ever need to come out too."""
        _run(capture_png(None))

        for key in CAPTURE_SETTINGS:
            assert key in kit.settings.values

    def test_no_carb_at_all_still_captures(self, kit, monkeypatch):
        """No carb means no Kit, and no Kit means nothing drew a camera mesh."""
        monkeypatch.delitem(sys.modules, "carb.settings")
        monkeypatch.delitem(sys.modules, "carb")
        monkeypatch.setattr(
            "builtins.__import__",
            _refusing_to_import("carb", __import__),
        )

        assert _run(capture_png(None)) == b"png-bytes"


def _refusing_to_import(refused: str, real):
    def fake(name, *args, **kwargs):
        if name == refused or name.startswith(f"{refused}."):
            raise ImportError(f"no module named {name}")
        return real(name, *args, **kwargs)

    return fake


class TestAViewportThatStopsDeliveringFrames:
    """A settle counts DELIVERED frames, so a viewport that stops delivering
    them never finishes counting. Minimising Kit, hiding the viewport, a
    renderer that stalls: each stops delivery without stopping the await, and
    none of them is an error anything reports.

    What makes an unbounded wait worse here than a slow capture is what it
    holds while it waits. The settle runs inside the capture lock AND inside
    the block that hides the camera meshes, so waiting forever means the user
    keeps neither their viewport nor their camera prims, for the rest of the
    session.
    """

    @pytest.fixture
    def stalled(self, kit, monkeypatch):
        async def never(_viewport, _frames: int = 0) -> None:
            kit.events.append(("wait", "forever"))
            await asyncio.Event().wait()

        sys.modules["omni.kit.viewport.utility"].next_viewport_frame_async = never
        monkeypatch.setattr(
            viewport_capture, "CAMERA_SETTLE_TIMEOUT_SECONDS", 0.01
        )
        return kit

    def test_the_capture_still_lands(self, stalled):
        """A picture noisier than it should be is worth more than no picture,
        which is what this module already decided about a settle that fails."""
        assert _run(capture_png("/World/Cameras/Lobby")) == b"png-bytes"

    def test_the_camera_meshes_come_back(self, stalled):
        """The fault this exists to stop. Left off, camera prims stay invisible
        on the user's stage until the app is restarted."""
        _run(capture_png("/World/Cameras/Lobby"))

        assert stalled.settings.get(CAMERA_MESHES) is True

    def test_the_viewport_is_handed_back(self, stalled):
        _run(capture_png("/World/Cameras/Lobby"))

        assert stalled.viewport.camera_path == VIEWPORT_CAMERA

    def test_the_lock_is_released(self, stalled):
        """The wait is inside the lock, so an unbounded one wedges every later
        capture and every viewfinder refresh behind it."""

        async def two():
            first = await capture_png("/World/Cameras/Lobby")
            second = await capture_active_viewport_png()
            return first, second

        assert _run(two()) == (b"png-bytes", b"png-bytes")


class TestOneCaptureAtATime:
    """The bug this lock exists for, reported from Base Editor: after a few
    captures in a row, camera prims stopped being drawn in the viewport at all
    and stayed that way.

    A capture BORROWS. Two of them overlapping each save what they find and
    each put back what they saved, so the second one saves the first one's
    change as if it were the truth, and restores it after the first has already
    put the real value back. The setting is left where nobody chose it.

    A settle is about a second of frames wide, and a camera still is taken
    whenever a viewfinder has nothing to show, so overlap is ordinary.
    """

    def test_two_captures_leave_the_setting_as_they_found_it(self, kit):
        loop = asyncio.new_event_loop()

        async def both():
            return await asyncio.gather(capture_png(None), capture_png(None))

        loop.run_until_complete(both())

        assert kit.settings.get(CAMERA_MESHES) is True

    def test_a_capture_overlapping_a_camera_capture_leaves_it_too(self, kit):
        loop = asyncio.new_event_loop()

        async def both():
            return await asyncio.gather(
                capture_png("/World/Cameras/Lobby"), capture_png(None)
            )

        loop.run_until_complete(both())

        assert kit.settings.get(CAMERA_MESHES) is True

    def test_neither_capture_reads_the_others_framing(self, kit):
        """The worse version of the same race. Two captures fighting over the
        camera path means one of them silently returns a picture of somewhere
        else, which no error would ever report."""
        loop = asyncio.new_event_loop()

        async def both():
            return await asyncio.gather(
                capture_png("/World/Cameras/Lobby"),
                capture_png("/World/Cameras/Atrium"),
            )

        loop.run_until_complete(both())

        reads = [event[1] for event in kit.events if event[0] == "read"]
        assert sorted(reads) == ["/World/Cameras/Atrium", "/World/Cameras/Lobby"]

    def test_the_viewport_ends_up_where_the_user_left_it(self, kit):
        loop = asyncio.new_event_loop()

        async def both():
            return await asyncio.gather(
                capture_png("/World/Cameras/Lobby"),
                capture_png("/World/Cameras/Atrium"),
            )

        loop.run_until_complete(both())

        assert kit.viewport.camera_path == VIEWPORT_CAMERA

    def test_a_failing_capture_does_not_hold_the_lock(self, kit):
        """A lock released only on the happy path is a panel that stops
        capturing after its first bad frame and never says why."""
        loop = asyncio.new_event_loop()
        kit.fail = CaptureError("the capture produced no pixels")

        async def first_fails_then_second_succeeds():
            with pytest.raises(CaptureError):
                await capture_png(None)
            kit.fail = None
            return await capture_png(None)

        assert loop.run_until_complete(first_fails_then_second_succeeds()) == b"png-bytes"


class TestASettingThatWasNeverSet:
    def test_it_is_destroyed_rather_than_written_back_as_None(self, kit):
        """Writing None would leave a null where Kit expects either its own
        default or nothing at all, which is not the state this found and not a
        state anything else knows how to read."""
        destroyed: list = []
        kit.settings.values.pop(CAMERA_MESHES)
        kit.settings.destroy_item = destroyed.append

        _run(capture_png(None))

        assert destroyed == [CAMERA_MESHES]


class TestACameraThatIsNoLongerThere:
    """Reported from Base Editor: a camera was deleted with the panel open and
    Capture went ahead anyway, returning the perspective view with no message
    and recording it as having come from the deleted camera.

    Assigning a path that no longer resolves is not an error Kit reports. The
    viewport simply stays on what it was showing, so the capture succeeds and
    the result is a picture of somewhere else wearing the right name. A wrong
    answer delivered confidently, which is worse than a failure."""

    def test_it_refuses_rather_than_capturing_something_else(self, kit):
        kit.on_stage.clear()

        with pytest.raises(CaptureError) as refused:
            _run(capture_png("/World/Cameras/Lobby"))

        assert "/World/Cameras/Lobby" in str(refused.value)

    def test_it_says_so_before_borrowing_anything(self, kit):
        """No point pointing a viewport at a camera that is not there, and no
        excuse for having to put it back afterwards."""
        kit.on_stage.clear()

        with pytest.raises(CaptureError):
            _run(capture_png("/World/Cameras/Lobby"))

        assert kit.events == []

    def test_the_active_viewport_is_unaffected_by_the_rule(self, kit):
        """It has no camera path to check, and capturing it is what the plugin
        did before cameras existed at all."""
        kit.on_stage.clear()

        assert _run(capture_png(None)) == b"png-bytes"

    def test_a_viewport_that_will_not_take_the_camera_refuses_too(self, kit):
        """The prim can be there and the viewport still decline it. Same silent
        failure: a picture of the wrong place under the right name."""

        class Stubborn(FakeViewport):
            @property
            def camera_path(self) -> str:
                return VIEWPORT_CAMERA

            @camera_path.setter
            def camera_path(self, value: str) -> None:
                self.events.append(("point", value))

        kit.viewport = Stubborn(kit.events)

        with pytest.raises(CaptureError):
            _run(capture_png("/World/Cameras/Lobby"))

    def test_a_viewport_that_cannot_say_is_taken_at_its_word(self, kit):
        """Refusing every capture because one getter moved would be a worse
        failure than the one being guarded against."""

        class Mute(FakeViewport):
            @property
            def camera_path(self):
                raise RuntimeError("this viewport has no camera property")

            @camera_path.setter
            def camera_path(self, value):
                self.events.append(("point", value))

        mute = Mute(kit.events)
        kit.viewport = mute

        # Reading it for the restore fails first, which is its own clear error.
        with pytest.raises(CaptureError):
            _run(capture_png("/World/Cameras/Lobby"))
