"""The palette, shared by the panel and the windows it opens.

Here rather than in `panel.py` for the same reason as `text.py`: `panel`
imports `dialogs`, so a colour both use has to sit under both of them.

omni.ui colours are 0xAABBGGRR, which is the reverse of a CSS hex and the
easiest thing in this plugin to get backwards.
"""

from __future__ import annotations

#: Amethyst #9968DF, the brand accent the web app and the other plugins use for
#: a primary action.
AMETHYST = 0xFFDF6899
AMETHYST_BRIGHT = 0xFFEE8AB4
HAIRLINE = 0xFF5A5A5A
TRANSPARENT = 0x00000000

#: The ground under a run that has not landed, and under a kit tile. Dark
#: enough to read as an empty slot rather than as a picture that failed to
#: load.
CELL = 0xFF2E2E2E
#: The same ground with the pointer over it. A step lighter rather than a
#: different colour: the tile is the same object, lit.
CELL_HOVERED = 0xFF3C3C3C

#: A tile that has no content yet. Below the resting cell rather than above it,
#: so a grid of them reads as space being held rather than as content.
CELL_EMPTY = 0xFF262626

#: What went wrong, said where it happened.
#:
#: The panel's status line sits at the very bottom under BILLED TO, which is a
#: long way from the button someone just pressed. A capture that refuses needs
#: to be read where the user is already looking, and colour is what separates
#: that from the labels around it.
#:
#: Light rather than a signal red: this sits on a dark panel, and #FF6B6B reads
#: as a warning without vibrating against the background the way pure red does.
DANGER = 0xFF6B6BFF

#: The ground under a text field or a logo well.
FIELD = 0xFF2B2B2B

#: A button that is available without asking to be pressed: every action on a
#: result except the one that carries weight.
QUIET_FILL = 0xFF464646
QUIET_BORDER = 0xFF565656

#: The Capture button. A step lighter than a quiet button, because it is the
#: primary action inside the tab body, and a step short of the brand colour,
#: because Generate is the primary action of the panel.
CAPTURE_FILL = 0xFF4F4F4F

#: Save on a result: the one verb most people came for, tinted towards the
#: brand rather than wearing it.
WEIGHTED_FILL = 0xFF744B5A
WEIGHTED_BORDER = 0xFFA0667A

#: A rule between blocks. Darker than HAIRLINE, which outlines controls.
SEPARATOR = 0xFF2F2F2F

#: The tab strip's rule and inactive labels.
TAB_RULE = 0xFF2B2B2B
TAB_INACTIVE = 0xFFA2A2A2

#: Text, from loudest to quietest. omni.ui cannot bold an arbitrary label, so
#: hierarchy comes from these and from size.
TEXT_EMPHASIS = 0xFFFFFFFF
TEXT_BODY = 0xFFD4D4D4
TEXT_SECONDARY = 0xFFA6A6A6
TEXT_META = 0xFF9A9A9A

#: The plate under a chip drawn over a picture, so the words stay readable
#: whatever the picture is.
CHIP_PLATE = 0xD1141414

#: The protection band along the bottom of the viewfinder: transparent at the
#: top, most of the way to black at the bottom.
BAND_TOP = 0x000C0304
BAND_BOTTOM = 0xB30C0304

#: The indeterminate bar under a run. Muted, because it claims only that
#: something is happening.
RUN_TRACK = 0xFF2B2B2B
RUN_BAR = 0xFFA0667A

#: Type sizes, in pixels.
SIZE_META = 10
SIZE_CAPS = 11
SIZE_BODY = 12
SIZE_EMPHASIS = 13
SIZE_TITLE = 15

#: Ready-made label styles for the three roles that recur on every surface.
STYLE_CAPS = {"font_size": SIZE_CAPS, "color": TEXT_META}
STYLE_META = {"font_size": SIZE_META, "color": TEXT_META}
STYLE_SECONDARY = {"font_size": SIZE_BODY, "color": TEXT_SECONDARY}
STYLE_HELP = {"font_size": SIZE_META, "color": TEXT_SECONDARY}

#: Buttons. Written out in full rather than layered, because omni.ui replaces
#: a widget's style rather than merging into it.
STYLE_QUIET_BUTTON = {
    "Button": {
        "background_color": QUIET_FILL,
        "border_color": QUIET_BORDER,
        "border_width": 1,
        "border_radius": 3,
    },
    "Button:hovered": {"background_color": CELL_HOVERED},
    "Button.Label": {"font_size": SIZE_BODY, "color": TEXT_BODY},
}
STYLE_WEIGHTED_BUTTON = {
    "Button": {
        "background_color": WEIGHTED_FILL,
        "border_color": WEIGHTED_BORDER,
        "border_width": 1,
        "border_radius": 3,
    },
    "Button:hovered": {"background_color": WEIGHTED_BORDER},
    "Button.Label": {"font_size": SIZE_BODY, "color": TEXT_EMPHASIS},
}
STYLE_CAPTURE_BUTTON = {
    "Button": {"background_color": CAPTURE_FILL, "border_radius": 3},
    "Button:hovered": {"background_color": QUIET_BORDER},
    "Button.Label": {"font_size": SIZE_BODY, "color": TEXT_EMPHASIS},
}
#: A button that reads as a link: words, no ground.
STYLE_TEXT_BUTTON = {
    "Button": {"background_color": TRANSPARENT, "padding": 0, "margin": 0},
    "Button:hovered": {"background_color": TRANSPARENT},
    "Button.Label": {"font_size": SIZE_META, "color": TEXT_BODY},
    "Button.Label:hovered": {"color": AMETHYST_BRIGHT},
}
