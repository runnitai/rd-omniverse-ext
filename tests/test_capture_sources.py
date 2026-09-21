"""Choosing where a capture comes from, and saying so afterwards.

The dropdown beside each viewfinder, what the panel puts in it, and what the
run records about the choice.

Three things here are worth pinning.

**The active viewport leads and carries None.** It is what every capture was
before this, and a stage with no authored cameras still offers it and nothing
else. None rather than a path, so "no particular camera" is one value rather
than a path that has to be recognised everywhere.

**The choice is per field.** A tool with a start frame and an end frame is
exactly the case where two different cameras is the point.

**The camera is recorded with the capture.** A run made from three cameras is
three rows in the compare picker, and the whole reason for naming them is that
"captured view" three times over says nothing about which is which.

**The list is re-read as the stage changes, but not on every ask.** Listing the
cameras walks every prim on the stage, and the image row asks on every rebuild.
See CAMERA_LIST_CACHE_SECONDS.

**The panel is TOLD when the stage moves.** Being asked was not enough: for a
while the only thing that asked was an image row being rebuilt, so a camera
authored with the panel open did not appear in the dropdown until the user
happened to add an image or leave the tab and come back. Reported off the
branch, with switching tabs as the workaround people found.
"""

from __future__ import annotations

import time

from rundiffusion_omniverse import cameras as cameras_module
from rundiffusion_omniverse.cameras import StageCamera
from rundiffusion_omniverse.panel import (
    ACTIVE_VIEWPORT_LABEL,
    CAMERA_LIST_CACHE_SECONDS,
    RunDiffusionPanel,
)

LOBBY = StageCamera(path="/World/Cameras/Lobby", name="Lobby", label="Lobby")
ATRIUM = StageCamera(path="/World/Cameras/Atrium", name="Atrium", label="Atrium")


class FakeForm:
    """Enough of ToolForm to answer what a field is capturing from.

    The viewfinder serves one field at a time, the first unless `target` says
    otherwise, and it is live only while it is open (not folded to its strip)
    and pointed at the active viewport.
    """

    def __init__(
        self, sources: dict | None = None, target: str | None = None, open_frame: bool = True
    ) -> None:
        self.sources = sources or {}
        self.image_field_keys = list(self.sources)
        self.target = target if target is not None else next(iter(self.sources), None)
        self.open_frame = open_frame

    def capture_source(self, key: str):
        return self.sources.get(key)

    def viewfinder_is_live(self) -> bool:
        return (
            self.open_frame
            and self.target in self.sources
            and self.sources[self.target] is None
        )


def _panel(monkeypatch, cameras=(), form=None) -> RunDiffusionPanel:
    monkeypatch.setattr(
        cameras_module, "stage_cameras", lambda: list(cameras), raising=False
    )
    panel = object.__new__(RunDiffusionPanel)
    panel._form = form if form is not None else FakeForm()
    panel._camera_sources = None
    # Long enough ago that a refresh re-reads rather than waiting out the
    # window. `TestARefreshWaitsOutTheCacheWindow` covers the other side.
    panel._camera_sources_read_at = -1e6
    panel._camera_watch = _FakeWatch()
    return panel


class TestWhatIsOffered:
    def test_a_stage_with_no_cameras_offers_the_viewport_alone(self, monkeypatch):
        panel = _panel(monkeypatch)

        assert panel._capture_sources() == [(ACTIVE_VIEWPORT_LABEL, None)]

    def test_the_viewport_comes_first(self, monkeypatch):
        """It is what every capture was before this, and what someone reaching
        for the button without reading the dropdown will get."""
        panel = _panel(monkeypatch, cameras=[LOBBY, ATRIUM])

        assert panel._capture_sources()[0] == (ACTIVE_VIEWPORT_LABEL, None)

    def test_every_camera_is_offered_by_label_and_path(self, monkeypatch):
        panel = _panel(monkeypatch, cameras=[LOBBY, ATRIUM])

        assert panel._capture_sources()[1:] == [
            ("Lobby", "/World/Cameras/Lobby"),
            ("Atrium", "/World/Cameras/Atrium"),
        ]

    def test_a_burst_of_asks_walks_the_stage_once(self, monkeypatch):
        """Every image-row rebuild asks, and a rebuild is what adding,
        removing, selecting or reordering an image does. Each ask used to walk
        every prim on the stage, on the UI thread, so ordinary clicking on an
        architectural scene hitched the panel."""
        calls: list = []

        def cameras():
            calls.append(None)
            return [LOBBY]

        monkeypatch.setattr(cameras_module, "stage_cameras", cameras, raising=False)
        panel = _panel(monkeypatch, cameras=[LOBBY])
        monkeypatch.setattr(cameras_module, "stage_cameras", cameras, raising=False)

        panel._capture_sources()
        panel._capture_sources()
        panel._capture_sources()

        assert len(calls) == 1

    def test_the_stage_is_read_again_once_the_window_has_passed(self, monkeypatch):
        """Cameras are added and deleted while the panel is open, and a list
        held for the session would go stale with nothing to correct it."""
        calls: list = []

        def cameras():
            calls.append(None)
            return [LOBBY]

        monkeypatch.setattr(cameras_module, "stage_cameras", cameras, raising=False)
        panel = _panel(monkeypatch, cameras=[LOBBY])
        monkeypatch.setattr(cameras_module, "stage_cameras", cameras, raising=False)

        panel._capture_sources()
        # As though the last read were longer ago than the window allows.
        panel._camera_sources_read_at -= CAMERA_LIST_CACHE_SECONDS + 1
        panel._capture_sources()

        assert len(calls) == 2

    def test_a_caller_cannot_edit_the_list_the_next_one_is_handed(
        self, monkeypatch
    ):
        """The held list is the cache itself, so handing it out directly would
        let one caller's sort or trim become everyone's."""
        panel = _panel(monkeypatch, cameras=[LOBBY, ATRIUM])

        first = panel._capture_sources()
        first.clear()

        assert len(panel._capture_sources()) == 3


class TestNamingTheSource:
    def test_the_viewport_is_named_as_the_dropdown_names_it(self, monkeypatch):
        panel = _panel(monkeypatch, cameras=[LOBBY])

        assert panel._camera_label(None) == ACTIVE_VIEWPORT_LABEL

    def test_a_camera_is_named_by_its_label(self, monkeypatch):
        panel = _panel(monkeypatch, cameras=[LOBBY])

        assert panel._camera_label("/World/Cameras/Lobby") == "Lobby"

    def test_a_camera_deleted_since_the_capture_falls_back_to_its_path(
        self, monkeypatch
    ):
        """The path is still true and still identifies the framing, which is
        what the label is for."""
        panel = _panel(monkeypatch, cameras=[])

        assert panel._camera_label("/World/Cameras/Gone") == "/World/Cameras/Gone"


def _primed(panel) -> None:
    """Read the list once, as an image row would, then let the window lapse.

    A refresh inside CAMERA_LIST_CACHE_SECONDS re-books itself rather than
    walking the stage again, which is its own test. These are about what a
    refresh DOES once it runs.
    """
    panel._capture_sources()
    panel._camera_sources_read_at -= CAMERA_LIST_CACHE_SECONDS + 1


class TestBeingToldTheStageMoved:
    """The dropdown is rebuilt with the image row and by nothing else, so
    without a watch it says whatever was true at the last rebuild."""

    def test_a_new_camera_redraws_the_rows(self, monkeypatch):
        panel = _panel(monkeypatch, cameras=[LOBBY], form=FakeForm({"image": None}))
        redrawn: list = []
        panel._form.redraw = redrawn.append
        _primed(panel)  # what the row last drew
        monkeypatch.setattr(
            cameras_module, "stage_cameras", lambda: [LOBBY, ATRIUM], raising=False
        )

        panel._refresh_capture_sources()

        assert redrawn == ["image"]

    def test_the_first_camera_on_a_stage_that_had_none_redraws_them(
        self, monkeypatch
    ):
        """The case reported: start with no cameras, author one."""
        panel = _panel(monkeypatch, cameras=[], form=FakeForm({"image": None}))
        redrawn: list = []
        panel._form.redraw = redrawn.append
        _primed(panel)
        monkeypatch.setattr(
            cameras_module, "stage_cameras", lambda: [LOBBY], raising=False
        )

        panel._refresh_capture_sources()

        assert redrawn == ["image"]

    def test_a_deleted_camera_redraws_them_too(self, monkeypatch):
        """Which is what puts a field pointed at it back on the active
        viewport, and says so."""
        panel = _panel(
            monkeypatch, cameras=[LOBBY, ATRIUM], form=FakeForm({"image": None})
        )
        redrawn: list = []
        panel._form.redraw = redrawn.append
        _primed(panel)
        monkeypatch.setattr(
            cameras_module, "stage_cameras", lambda: [ATRIUM], raising=False
        )

        panel._refresh_capture_sources()

        assert redrawn == ["image"]

    def test_an_edit_that_changes_no_camera_redraws_nothing(self, monkeypatch):
        """Moving a camera, renaming a mesh, adding a light: each resyncs
        something and none of them changes what the dropdown offers. A row that
        rebuilt on every one would flicker under the user's hands."""
        panel = _panel(monkeypatch, cameras=[LOBBY], form=FakeForm({"image": None}))
        redrawn: list = []
        panel._form.redraw = redrawn.append
        _primed(panel)

        panel._refresh_capture_sources()

        assert redrawn == []

    def test_every_image_field_is_redrawn(self, monkeypatch):
        """A tool with a start frame and an end frame has two dropdowns, and
        both of them are stale."""
        panel = _panel(
            monkeypatch,
            cameras=[LOBBY],
            form=FakeForm({"start": None, "end": "/World/Cameras/Lobby"}),
        )
        redrawn: list = []
        panel._form.redraw = redrawn.append
        _primed(panel)
        monkeypatch.setattr(
            cameras_module, "stage_cameras", lambda: [LOBBY, ATRIUM], raising=False
        )

        panel._refresh_capture_sources()

        assert sorted(redrawn) == ["end", "start"]

    def test_a_refresh_inside_the_cache_window_re_books_instead_of_walking(
        self, monkeypatch
    ):
        """`Redraws` collapses notices to one per FRAME, not one per burst. A
        stage that resyncs for several seconds would otherwise walk every prim
        on every frame of it, which is the cost the cache exists to avoid."""
        calls: list = []

        def cameras():
            calls.append(None)
            return [LOBBY]

        monkeypatch.setattr(cameras_module, "stage_cameras", cameras, raising=False)
        panel = _panel(monkeypatch, cameras=[LOBBY])
        monkeypatch.setattr(cameras_module, "stage_cameras", cameras, raising=False)
        booked: list = []
        panel._redraws = type(
            "R", (), {"request": lambda _s, key, _render: booked.append(key)}
        )()
        panel._camera_sources_read_at = time.monotonic()

        panel._refresh_capture_sources()

        assert calls == [], "it walked the stage inside the window"
        assert booked == ["cameras"], "it dropped the notice instead of re-booking"

    def test_the_notice_only_books_the_work(self, monkeypatch):
        """One edit arrives as a burst of notices and an import as thousands.
        Walking the stage per notice is the cost the cache exists to avoid."""
        panel = _panel(monkeypatch, cameras=[LOBBY])
        booked: list = []
        panel._redraws = type(
            "R", (), {"request": lambda _s, key, _render: booked.append(key)}
        )()

        panel._on_stage_cameras_changed()
        panel._on_stage_cameras_changed()

        assert booked == ["cameras", "cameras"]


class TestWatchingTheViewport:
    def test_a_field_on_the_viewport_keeps_the_live_preview_running(self, monkeypatch):
        panel = _panel(monkeypatch, form=FakeForm({"image": None}))
        panel._current_tab = 0
        started: list = []
        panel._viewport_preview = _FakePreview(started)

        panel._sync_viewport_watch()

        assert started == ["start"]

    def test_a_frame_on_a_named_camera_stops_it(self, monkeypatch):
        """That frame is a still. A live readback would be feeding nothing, on
        every frame, while the user types a prompt."""
        panel = _panel(
            monkeypatch, form=FakeForm({"image": "/World/Cameras/Lobby"})
        )
        panel._current_tab = 0
        started: list = []
        panel._viewport_preview = _FakePreview(started)

        panel._sync_viewport_watch()

        assert started == ["stop"]

    def test_it_follows_the_field_the_frame_captures_into(self, monkeypatch):
        """A start frame on a camera and an end frame on the viewport: the
        frame is live while it is pointed at the end frame, and not otherwise."""
        sources = {"start": "/World/Cameras/Lobby", "end": None}
        for target, expected in (("end", ["start"]), ("start", ["stop"])):
            panel = _panel(monkeypatch, form=FakeForm(sources, target=target))
            panel._current_tab = 0
            started: list = []
            panel._viewport_preview = _FakePreview(started)

            panel._sync_viewport_watch()

            assert started == expected

    def test_a_frame_folded_to_its_strip_stops_it(self, monkeypatch):
        """The strip shows the capture already taken, not the live view."""
        panel = _panel(monkeypatch, form=FakeForm({"image": None}, open_frame=False))
        panel._current_tab = 0
        started: list = []
        panel._viewport_preview = _FakePreview(started)

        panel._sync_viewport_watch()

        assert started == ["stop"]

    def test_a_tool_with_no_image_field_pays_nothing(self, monkeypatch):
        panel = _panel(monkeypatch, form=FakeForm({}))
        panel._current_tab = 0
        started: list = []
        panel._viewport_preview = _FakePreview(started)

        panel._sync_viewport_watch()

        assert started == ["stop"]


class TestWatchingTheStageForCameras:
    """Scoped more widely than the live preview. The preview is only wanted
    where a viewfinder is showing the viewport; the camera list is wanted
    wherever a dropdown exists, whichever source it is set to."""

    def test_a_field_on_a_named_camera_still_watches(self, monkeypatch):
        """It is the field that most needs to hear that its camera has gone."""
        panel = _panel(
            monkeypatch, form=FakeForm({"image": "/World/Cameras/Lobby"})
        )
        panel._current_tab = 0
        panel._viewport_preview = _FakePreview([])

        panel._sync_viewport_watch()

        assert panel._camera_watch.log == ["start"]

    def test_a_field_on_the_viewport_watches_too(self, monkeypatch):
        panel = _panel(monkeypatch, form=FakeForm({"image": None}))
        panel._current_tab = 0
        panel._viewport_preview = _FakePreview([])

        panel._sync_viewport_watch()

        assert panel._camera_watch.log == ["start"]

    def test_a_tool_with_no_image_field_pays_nothing(self, monkeypatch):
        """No dropdown on screen, so nothing to keep current."""
        panel = _panel(monkeypatch, form=FakeForm({}))
        panel._current_tab = 0
        panel._viewport_preview = _FakePreview([])

        panel._sync_viewport_watch()

        assert panel._camera_watch.log == ["stop"]

    def test_another_tab_pays_nothing(self, monkeypatch):
        panel = _panel(monkeypatch, form=FakeForm({"image": None}))
        panel._current_tab = 1
        panel._viewport_preview = _FakePreview([])

        panel._sync_viewport_watch()

        assert panel._camera_watch.log == ["stop"]


class _FakePreview:
    def __init__(self, log: list) -> None:
        self._log = log

    def start(self) -> None:
        self._log.append("start")

    def stop(self) -> None:
        self._log.append("stop")


class _FakeWatch:
    def __init__(self) -> None:
        self.log: list = []

    def start(self) -> None:
        self.log.append("start")

    def stop(self) -> None:
        self.log.append("stop")
