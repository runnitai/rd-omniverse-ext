"""What the panel offers to do with a result, given what the result is.

Video and 3D outputs changed this. Every result used to be an image, so every
result got the same four buttons. Two of them cannot work on anything else:

- Send to RunDiffusion posts to /uploads, which accepts jpeg, png, webp, heic
  and heif only, so sending a video or a model fails at the server.
- Compare puts two pictures side by side.

And one only makes sense for a model: adding it to the open stage, which is the
reason to generate one inside Omniverse rather than anywhere else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rundiffusion_omniverse.api.generate import (
    KIND_ASSET_3D,
    KIND_IMAGE,
    KIND_VIDEO,
)
from rundiffusion_omniverse.panel import RunDiffusionPanel, SessionResult


def _result(kind: str, suffix: str = ".png") -> SessionResult:
    return SessionResult(
        "req-1", Path(f"result{suffix}"), "Tool", "tool-1", {}, {}, "", kind=kind
    )


def _labels(kind: str, suffix: str = ".png") -> list[str]:
    panel = object.__new__(RunDiffusionPanel)
    return [label for label, _fn in panel._result_actions(_result(kind, suffix))]


class TestImages:
    def test_an_image_keeps_everything_it_had(self):
        assert _labels(KIND_IMAGE) == [
            "Save to computer",
            "Open in system viewer",
            "Compare",
            "Send to RunDiffusion",
        ]


class TestVideo:
    def test_a_video_is_not_offered_compare(self):
        assert "Compare" not in _labels(KIND_VIDEO, ".mp4")

    def test_a_video_can_be_saved(self):
        assert "Save to computer" in _labels(KIND_VIDEO, ".mp4")

    def test_a_video_can_be_played(self):
        # omni.ui has no video widget, so this is what plays it.
        assert "Open in system viewer" in _labels(KIND_VIDEO, ".mp4")


class TestAsset3D:
    def test_a_model_offers_exactly_what_is_useful_for_one(self):
        """Open shows nothing for a model and Compare wipes one picture over
        another, so neither belongs here. Reported from a real 3D result."""
        assert _labels(KIND_ASSET_3D, ".glb") == [
            "Add to stage",
            "Save to computer",
        ]

    def test_a_model_is_not_offered_open(self):
        assert "Open in system viewer" not in _labels(KIND_ASSET_3D, ".glb")

    def test_a_model_is_not_offered_compare(self):
        assert "Compare" not in _labels(KIND_ASSET_3D, ".glb")

    def test_a_model_leads_with_the_stage(self):
        # First, because it is the reason to generate one here at all.
        assert _labels(KIND_ASSET_3D, ".glb")[0] == "Add to stage"

    def test_a_model_can_be_saved(self):
        assert "Save to computer" in _labels(KIND_ASSET_3D, ".glb")

    @pytest.mark.parametrize("kind", [KIND_IMAGE, KIND_VIDEO])
    def test_nothing_else_is_offered_the_stage(self, kind):
        assert "Add to stage" not in _labels(kind)


class TestEveryKind:
    @pytest.mark.parametrize(
        "kind,suffix", [(KIND_IMAGE, ".png"), (KIND_VIDEO, ".mp4"), (KIND_ASSET_3D, ".glb")]
    )
    def test_a_result_can_always_be_kept(self, kind, suffix):
        # Whatever the panel can or cannot draw, the run was paid for.
        assert "Save to computer" in _labels(kind, suffix)


class TestTheSummaryLine:
    """What a result that cannot be drawn says about itself.

    Real values from the first successful runs: a 1024x800 5.04s 3.1MB mp4 and
    a 2.2MB glb. Neither run returned a poster image (each returns exactly one
    output), and Kit ships no video decoder, so this line is the preview.
    """

    def _result(self, **kwargs) -> SessionResult:
        defaults = dict(
            kind=KIND_VIDEO, duration_seconds=None, width=None, height=None,
            size_bytes=None,
        )
        defaults.update(kwargs)
        suffix = defaults.pop("suffix", ".mp4")
        return SessionResult(
            "req-1", Path(f"result{suffix}"), "Tool", "tool-1", {}, {}, "", **defaults
        )

    def test_a_video_reports_what_it_is(self):
        result = self._result(width=1024, height=800, duration_seconds=5.042, size_bytes=3132053)
        assert result.summary == "1024 x 800  5.0s  3.1 MB  MP4"

    def test_a_model_reports_what_it_can(self):
        # A glb comes back with no dimensions and no duration.
        result = self._result(kind=KIND_ASSET_3D, suffix=".glb", size_bytes=2219560)
        assert result.summary == "2.2 MB  GLB"

    def test_a_result_with_nothing_known_still_names_its_format(self):
        assert self._result(suffix=".mp4").summary == "MP4"

    def test_a_file_with_no_suffix_does_not_produce_an_empty_line(self):
        assert self._result(suffix="").summary == "file"


class TestUploadableKinds:
    """Send is offered only where the upload can complete.

    POST /uploads takes images only, so the button is absent for a video or a
    model rather than put in front of someone whose only outcome is an apology.
    Widening the endpoint is planned; this follows UPLOADABLE_KINDS, so the
    button comes back on its own when that list grows.
    """

    def test_only_images_can_be_uploaded_today(self):
        from rundiffusion_omniverse.panel import UPLOADABLE_KINDS

        assert UPLOADABLE_KINDS == (KIND_IMAGE,)

    @pytest.mark.parametrize("kind", [KIND_VIDEO, KIND_ASSET_3D])
    def test_send_is_not_offered_for_what_cannot_be_sent(self, kind):
        assert "Send to RunDiffusion" not in _labels(kind, ".glb")

    def test_send_follows_the_list_rather_than_the_kind(self, monkeypatch):
        """When the endpoint widens, the button returns with no other edit."""
        import rundiffusion_omniverse.panel as panel_module

        monkeypatch.setattr(
            panel_module, "UPLOADABLE_KINDS", (KIND_IMAGE, KIND_ASSET_3D)
        )
        assert "Send to RunDiffusion" in _labels(KIND_ASSET_3D, ".glb")
