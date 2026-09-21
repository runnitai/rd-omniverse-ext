"""Tests for when the viewport preview decides to re-capture.

The preview exists so an image field saying "sending the active viewport" can
show which frame it means. That promise is only kept if the thumbnail follows
the camera, and it is only affordable if it does NOT follow it every frame: a
capture is a full readback, and one drag emits a camera change per frame.

So the whole design is a debounce, and a debounce is exactly the thing that
looks fine in a screenshot while being wrong. These drive the update handler
with a fake clock and a fake camera, with no Kit, no GPU and no window.

`write_thumbnail` is not covered here: it needs Pillow, which Kit prebundles and
a plain test process does not have. It is verified by looking at the panel.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rundiffusion_omniverse import viewport_preview
from rundiffusion_omniverse.viewport_preview import SETTLE_SECONDS, ViewportPreview


class FakeCamera:
    """A camera signature the test moves by hand, and a clock it controls."""

    def __init__(self) -> None:
        self.signature = ("a",)
        self.now = 100.0
        self.captures = 0

    def install(self, preview: ViewportPreview, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(viewport_preview.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(preview, "_camera_signature", lambda: self.signature)
        monkeypatch.setattr(preview, "refresh_now", self._capture)

    def _capture(self) -> None:
        self.captures += 1

    def tick(self, preview: ViewportPreview, seconds: float = 0.0) -> None:
        self.now += seconds
        preview._on_update(None)


@pytest.fixture
def preview(tmp_path: Path) -> ViewportPreview:
    return ViewportPreview(tmp_path)


@pytest.fixture
def camera(preview: ViewportPreview, monkeypatch: pytest.MonkeyPatch) -> FakeCamera:
    fake = FakeCamera()
    fake.install(preview, monkeypatch)
    return fake


def test_a_moving_camera_captures_nothing(preview, camera):
    """One drag is a signature change per frame. Capturing on each would fire
    sixty readbacks for one orbit, which is the failure this design avoids."""
    for step in range(60):
        camera.signature = (step,)
        camera.tick(preview, 1 / 60)

    assert camera.captures == 0


def test_a_settled_camera_captures_once(preview, camera):
    camera.signature = ("moved",)
    camera.tick(preview)
    camera.tick(preview, SETTLE_SECONDS + 0.1)

    assert camera.captures == 1


def test_a_camera_that_stays_still_does_not_keep_capturing(preview, camera):
    """The pending refresh is consumed when it runs. Without that, every frame
    after the first settle would start another capture forever."""
    camera.signature = ("moved",)
    camera.tick(preview)
    camera.tick(preview, SETTLE_SECONDS + 0.1)
    for _ in range(30):
        camera.tick(preview, 1 / 60)

    assert camera.captures == 1


def test_a_second_move_captures_again(preview, camera):
    camera.signature = ("first",)
    camera.tick(preview)
    camera.tick(preview, SETTLE_SECONDS + 0.1)

    camera.signature = ("second",)
    camera.tick(preview)
    camera.tick(preview, SETTLE_SECONDS + 0.1)

    assert camera.captures == 2


def test_nothing_captures_before_the_settle_elapses(preview, camera):
    camera.signature = ("moved",)
    camera.tick(preview)
    camera.tick(preview, SETTLE_SECONDS / 2)

    assert camera.captures == 0


def test_losing_every_viewport_is_a_signature_change_like_any_other(preview, camera):
    """A closed viewport reads as None, which settles and captures like any
    other change. That capture is what replaces the thumbnail with the message
    saying there is no viewport, rather than leaving a view that is gone."""
    camera.signature = ("looking at something",)
    camera.tick(preview)
    camera.tick(preview, SETTLE_SECONDS + 0.1)

    camera.signature = None
    camera.tick(preview)
    camera.tick(preview, SETTLE_SECONDS + 0.1)

    assert camera.captures == 2


def test_a_new_thumbnail_replaces_the_old_file(preview, tmp_path):
    """omni.ui.Image caches by path, so a refresh must write a NEW name or the
    panel keeps showing the old texture. The previous file is then removed."""
    first = tmp_path / "viewport-preview-1.png"
    first.write_bytes(b"first")
    preview._path = first

    second = tmp_path / "viewport-preview-2.png"
    second.write_bytes(b"second")
    preview._set(second, None)

    assert preview.path == second
    assert not first.exists()


def test_a_lost_viewport_clears_the_thumbnail_and_says_why(preview, tmp_path):
    """Leaving the last view on screen would be showing something that is gone."""
    stale = tmp_path / "viewport-preview-1.png"
    stale.write_bytes(b"stale")
    preview._path = stale

    preview._set(None, "There is no active viewport to capture.")

    assert preview.path is None
    assert preview.message == "There is no active viewport to capture."
    assert not stale.exists()


def test_listeners_hear_about_a_change_once(tmp_path):
    changes = []
    preview = ViewportPreview(tmp_path, on_changed=lambda: changes.append(1))

    path = tmp_path / "viewport-preview-1.png"
    path.write_bytes(b"x")
    preview._set(path, None)
    preview._set(path, None)

    assert len(changes) == 1
