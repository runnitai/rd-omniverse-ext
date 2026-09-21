"""What the panel says while a capture is being taken.

Every capture now settles for sixty frames before it reads a buffer, because
hiding the camera meshes changes what is being rendered just as much as moving
the camera does. On a heavy path-traced stage that is seconds.

The panel said "Capturing ..." for a named camera only, and said it into the
status line, which is off by default. So on the active viewport, which is where
most people start, pressing Capture produced no visible change at all: the
panel looked frozen and the button looked like a click that had missed. It is
said in the viewfinder now, for every source, and only there.

The camera is also looked up once rather than twice. Each lookup walks every
prim on the stage, and the second one only existed to write the same name into
the slot that the first had already put on screen.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from rundiffusion_omniverse import panel as panel_module
from rundiffusion_omniverse.form import CAPTURING_TEXT
from rundiffusion_omniverse.panel import ACTIVE_VIEWPORT_LABEL, RunDiffusionPanel
from rundiffusion_omniverse.viewport_capture import CaptureError


class FakeForm:
    """Enough of ToolForm to record what the panel tells it about a capture."""

    def __init__(self) -> None:
        self.busy: list = []
        self.errors: list = []
        self.added: list = []
        self.cleared_errors: list = []
        self.sources: dict = {}
        self.image_field_keys: list = ["image"]
        self.matched_aspects: list = []
        self.pinning: dict = {}
        #: The shape a capture is cropped to: a size picked by hand, or None.
        self.forced_aspect = None

    def capture_aspect(self):
        return self.forced_aspect

    def show_capture_busy(self, key: str, message: str) -> None:
        self.busy.append(("show", key, message))

    def clear_capture_busy(self, key: str) -> None:
        self.busy.append(("clear", key))

    def show_capture_error(self, key: str, message: str) -> None:
        self.errors.append((key, message))

    def clear_capture_error(self, key: str) -> None:
        self.cleared_errors.append(key)

    def add_capture(
        self, key: str, png, thumbnail, description: str = "", camera_path=None
    ):
        self.added.append((key, png, description, camera_path))
        return 1

    def pin_view(self, key: str) -> bool:
        return bool(self.pinning.get(key))

    def export_capture_sources(self) -> dict:
        return dict(self.sources)

    def restore_capture_sources(self, state) -> None:
        self.sources = dict(state or {})

    def capture_source(self, key: str):
        return self.sources.get(key)

    def match_capture_aspect(self, aspect) -> None:
        self.matched_aspects.append(aspect)


class FakeStills:
    def __init__(self) -> None:
        self.forgotten: list = []

    def forget(self, path: str) -> None:
        self.forgotten.append(path)

    def refresh(self, path: str, _spawn) -> None:
        return None


def _panel(monkeypatch, tmp_path, *, png=b"png-bytes", fail=None, cameras=()):
    async def capture(camera_path=None):
        if fail is not None:
            raise fail
        return png

    monkeypatch.setattr(panel_module, "capture_png", capture)
    monkeypatch.setattr(panel_module, "write_thumbnail", lambda _png, _path: True)

    made = object.__new__(RunDiffusionPanel)
    made._form = FakeForm()
    made._result_dir = tmp_path
    made._camera_stills = FakeStills()
    made._camera_sources = [(ACTIVE_VIEWPORT_LABEL, None), *cameras]
    # Far enough in the future that the cache above is what answers, so these
    # tests never reach a USD stage they do not have.
    made._camera_sources_read_at = float("inf")
    made.statuses = []
    made.errors = []
    made._set_status = made.statuses.append
    made._set_error = made.errors.append
    made._spawn = lambda coroutine: coroutine
    return made


def _png(width: int, height: int) -> bytes:
    """A PNG header, which is all the captured shape is ever read from."""
    return (
        b'\x89PNG\r\n\x1a\n'
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


class TestSayingACaptureIsUnderWay:
    def test_the_active_viewport_says_so_too(self, monkeypatch, tmp_path):
        """The path that just became slow, and the one that said nothing."""
        panel = _panel(monkeypatch, tmp_path)

        _run(panel._capture_into("image", None))

        assert ("show", "image", CAPTURING_TEXT) in panel._form.busy

    def test_a_named_camera_says_so_the_same_way(self, monkeypatch, tmp_path):
        """The frame itself shows which camera it is looking through, so the
        band does not have to repeat the name."""
        panel = _panel(
            monkeypatch, tmp_path, cameras=[("Lobby", "/World/Cameras/Lobby")]
        )

        _run(panel._capture_into("image", "/World/Cameras/Lobby"))

        assert ("show", "image", CAPTURING_TEXT) in panel._form.busy

    def test_it_is_said_in_the_frame_and_not_in_the_status_line(
        self, monkeypatch, tmp_path
    ):
        """The status line is off by default and a panel's height from the
        frame, so a second copy there was one nobody read."""
        panel = _panel(monkeypatch, tmp_path)

        _run(panel._capture_into("image", None))

        assert panel.statuses == []
        assert panel._form.busy[0][0] == "show"

    def test_it_stops_saying_it_once_the_capture_lands(self, monkeypatch, tmp_path):
        panel = _panel(monkeypatch, tmp_path)

        _run(panel._capture_into("image", None))

        assert ("clear", "image") in panel._form.busy

    def test_it_stops_saying_it_when_the_capture_is_refused(
        self, monkeypatch, tmp_path
    ):
        """Otherwise a field that refused once claims to be capturing for as
        long as the panel is open."""
        panel = _panel(
            monkeypatch, tmp_path, fail=CaptureError("No viewport.")
        )

        _run(panel._capture_into("image", None))

        assert ("clear", "image") in panel._form.busy
        assert panel._form.errors == [("image", "No viewport.")]

    def test_the_refusal_is_what_is_left_on_screen(self, monkeypatch, tmp_path):
        """Cleared AFTER the refusal has been recorded, so the one coalesced
        redraw draws the refusal rather than a blank where the line was."""
        panel = _panel(
            monkeypatch, tmp_path, fail=CaptureError("No viewport.")
        )

        _run(panel._capture_into("image", None))

        assert panel._form.busy[-1] == ("clear", "image")


class TestACaptureThatFailsSomeOtherWay:
    """`capture_png` raises more than CaptureError: a viewport getter that
    moved, an optional import that is not there, a USD call that raised. The
    status line is off by default, so without a message the busy text simply
    disappears and the button looks like a click that missed."""

    def test_it_is_reported_under_the_button(self, monkeypatch, tmp_path):
        panel = _panel(monkeypatch, tmp_path, fail=RuntimeError("the viewport moved"))

        _run(panel._capture_into("image", None))

        assert panel._form.errors, "nothing was said under the button"
        assert "the viewport moved" in panel._form.errors[0][1]

    def test_the_busy_line_still_goes(self, monkeypatch, tmp_path):
        panel = _panel(monkeypatch, tmp_path, fail=RuntimeError("boom"))

        _run(panel._capture_into("image", None))

        assert ("clear", "image") in panel._form.busy

    def test_no_image_is_pinned(self, monkeypatch, tmp_path):
        panel = _panel(monkeypatch, tmp_path, fail=RuntimeError("boom"))

        _run(panel._capture_into("image", None))

        assert panel._form.added == []


class TestNamingTheCameraOnce:
    def test_the_slot_records_the_camera_it_came_from(self, monkeypatch, tmp_path):
        panel = _panel(
            monkeypatch, tmp_path, cameras=[("Lobby", "/World/Cameras/Lobby")]
        )

        _run(panel._capture_into("image", "/World/Cameras/Lobby"))

        assert panel._form.added[0][2] == "Captured from Lobby"

    def test_a_viewport_capture_still_says_nothing_in_particular(
        self, monkeypatch, tmp_path
    ):
        """It has nothing more specific to say than the words the compare
        picker already uses for it."""
        panel = _panel(monkeypatch, tmp_path)

        _run(panel._capture_into("image", None))

        assert panel._form.added[0][2] == ""


class TestKeepingTheViewACaptureWasTakenFrom:
    """Ticking a box is what authors a camera prim. Nothing else does.

    The rule is not a preference. An ordinary capture must leave the user's
    stage exactly as it found it, or an afternoon's work leaves a hundred
    cameras behind that nobody asked for and somebody has to delete.
    """

    def _pinning(self, monkeypatch, pinned="/RunDiffusion/Cameras/View"):
        saved: list = []

        def save_active_view(*args, **kwargs):
            saved.append((args, kwargs))
            return pinned

        monkeypatch.setattr(
            panel_module.render_cameras, "save_active_view", save_active_view
        )
        return saved

    def test_an_ordinary_capture_authors_nothing(self, monkeypatch, tmp_path):
        saved = self._pinning(monkeypatch)
        panel = _panel(monkeypatch, tmp_path)

        _run(panel._capture_into("image", None))

        assert saved == []

    def test_the_framing_is_kept_when_the_box_is_ticked(self, monkeypatch, tmp_path):
        saved = self._pinning(monkeypatch)
        panel = _panel(monkeypatch, tmp_path)
        panel._form.pinning["image"] = True

        _run(panel._capture_into("image", None))

        assert len(saved) == 1

    def test_and_the_slot_remembers_which_camera_it_was(self, monkeypatch, tmp_path):
        """This is the whole of what makes the framing returnable: it travels
        with the slot into the run and from there onto the result."""
        self._pinning(monkeypatch)
        panel = _panel(monkeypatch, tmp_path)
        panel._form.pinning["image"] = True

        _run(panel._capture_into("image", None))

        assert panel._form.added[0][3] == "/RunDiffusion/Cameras/View"

    def test_the_slot_is_named_after_the_camera_that_was_kept(
        self, monkeypatch, tmp_path
    ):
        self._pinning(monkeypatch)
        panel = _panel(monkeypatch, tmp_path)
        panel._form.pinning["image"] = True

        _run(panel._capture_into("image", None))

        assert panel._form.added[0][2] == "Captured from View"

    def test_a_named_camera_is_never_copied(self, monkeypatch, tmp_path):
        """It is already reproducible, under the user's own name, in their own
        outliner. A second copy of it would be clutter offered as a feature."""
        saved = self._pinning(monkeypatch)
        panel = _panel(
            monkeypatch, tmp_path, cameras=[("Lobby", "/World/Cameras/Lobby")]
        )
        panel._form.pinning["image"] = True

        _run(panel._capture_into("image", "/World/Cameras/Lobby"))

        assert saved == []
        assert panel._form.added[0][3] == "/World/Cameras/Lobby"

    def test_a_framing_that_could_not_be_kept_still_leaves_the_image(
        self, monkeypatch, tmp_path
    ):
        """The pixels are already in hand and the run is not lost. What was
        lost is the button they pressed, and that is said out loud rather than
        left to look like it worked."""
        self._pinning(monkeypatch, pinned=None)
        panel = _panel(monkeypatch, tmp_path)
        panel._form.pinning["image"] = True

        _run(panel._capture_into("image", None))

        assert panel._form.added[0][3] is None
        assert panel._form.errors == [
            ("image", "Captured, but that view could not be kept as a camera.")
        ]


class TestTakingTheShapeOfTheViewIntoTheRun:
    def test_the_captured_aspect_reaches_the_form(self, monkeypatch, tmp_path):
        panel = _panel(monkeypatch, tmp_path, png=_png(1600, 900))

        _run(panel._capture_into("image", None))

        assert panel._form.matched_aspects == [pytest.approx(16 / 9)]

    def test_a_capture_that_is_not_a_readable_png_moves_no_size(
        self, monkeypatch, tmp_path
    ):
        """None means no opinion, so the tool's own default is left alone."""
        panel = _panel(monkeypatch, tmp_path, png=b"not a png")

        _run(panel._capture_into("image", None))

        assert panel._form.matched_aspects == [None]

    def test_a_refused_capture_has_no_shape_to_offer(self, monkeypatch, tmp_path):
        panel = _panel(monkeypatch, tmp_path, fail=CaptureError("No viewport."))

        _run(panel._capture_into("image", None))

        assert panel._form.matched_aspects == []


class TestCroppingToTheChosenSize:
    def test_a_capture_is_cropped_to_a_size_picked_by_hand(self, monkeypatch, tmp_path):
        from io import BytesIO

        Image = pytest.importorskip("PIL.Image")
        buffer = BytesIO()
        Image.new("RGB", (160, 90)).save(buffer, format="PNG")
        panel = _panel(monkeypatch, tmp_path, png=buffer.getvalue())
        panel._form.forced_aspect = 1.0

        _run(panel._capture_into("image", None))

        with Image.open(BytesIO(panel._form.added[0][1])) as sent:
            assert sent.size == (90, 90)
        assert panel._form.matched_aspects == [1.0]

    def test_without_one_the_capture_is_sent_whole(self, monkeypatch, tmp_path):
        panel = _panel(monkeypatch, tmp_path, png=_png(1600, 900))

        _run(panel._capture_into("image", None))

        assert panel._form.added[0][1] == _png(1600, 900)
