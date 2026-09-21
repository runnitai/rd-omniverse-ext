"""The image assets that travel inside the packaged extension.

Nothing else in the suite touches them: the panel draws them, and drawing needs
a running Kit. What can be checked without one is that they are still there and
still what they claim to be, which is the failure that actually happens. A file
renamed, a folder moved, an icon hand-edited into something the rasteriser
behind omni.ui will not draw: each of those reaches a user as a button with a
hole in it, and none of them is visible in a diff of the Python.

The packaged zip is `exts/rundiffusion.omniverse` whole (see the packaging
step in CI), so everything under data/ ships and a path that resolves here
resolves in the zip.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from rundiffusion_omniverse.panel import (
    DATA_DIR,
    ICON_SIZE,
    MARK_PATH,
    OPEN_IN_NEW_ICON,
)

#: The `d` of Material Design Icons' `open-in-new`, as published.
#:
#: Pinned so an edit to the shipped copy has to be a deliberate one. Taken from
#: Templarian/MaterialDesign, svg/open-in-new.svg, and compared byte for byte
#: against it on 2026-08-31. Provenance and licence: data/ICONS-LICENSE.md.
MDI_OPEN_IN_NEW = (
    "M14,3V5H17.59L7.76,14.83L9.17,16.24L19,6.41V10H21V3M19,19H5V5H12V3H5C3.89,"
    "3 3,3.9 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19V12H19V19Z"
)

#: PNG's magic number. A file that does not start with this is not one,
#: whatever it is called.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TestTheyAreWhereTheCodeLooks:
    def test_the_data_folder_is_inside_the_extension(self):
        """Not beside it. Anything outside `exts/rundiffusion.omniverse` is
        left behind by the packaging and the asset is missing on a user's
        machine while it is present on the developer's."""
        assert DATA_DIR.is_dir()
        assert DATA_DIR.parent.name == "rundiffusion.omniverse"

    def test_the_mark_is_there(self):
        assert MARK_PATH.is_file()

    def test_the_icon_is_there(self):
        assert OPEN_IN_NEW_ICON.is_file()


class TestTheMark:
    def test_it_is_really_a_png(self):
        """omni.ui goes by content, not by the extension on the name."""
        assert MARK_PATH.read_bytes().startswith(PNG_MAGIC)


class TestTheIcon:
    @pytest.fixture
    def svg(self):
        return OPEN_IN_NEW_ICON.read_text(encoding="utf-8")

    def test_it_is_well_formed(self, svg):
        """A rasteriser handed malformed XML draws nothing, and draws it
        silently."""
        assert ET.fromstring(svg).tag.endswith("svg")

    def test_it_keeps_the_square_view_box_it_is_sized_against(self, svg):
        """`image_width` and `image_height` are set to the same number, so a
        view box that stopped being square would letterbox the icon inside the
        button rather than fill it."""
        assert ET.fromstring(svg).get("viewBox") == "0 0 24 24"

    def test_the_path_is_still_the_one_mdi_publishes(self, svg):
        """Pinned rather than eyeballed. Path data is unreadable by hand, so a
        stray edit is invisible in review and shows up as a shape nobody
        recognises."""
        drawn = re.search(r'<path[^>]*\bd="([^"]+)"', svg).group(1)

        assert drawn == MDI_OPEN_IN_NEW

    def test_it_is_drawn_in_a_flat_fill(self, svg):
        """Flat fills are what every icon SVG shipped with the Kit SDK uses,
        and they are the part of the format there is no doubt about. An icon
        that grew a gradient would be betting the button on a corner of the
        rasteriser nothing here has tested, and it would lose silently."""
        assert "gradient" not in svg.lower()

    def test_it_is_white_so_the_panel_can_tint_it(self, svg):
        """The colour is chosen in panel.py through the `Button.Image` style,
        beside the rest of the theme. That only works from white."""
        fills = re.findall(r'<path[^>]*\bfill="([^"]+)"', svg)

        assert fills == ["#ffffff"]


class TestTheAttributionThatComesWithIt:
    """Material Design Icons is Apache 2.0, which asks for the licence to
    travel with the work. This extension is submitted to a marketplace, so the
    notice ships in data/ rather than living in a wiki nobody packages."""

    @pytest.fixture
    def notice(self):
        return (DATA_DIR / "ICONS-LICENSE.md").read_text(encoding="utf-8")

    def test_the_notice_ships(self):
        assert (DATA_DIR / "ICONS-LICENSE.md").is_file()

    def test_it_names_the_licence(self, notice):
        assert "Apache License" in notice

    def test_it_names_what_the_licence_covers(self, notice):
        assert OPEN_IN_NEW_ICON.name in notice


class TestHowBigItIsDrawn:
    def test_the_icon_is_smaller_than_the_button_it_sits_in(self):
        """A mark beside the words rather than a second control. The button is
        22px, and an icon drawn at that height fills it edge to edge."""
        assert ICON_SIZE < 22
