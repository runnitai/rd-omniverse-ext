"""A video result has a picture now, and a 3D one still does not.

The Generate API returns a signed first-frame JPEG beside a video output.
Kit ships no video decoder, so before this the panel could only
draw a tile naming the format, the duration and the size, which is what it will
still do wherever there is no still to draw.

Three ways there is no still, and all three have to land on that same fallback:
a 3D asset (the server deliberately makes none), a video whose frame could not
be extracted (`preview_url` is null rather than a URL that 404s), and a
download that failed after the fact. None of them may cost the render.
"""

from __future__ import annotations

import asyncio

import pytest

from rundiffusion_omniverse.api import generate as generate_api
from rundiffusion_omniverse.api.generate import (
    KIND_ASSET_3D,
    KIND_IMAGE,
    KIND_VIDEO,
    GenerateResult,
    Output,
    download_outputs,
    parse_outputs,
)

RECORDABLE = (KIND_IMAGE, KIND_VIDEO, KIND_ASSET_3D)


def _run(coroutine):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


class TestReadingIt:
    def test_a_video_output_carries_its_still(self):
        outputs = parse_outputs(
            [{"type": "VID", "url": "https://storage/clip.mp4", "preview_url": "https://storage/clip_preview.jpg"}]
        )

        assert outputs[0].preview_url == "https://storage/clip_preview.jpg"
        assert outputs[0].has_preview is True

    def test_no_preview_is_not_an_error(self):
        """The server sends null rather than a URL that 404s, precisely so a
        caller can branch on it."""
        outputs = parse_outputs([{"type": "VID", "url": "https://storage/clip.mp4", "preview_url": None}])

        assert outputs[0].preview_url is None
        assert outputs[0].has_preview is False

    def test_an_output_that_predates_previews_reads_cleanly(self):
        """The field is absent, not null. Same answer."""
        assert parse_outputs([{"type": "VID", "url": "https://storage/clip.mp4"}])[0].has_preview is False

    def test_an_expired_output_has_no_usable_preview(self):
        """The still is signed on the same TTL as the output and shares the
        bucket's lifecycle, so it expires with the thing it previews."""
        output = Output(
            url="https://storage/clip.mp4",
            preview_url="https://storage/clip_preview.jpg",
            type="VID",
            expired=True,
        )

        assert output.has_preview is False


class TestFetchingIt:
    @pytest.fixture
    def downloads(self, monkeypatch):
        """Answers by URL, and records what was asked for."""
        answers: dict[str, object] = {}
        asked: list[str] = []

        async def _download(url: str) -> bytes:
            asked.append(url)
            answer = answers.get(url, b"bytes")
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(generate_api, "download_async", _download)
        return answers, asked

    def _result(self, *outputs) -> GenerateResult:
        made = object.__new__(GenerateResult)
        made.outputs = list(outputs)
        return made

    def test_the_still_is_fetched_with_the_video(self, downloads):
        answers, asked = downloads
        answers["https://storage/clip.mp4"] = b"VIDEO"
        answers["https://storage/clip_preview.jpg"] = b"JPEG"
        result = self._result(
            Output(url="https://storage/clip.mp4", preview_url="https://storage/clip_preview.jpg", type="VID")
        )

        fetched = _run(download_outputs(result, kinds=RECORDABLE))

        assert fetched[0].data == b"VIDEO"
        assert fetched[0].preview == b"JPEG"

    def test_an_output_with_no_still_asks_for_nothing_extra(self, downloads):
        """A 3D asset. One request, not two, and no None-URL download."""
        _answers, asked = downloads
        result = self._result(Output(url="https://storage/model.glb", type="ASSET_3D"))

        fetched = _run(download_outputs(result, kinds=RECORDABLE))

        assert asked == ["https://storage/model.glb"]
        assert fetched[0].preview is None

    def test_a_still_that_fails_to_download_does_not_lose_the_render(self, downloads):
        """The render is already in hand by the time the still is fetched.
        Losing the run over a thumbnail would be the wrong trade."""
        answers, _asked = downloads
        answers["https://storage/clip.mp4"] = b"VIDEO"
        answers["https://storage/clip_preview.jpg"] = OSError("connection reset")
        result = self._result(
            Output(url="https://storage/clip.mp4", preview_url="https://storage/clip_preview.jpg", type="VID")
        )

        fetched = _run(download_outputs(result, kinds=RECORDABLE))

        assert fetched[0].data == b"VIDEO"
        assert fetched[0].preview is None

    def test_a_render_that_fails_is_still_skipped_whole(self, downloads):
        """And its preview is not fetched for a result nobody will have."""
        answers, asked = downloads
        answers["https://storage/clip.mp4"] = OSError("gone")
        result = self._result(
            Output(url="https://storage/clip.mp4", preview_url="https://storage/clip_preview.jpg", type="VID")
        )

        fetched = _run(download_outputs(result, kinds=RECORDABLE))

        assert fetched == []
        assert "https://storage/clip_preview.jpg" not in asked


class TestWhatGetsDrawn:
    """`drawable_path` is the one question every drawing site asks: the results
    strip, the This session grid, and the viewer. None means "use the tile",
    which is what all three did for every video and model before previews
    existed, so the fallback is the old behaviour rather than new code."""

    def _result(self, tmp_path, kind, preview=None):
        from rundiffusion_omniverse.panel import SessionResult

        return SessionResult(
            "req-1",
            tmp_path / "result.bin",
            "A tool",
            kind=kind,
            preview_path=preview,
        )

    def test_an_image_is_its_own_picture(self, tmp_path):
        result = self._result(tmp_path, KIND_IMAGE)

        assert result.drawable_path == result.path

    def test_an_image_ignores_a_still_even_if_one_arrived(self, tmp_path):
        """Nothing sends one, and drawing a JPEG copy instead of the render
        would quietly show a worse picture than the one on disk."""
        still = tmp_path / "still.jpg"
        result = self._result(tmp_path, KIND_IMAGE, preview=still)

        assert result.drawable_path == result.path

    def test_a_video_draws_its_still(self, tmp_path):
        still = tmp_path / "still.jpg"
        result = self._result(tmp_path, KIND_VIDEO, preview=still)

        assert result.drawable_path == still

    def test_a_video_without_one_draws_nothing(self, tmp_path):
        assert self._result(tmp_path, KIND_VIDEO).drawable_path is None

    def test_a_3d_asset_draws_nothing(self, tmp_path):
        """The server makes no still for one, deliberately: Kit can import and
        render a .glb itself, and a software-rasterised thumbnail would be the
        worse of the two pictures."""
        assert self._result(tmp_path, KIND_ASSET_3D).drawable_path is None


class TestWritingItDown:
    def _panel(self, tmp_path):
        from rundiffusion_omniverse.panel import RunDiffusionPanel

        panel = object.__new__(RunDiffusionPanel)
        panel._result_dir = tmp_path
        return panel

    def test_a_still_lands_beside_its_result(self, tmp_path):
        path = self._panel(tmp_path)._write_preview("abc123", b"JPEG")

        assert path is not None
        assert path.read_bytes() == b"JPEG"
        assert path.name.startswith("result-abc123")

    def test_it_is_named_jpg(self, tmp_path):
        """The server sends JPEG rather than the WebP the Runnit thumbnails
        use, because WebP is exactly what Kit's texture loader cannot read."""
        assert self._panel(tmp_path)._write_preview("abc123", b"JPEG").suffix == ".jpg"

    def test_nothing_to_write_writes_nothing(self, tmp_path):
        assert self._panel(tmp_path)._write_preview("abc123", None) is None

    def test_a_still_that_cannot_be_written_is_not_fatal(self, tmp_path):
        panel = self._panel(tmp_path / "nowhere")

        assert panel._write_preview("abc123", b"JPEG") is None
