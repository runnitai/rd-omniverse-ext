"""A file is written, drawn and offered by what it actually is.

The plugin began when a run returned one picture, and "it is a PNG" was baked
into every path that touched a file. Then video and 3D tools became selectable
and the results grew a `type` and a `mime_type`, but only the RUN path learned
to read them. Everything reached through the library kept the old assumption: a
generated video was downloaded as `asset-<uuid>.png`, opened in a viewer that
draws images as an empty frame, suggested to the save dialog as `clip.mp4.png`,
and offered to an image field that would reject it.

The reference kind and the media kind are two different questions, and
conflating them is the whole bug. `LIBRARY_REF` says which endpoint the item
came from and how to name it in a request. It says nothing about the file.
"""

from __future__ import annotations

from rundiffusion_omniverse.api import media
from rundiffusion_omniverse.api.assets import Asset
from rundiffusion_omniverse.saving import SAVE_FILTERS, default_suffix, filters_for


def _asset(**overrides) -> Asset:
    fields = dict(
        id="run:result", kind="LIBRARY_REF", url=None, thumb_url=None
    )
    fields.update(overrides)
    return Asset(**fields)


class TestWhatItIs:
    def test_the_server_type_decides(self):
        assert _asset(type="VID").media_kind == media.KIND_VIDEO
        assert _asset(type="ASSET_3D").media_kind == media.KIND_ASSET_3D
        assert _asset(type="IMG").media_kind == media.KIND_IMAGE

    def test_the_mime_answers_when_the_type_is_missing(self):
        """An item that predates the field, or comes back without it."""
        assert _asset(mime_type="video/mp4").media_kind == media.KIND_VIDEO
        assert _asset(mime_type="image/webp").media_kind == media.KIND_IMAGE

    def test_the_type_wins_over_the_mime(self):
        """`application/octet-stream` is in the 3D mime set because that is what
        some model downloads declare. A row the server calls IMG is an image
        whatever its transfer encoding says."""
        assert (
            _asset(type="IMG", mime_type="application/octet-stream").media_kind
            == media.KIND_IMAGE
        )

    def test_the_reference_kind_says_nothing_about_the_file(self):
        """The bug in one line: both of these are LIBRARY_REF."""
        assert _asset(type="IMG").is_image is True
        assert _asset(type="VID").is_image is False

    def test_something_unrecognised_is_not_quietly_an_image(self):
        """LAYERS is on this surface and is none of the three. Guessing image
        is what put undrawable things in front of the viewer."""
        assert _asset(type="LAYERS").is_image is False


class TestWhatItIsCalled:
    def test_the_mime_names_the_file(self):
        assert _asset(type="VID", mime_type="video/mp4").extension == ".mp4"
        assert _asset(type="IMG", mime_type="image/jpeg").extension == ".jpg"

    def test_an_unknown_mime_falls_back_to_the_url(self):
        """The URL is signed and its path is not a promise, which is why it is
        the fallback rather than the answer."""
        asset = _asset(url="https://storage/x/clip.webm?sig=abc")

        assert asset.extension == ".webm"

    def test_nothing_to_go_on_is_not_called_a_picture(self):
        """`.bin` is unhelpful. `.png` on a file that is not one is a file the
        operating system will not open and the user cannot fix."""
        assert _asset().extension == ".bin"

    def test_a_query_string_artefact_is_not_taken_as_a_suffix(self):
        assert _asset(url="https://storage/download?name=x").extension == ".bin"


class TestTheSaveDialog:
    def test_it_opens_on_the_format_the_file_already_is(self):
        """`filters_for` was written for this and then never called, so the
        dialog always opened on PNG."""
        assert filters_for("rundiffusion-ab12cd34.mp4")[0] == (".mp4", "MP4 video")

    def test_every_filter_is_still_offered_after_the_one_that_matched(self):
        assert len(filters_for("x.glb")) == len(SAVE_FILTERS)

    def test_an_unrecognised_name_leaves_the_order_alone(self):
        assert filters_for("x.tiff") == SAVE_FILTERS

    def test_a_name_returned_without_a_suffix_keeps_its_own_format(self):
        """The dialog hands back the name and the folder separately, and the
        name can come home without the extension. Putting `.png` back on a
        video is how an MP4 came to be written as a PNG."""
        assert default_suffix("rundiffusion-ab12cd34.mp4") == ".mp4"
        assert default_suffix("model.glb") == ".glb"

    def test_a_suggestion_with_no_format_of_its_own_still_gets_one(self):
        """Nothing better to say, and the caller has already failed to name the
        format."""
        assert default_suffix("rundiffusion") == ".png"


class TestWhatIsWorthDownloading:
    """`thumb_url` falls back to the full file server-side when no thumbnail
    has been generated. That is exactly right for an image and useless for
    anything else: it means a grid holding a two-minute video downloads the
    whole thing to fail to decode it, and does so again on every redraw,
    because a failure is not cached. The cell reads "Video" either way.
    """

    def test_an_image_is_previewed_from_whatever_it_has(self):
        asset = _asset(type="IMG", url="https://storage/a.png")

        assert asset.drawable_preview_url == "https://storage/a.png"

    def test_a_video_falling_back_to_itself_is_not_downloaded(self):
        """The fallback: same URL in both fields."""
        asset = _asset(
            type="VID",
            url="https://storage/clip.mp4",
            thumb_url="https://storage/clip.mp4",
        )

        assert asset.drawable_preview_url is None

    def test_a_video_with_no_thumbnail_at_all_is_not_downloaded(self):
        asset = _asset(type="VID", url="https://storage/clip.mp4")

        assert asset.drawable_preview_url is None

    def test_a_video_with_a_REAL_still_is_drawn(self):
        """A generated still is a different object at a different URL, which is
        what distinguishes it from the fallback. These are common, and
        refusing to draw them would be the opposite mistake."""
        asset = _asset(
            type="VID",
            url="https://storage/clip.mp4",
            thumb_url="https://storage/thumbs/clip_512x512.webp",
        )

        assert asset.drawable_preview_url == "https://storage/thumbs/clip_512x512.webp"

    def test_the_grid_preview_url_is_unchanged_for_images(self):
        """`preview_url` is still what an image grid uses, and still prefers
        the thumbnail: this is a second question, not a replacement."""
        asset = _asset(
            type="IMG", url="https://storage/a.png", thumb_url="https://storage/a.webp"
        )

        assert asset.preview_url == "https://storage/a.webp"
