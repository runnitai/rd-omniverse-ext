"""Cropping a picture to the shape of the output size that was chosen.

Cropped about the centre, never padded: bars of nothing sent to a model would
be generated from as though they were part of the scene.
"""

from __future__ import annotations

from io import BytesIO

import pytest

from rundiffusion_omniverse.dialogs import wrap_pills
from rundiffusion_omniverse.viewport_preview import crop_box, crop_png


def _png(width: int, height: int) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    buffer = BytesIO()
    Image.new("RGB", (width, height)).save(buffer, format="PNG")
    return buffer.getvalue()


class TestTheBox:
    def test_a_wide_picture_loses_its_sides(self):
        assert crop_box(1600, 900, 1.0) == (350, 0, 1250, 900)

    def test_a_tall_picture_loses_its_top_and_bottom(self):
        assert crop_box(900, 1600, 1.0) == (0, 350, 900, 1250)

    def test_a_picture_already_that_shape_is_untouched(self):
        assert crop_box(1600, 900, 16 / 9) == (0, 0, 1600, 900)


class TestThePicture:
    """Pillow ships inside Kit rather than with the test environment, so these
    run wherever it is installed and skip where it is not."""

    def test_it_comes_back_the_chosen_shape(self):
        Image = pytest.importorskip("PIL.Image")
        with Image.open(BytesIO(crop_png(_png(160, 90), 9 / 16))) as image:
            assert image.size == (51, 90)

    def test_no_shape_leaves_it_alone(self):
        png = _png(160, 90)

        assert crop_png(png, None) is png

    def test_something_that_is_not_a_picture_is_not_lost(self):
        pytest.importorskip("PIL.Image")
        assert crop_png(b"not a png", 1.0) == b"not a png"


class TestWrappingPills:
    def test_they_share_a_row_while_they_fit(self):
        assert wrap_pills(["All", "Fast"], 400) == [[0, 1]]

    def test_they_start_a_new_row_rather_than_scrolling_sideways(self):
        """Reported from Base Editor: a kit's tabs ran past the window's edge."""
        rows = wrap_pills(["All", "Featured", "Most powerful", "Fast"], 160)

        assert len(rows) > 1
        assert sorted(i for row in rows for i in row) == [0, 1, 2, 3]

    def test_one_too_wide_for_any_row_still_gets_one(self):
        assert wrap_pills(["x" * 200], 100) == [[0]]
