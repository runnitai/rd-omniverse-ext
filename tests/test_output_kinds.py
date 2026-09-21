"""What a finished run actually produced.

Groundwork for video and 3D outputs. The panel treated
every output as an image: `download_outputs` fetched each URL and the panel
wrote the bytes to a `.png` whatever they were. Nothing filters video or 3D
tools out of the browser, so that was reachable, not theoretical.

Each output carries its own `type` and `mime_type` and both were ignored.
"""

from __future__ import annotations

import pytest

from rundiffusion_omniverse.api.generate import (
    KIND_ASSET_3D,
    KIND_IMAGE,
    KIND_OTHER,
    KIND_VIDEO,
    Output,
    parse_outputs,
)


class TestKindFromServerType:
    """`type` is the server's word for it, so it wins."""

    @pytest.mark.parametrize(
        "type_,expected",
        [
            ("IMG", KIND_IMAGE),
            ("VID", KIND_VIDEO),
            ("ASSET_3D", KIND_ASSET_3D),
            ("img", KIND_IMAGE),
        ],
    )
    def test_a_known_type_maps(self, type_, expected):
        assert Output(type=type_).kind == expected

    @pytest.mark.parametrize("type_", ["LAYERS", "TEXT", "MODEL"])
    def test_a_type_with_nowhere_to_go_is_other(self, type_):
        # Real members of RunnitNodeRunResultType this panel cannot draw.
        assert Output(type=type_).kind == KIND_OTHER

    def test_the_type_beats_a_disagreeing_mime(self):
        assert Output(type="VID", mime_type="image/png").kind == KIND_VIDEO


class TestKindFromMime:
    """The fallback, for an output that arrives without a type."""

    @pytest.mark.parametrize(
        "mime,expected",
        [
            ("image/png", KIND_IMAGE),
            ("image/webp", KIND_IMAGE),
            ("video/mp4", KIND_VIDEO),
            ("model/gltf-binary", KIND_ASSET_3D),
            ("text/plain", KIND_OTHER),
        ],
    )
    def test_the_mime_decides(self, mime, expected):
        assert Output(mime_type=mime).kind == expected

    def test_nothing_at_all_is_other(self):
        # Never guessed as an image: guessing is what wrote a video to a .png.
        assert Output().kind == KIND_OTHER


class TestFetchable:
    def test_an_output_with_a_url_is_fetchable(self):
        assert Output(url="https://example.test/a.png").is_fetchable is True

    def test_an_expired_output_is_not(self):
        assert Output(url="https://example.test/a.png", expired=True).is_fetchable is False

    def test_an_output_with_no_url_is_not(self):
        assert Output(url=None).is_fetchable is False


class TestParsing:
    def test_the_whole_shape_is_read(self):
        [out] = parse_outputs([
            {
                "url": "https://example.test/a.mp4",
                "type": "VID",
                "mime_type": "video/mp4",
                "width": 1920,
                "height": 1080,
                "duration_seconds": 5,
                "size_bytes": 1234,
                "expired": False,
            }
        ])
        assert out.kind == KIND_VIDEO
        assert (out.width, out.height, out.duration_seconds) == (1920, 1080, 5)

    @pytest.mark.parametrize("raw", [None, {}, "outputs", 7])
    def test_anything_that_is_not_a_list_is_no_outputs(self, raw):
        assert parse_outputs(raw) == []

    def test_a_junk_entry_is_dropped_not_guessed(self):
        assert parse_outputs([{"url": "https://example.test/a.png"}, "junk"]) == [
            Output(url="https://example.test/a.png")
        ]
