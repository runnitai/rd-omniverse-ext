"""Renders a tool's declared schema as omni.ui controls, and reads the values back.

A tool publishes a tree of fields: what it takes, how each should look, what the
bounds are. This module turns that tree into controls and collects what the user
entered. It is the difference between running one hardcoded shape and running a
tool at full capacity.

The field-type vocabulary is the one the API publishes, and the dispatch
follows it exactly, so a tool renders the way its author declared it.

Three decisions worth stating up front, each already paid for once:

**An image field's value is the bare descriptor, never a composite.** The server
looks for `kind == "MULTIPART"` at the top level of a value (or one level into a
list, for IMGS). A descriptor nested inside an object is never found, the staged
file gets no field to attach to, and the run dies about a second later with no
error recorded. So strength cannot be sent at all, and the field says so rather
than offering a control that would break the run.

**An empty SEED means random.** It is omitted rather than sent as 0, which is a
specific seed and would make every run identical.

**Group depth decides the chrome.** A depth-1 group gets a collapsable frame;
deeper groups get a label and an indent, because nested frames eat the width a
docked panel does not have.
"""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Callable

import omni.ui as ui

from . import redraw as redraw_module
from .text import shorten
from .theme import (
    AMETHYST as _AMETHYST,
    AMETHYST_BRIGHT as _AMETHYST_BRIGHT,
    BAND_BOTTOM as _BAND_BOTTOM,
    BAND_TOP as _BAND_TOP,
    CELL_EMPTY as _CELL_EMPTY,
    CHIP_PLATE as _CHIP_PLATE,
    DANGER as _DANGER,
    FIELD as _FIELD,
    HAIRLINE as _HAIRLINE,
    SEPARATOR as _SEPARATOR,
    SIZE_BODY as _SIZE_BODY,
    SIZE_CAPS as _SIZE_CAPS,
    SIZE_META as _SIZE_META,
    STYLE_CAPS as _STYLE_CAPS,
    STYLE_CAPTURE_BUTTON as _STYLE_CAPTURE_BUTTON,
    STYLE_HELP as _STYLE_HELP,
    STYLE_META as _STYLE_META,
    STYLE_QUIET_BUTTON as _STYLE_QUIET_BUTTON,
    STYLE_TEXT_BUTTON as _STYLE_TEXT_BUTTON,
    TEXT_BODY as _TEXT_BODY,
    TEXT_EMPHASIS as _TEXT_EMPHASIS,
    TEXT_SECONDARY as _TEXT_SECONDARY,
)
from .api.tools import (
    DISPLAY_ONLY_TYPES,
    IMAGE_TYPES,
    ImageSelection,
    MULTI_IMAGE_TYPES,
    PROMPT_TYPES,
    ToolField,
    MULTI_SELECT_TYPES,
    NUMBER_TYPES,
    SINGLE_SELECT_TYPES,
    SLIDER_TYPES,
    TEXT_TYPES,
    ToolDetail,
)

logger = logging.getLogger(__name__)

#: Presentation keys a tool may nest its choices under. The options are the same
#: either way, so callers should not have to care which the author picked.
_OPTION_PRESENTATIONS = ("text", "radio", "image")

_TAGS = re.compile(r"<[^>]+>")

#: A line with nothing but whitespace on it: the user's own paragraph break.
_BLANK_LINE = re.compile(r"\n[ \t]*\n")

#: Slider bounds when a tool declares none. STEPS counts iterations and CFG is a
#: scale, so they do not share a sensible range.
_SLIDER_FALLBACKS = {
    "STEPS": (0.0, 60.0, 1.0),
    "UPSCALE_FACTOR_SLIDER": (1.0, 4.0, 0.5),
}
_SLIDER_DEFAULT = (0.0, 20.0, 0.1)

#: What the viewfinder's bottom band says while a capture is running. Short on
#: purpose: it replaces the count of images that will be sent, on one line over
#: the picture, and the frame itself is what says which view is being taken.
CAPTURING_TEXT = "Capturing..."

#: Beside an output size a capture has matched to the view. Only while it is
#: still the capture's choice: picking a size by hand takes it away.
SIZE_FOLLOWS_VIEW_TEXT = "follows the view"

#: Edge of an image thumbnail in the image row.
PREVIEW_SIZE = 58

#: The gutter between wrapped thumbnails. Folded into the grid's cell size
#: rather than set as a spacing, because a grid sizes its own cells and a
#: spacing on top of that is what pushes the last column off the edge.
PREVIEW_GAP = 6

#: The tile that adds an image to a field. Smaller than a thumbnail, because it
#: is a control rather than a picture, and a row of equal squares made it read
#: as an image that failed to load.
ADD_TILE = 44

#: The viewfinder at the top of the Create tab, in pixels.
#:
#: The widest element on the tab, because framing a view is the first thing a
#: person does here and the last thing they check before pressing Capture. It
#: follows the panel's width at 16:9 up to TARGET, which is what 16:9 comes to
#: in a panel docked at about 380px, and never shrinks under MIN.
VIEWFINDER_TARGET_HEIGHT = 205
VIEWFINDER_MIN_HEIGHT = 120

#: Below this panel width the frame would be too small to judge a view by, so
#: it is drawn as the one-line strip instead, until someone asks to see it.
VIEWFINDER_STRIP_BELOW = 300

#: Used before the panel has been laid out and cannot say how wide it is.
VIEWFINDER_FALLBACK_WIDTH = 380

#: The strip a viewfinder becomes once it has done its job: a small copy of the
#: last capture, where it came from, and the two things worth doing next.
STRIP_HEIGHT = 40
STRIP_THUMB_WIDTH = 52
STRIP_THUMB_HEIGHT = 30

#: The darkened band along the bottom of the frame that the words sit on.
BAND_HEIGHT = 52

#: The chips over the top corners of the frame.
CHIP_HEIGHT = 22
SOURCE_CHIP_WIDTH = 160

#: The shapes the corner of the frame names, as (width, height). The label is
#: always the nearest of these: a viewport docked by hand is never exactly any
#: ratio, and "1.71:1" reads as arithmetic where "16:9" reads as a shape.
_NAMED_ASPECTS = (
    (21, 9),
    (2, 1),
    (16, 9),
    (16, 10),
    (3, 2),
    (4, 3),
    (5, 4),
    (1, 1),
    (4, 5),
    (3, 4),
    (2, 3),
    (10, 16),
    (9, 16),
)

#: How long after the source dropdown changes a click on the frame is taken to
#: be the tail of choosing a camera rather than a request to capture.
SOURCE_CLICK_GRACE_SECONDS = 0.5

#: The server takes 12 input files per REQUEST, counting multipart parts and
#: references together. Mirrors the server's own per-request input file cap.
#:
#: Per request, not per field, and the difference is the whole point of the
#: name. It was enforced per field, which is the same number for a tool with
#: one image field and twice the limit for a tool with two: 24 images accepted
#: here, uploaded, and then refused with a 413, which is exactly the outcome
#: `has_room` exists to prevent.
MAX_INPUT_FILES = 12

#: How many of a tool's remaining inputs stay on the surface before the rest
#: fold away. Two is what fits under the image row without pushing Generate off
#: a 720px panel.
INLINE_FIELD_LIMIT = 2

#: One line of text in a multiline field, in pixels, at the body size those
#: fields are drawn in.
TEXT_LINE_HEIGHT = 14

#: The type size inside a multiline field. Set rather than inherited, because
#: the line height above is measured against it.
TEXT_FONT_SIZE = 12

#: The chrome above and below the text inside a multiline field.
TEXT_PADDING = 10

#: An EMPTY multiline field never shrinks below this, so a blank prompt still
#: reads as somewhere to write a paragraph rather than as a one-line box.
TEXT_MIN_LINES = 3

#: A field with something in it can be shorter. A one-line prompt in a
#: three-line box spends a line of a docked panel on nothing.
TEXT_MIN_FILLED_LINES = 2

#: Roughly how many pixels one character takes in a field, used to work out how
#: many columns the box currently holds.
#:
#: An average rather than a measurement. omni.ui will not measure a string for
#: us, and the font is proportional, so an `m` and an `l` are not the same
#: width. Erring wide would break lines past the edge and put the horizontal
#: scroll back, which is the whole fault being fixed, so this leans narrow and
#: leaves a ragged right margin instead.
TEXT_CHAR_WIDTH = 7.0

#: Used when the field cannot say how wide it is, which is the case before it
#: has ever been laid out.
TEXT_FALLBACK_COLUMNS = 46

#: However narrow the panel is dragged, wrapping to fewer columns than this
#: turns a prompt into a column of single words.
TEXT_MIN_COLUMNS = 16

#: ...and never grows past this, so a pasted essay cannot push Generate off
#: the bottom of the panel. Past it the field scrolls, which is what every
#: text box does and what the old fixed height did for three lines.
TEXT_MAX_LINES = 14


class LayoutPlan:
    """Which of a tool's fields go where on the Create tab.

    Grouped by POSITION, not by meaning. Tool schemas carry no reliable notion
    of "basic" versus "advanced", and the group labels they do carry are written
    per tool by whoever authored it, so any semantic grouping this panel invents
    would be right for some tools and misleading for the rest.

    Position needs no such knowledge. The prompt and the image fields are found
    by TYPE, which is always reliable. Of whatever remains, the first two render
    inline and everything after them folds into one frame in schema order, under
    a heading that is a count rather than a claim. A tool with three inputs never
    shows the frame at all.
    """

    def __init__(self) -> None:
        self.prompt: ToolField | None = None
        self.images: list[ToolField] = []
        self.inline: list[ToolField] = []
        self.more: list[ToolField] = []

    @property
    def has_more(self) -> bool:
        return bool(self.more)


def prompt_text(tool: ToolDetail, values: dict) -> str:
    """What the user asked for, as words, or "" if this tool takes no prompt.

    Found by TYPE through the same layout plan the form renders by, so the
    answer cannot drift from the field that is actually on screen as the prompt.
    A tool schema's keys are per tool, so there is nothing to hardcode here.
    """
    plan = plan_layout(tool)
    if plan.prompt is None:
        return ""
    value = values.get(plan.prompt.key)
    return str(value).strip() if value else ""


def plan_layout(tool: ToolDetail) -> LayoutPlan:
    """Sort `tool`'s fields into the four positions the Create tab renders."""
    plan = LayoutPlan()
    for tool_field in tool.fields:
        field_type = (tool_field.type or "").upper()
        if field_type in DISPLAY_ONLY_TYPES:
            continue
        if field_type in IMAGE_TYPES:
            plan.images.append(tool_field)
        elif field_type in PROMPT_TYPES and plan.prompt is None:
            # The first PROMPT field only. A tool with two of them is not a
            # thing seen in practice, and picking a second one to promote would
            # be the kind of guess this module exists to avoid.
            plan.prompt = tool_field
        elif len(plan.inline) < INLINE_FIELD_LIMIT:
            plan.inline.append(tool_field)
        else:
            plan.more.append(tool_field)
    return plan


def options_for(display: dict) -> list[tuple[str, str]]:
    """The (label, value) choices on a select-style field."""
    for key in _OPTION_PRESENTATIONS:
        presentation = display.get(key)
        if isinstance(presentation, dict) and isinstance(presentation.get("options"), list):
            out = []
            for option in presentation["options"]:
                if isinstance(option, dict):
                    out.append((str(option.get("text") or ""), str(option.get("value") or "")))
            return out
    return []


def size_options_for(display: dict) -> list[tuple[str, int, int]]:
    """The output sizes a WIDTH_HEIGHT field declares.

    A tool states which sizes it can actually produce, and providers reject
    dimensions off their own grid, so the list is chosen from rather than
    computed.
    """
    select = display.get("select")
    if not isinstance(select, dict) or not isinstance(select.get("options"), list):
        return []

    sizes = []
    for option in select["options"]:
        if not isinstance(option, dict):
            continue
        width, height = option.get("width"), option.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            continue
        text = str(option.get("text") or "").strip()
        label = f"{text} ({width}x{height})" if text else f"{width}x{height}"
        sizes.append((label, width, height))
    return sizes


def size_matching_aspect(
    sizes: list[tuple[str, int, int]], aspect: float | None
) -> int | None:
    """Which declared size is closest in SHAPE to `aspect`, by index.

    A tool states the sizes it can actually produce and providers reject
    dimensions off their own grid, so the viewport cannot decide the pixels.
    What it can decide is which of the offered shapes to take, and that is the
    part a person was doing by hand: capture a wide view, generate it square,
    notice the building is squashed, change the dropdown, generate again.

    Compared as the log of the ratio between the two aspects, which makes the
    measure symmetric: 4:3 is exactly as far from 3:4 as 3:4 is from 4:3. A
    plain subtraction is not, and would quietly prefer landscape rows over
    portrait ones on a tall viewport.

    Where two sizes are the same shape, and they usually are (1024x1024 and
    512x512 both being square), the larger one wins. A person who framed a
    view wants it back at the best the tool offers, and asking them to notice
    that the auto-match took the small square would be worse than not matching
    at all.

    None when there is nothing to choose from or nothing to match against.
    """
    if not sizes or aspect is None or aspect <= 0:
        return None

    best: int | None = None
    best_distance: float | None = None
    for position, (_label, width, height) in enumerate(sizes):
        if width <= 0 or height <= 0:
            continue
        distance = abs(math.log((width / height) / aspect))
        if best_distance is None or distance < best_distance - 1e-9:
            best, best_distance = position, distance
        elif best is not None and abs(distance - best_distance) <= 1e-9:
            _, best_width, best_height = sizes[best]
            if width * height > best_width * best_height:
                best = position
    return best


def cameras_in_image_state(image_state: dict[str, Any] | None) -> list[str]:
    """The cameras a run's inputs were framed by, in the order they were sent.

    Reads the same exported state a run was submitted with, so the answer is
    about that run rather than about whatever the form holds now. Duplicates
    are dropped: two captures from the same camera are one place to stand.

    Empty for a run nobody pinned a view for, which is most of them, and the
    caller shows no way back rather than one that goes nowhere.
    """
    found: list[str] = []
    for slots in (image_state or {}).values():
        for slot in slots or []:
            *_rest, camera_path = slot
            if camera_path and camera_path not in found:
                found.append(camera_path)
    return found


def unwrap_text(text: str) -> str:
    """Take the soft line breaks back out, keeping the paragraphs.

    The rule this and `reflow_text` share, and the only one that makes
    wrapping possible in a toolkit that will not wrap: **a single line break
    is ours, a blank line is yours.** omni.ui gives no way to mark a break as
    soft, so the two kinds have to be told apart by shape rather than by a
    flag, and paragraph-per-blank-line is the convention every plain-text
    editor already uses.

    What it buys is that nothing this module inserted is ever sent. The user
    sees their prompt wrapped; the request carries the prompt they wrote, with
    their paragraph breaks intact and our line breaks gone.
    """
    paragraphs = []
    for block in _BLANK_LINE.split(text or ""):
        joined = " ".join(part.strip() for part in block.split("\n") if part.strip())
        if joined:
            paragraphs.append(joined)
    return "\n\n".join(paragraphs)


def wrap_paragraph(paragraph: str, columns: int) -> list[str]:
    """One paragraph broken into lines of at most `columns`, at spaces only.

    A word longer than the whole line is left alone on a line of its own. It
    is the one case that still scrolls sideways, and breaking a word in half
    to avoid that would corrupt what the user typed to fix how it looks.
    """
    lines: list[str] = []
    current = ""
    for word in paragraph.split(" "):
        if not word:
            continue
        candidate = f"{current} {word}" if current else word
        if current and len(candidate) > columns:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def reflow_text(text: str, columns: int) -> str:
    """`text` wrapped to `columns`, at spaces, paragraphs preserved.

    Unwraps before it wraps, which is what makes this idempotent AND what
    makes it re-flow. Wrapping already-wrapped text without unwrapping it
    first would treat every wrapped line as a paragraph of its own, so
    deleting a word from the middle of a prompt would leave the ragged lines
    it left behind forever.
    """
    if columns < 1:
        return text or ""
    lines: list[str] = []
    for paragraph in unwrap_text(text).split("\n\n"):
        if lines:
            lines.append("")
        lines.extend(wrap_paragraph(paragraph, columns))
    return "\n".join(lines)


def text_area_lines(text: str) -> int:
    """How many lines `text` occupies in a multiline field, bounds included.

    Counts the line breaks that are in the text, and ONLY those.

    An earlier version instead ESTIMATED where the text would wrap and made
    the field taller to fit lines that were never drawn, because omni.ui does
    not wrap a multiline StringField at all. Reported from Base Editor as "the
    box grew but the text didn't wrap", which is precisely what it was doing.

    The answer was not a better estimate: it was to put the breaks in, which
    `reflow_text` does when an edit ends. Once they are really there, counting
    them is exact rather than a guess, and this function needs to know nothing
    about fonts or widths.

    `split` rather than `splitlines`, so a trailing newline counts as the empty
    line it is. Someone who presses Enter at the end of a prompt is asking for
    room to write on, and `splitlines` throws that line away.
    """
    lines = len((text or "").split("\n"))
    floor = TEXT_MIN_FILLED_LINES if (text or "").strip() else TEXT_MIN_LINES
    return max(floor, min(TEXT_MAX_LINES, lines))


def text_area_height(text: str) -> int:
    """The pixel height a multiline field should take to show `text`.

    An empty field comes out at three lines, so there is room to start
    writing; a short prompt settles at two.
    """
    return text_area_lines(text) * TEXT_LINE_HEIGHT + TEXT_PADDING


def viewfinder_height(width: float | None) -> int | None:
    """How tall the viewfinder should be in a panel `width` pixels wide.

    16:9 of the width, held between the minimum and the target. None means
    the panel is too narrow for a frame worth judging a view by, and the strip
    should be drawn instead.

    A width of nothing means the panel has not been laid out yet, which is the
    first frame of every build, so the answer is the one for a panel of the
    usual width rather than the strip: a frame that flashed as a strip and then
    grew would be worse than one that is right a frame late.
    """
    if not width or width <= 0:
        width = VIEWFINDER_FALLBACK_WIDTH
    if width < VIEWFINDER_STRIP_BELOW:
        return None
    return max(
        VIEWFINDER_MIN_HEIGHT, min(VIEWFINDER_TARGET_HEIGHT, round(width * 9 / 16))
    )


def aspect_label(aspect: float | None) -> str:
    """A shape as a person would name it: `16:9`, `1:1`, `9:16`.

    The nearest standard ratio, measured as the log of the ratio between the
    two so that portrait and landscape are judged alike. Empty when there is
    no shape to report, so the corner of the frame says nothing rather than
    something invented.
    """
    if aspect is None or aspect <= 0:
        return ""
    width, height = min(
        _NAMED_ASPECTS, key=lambda pair: abs(math.log(aspect / (pair[0] / pair[1])))
    )
    return f"{width}:{height}"


def summary_part(field_type: str, label: str, value: str) -> str:
    """One folded field's value, with the field's name dropped where it can be.

    The summary under More settings is read at a glance, and repeating every
    field's name turned it into a wrapped two-line sentence. So a bare value is
    shown where the value says what it is ("regular", "1024x1024"), and a word
    of the label is kept only where the value alone would be a stray number or
    a stray "off": "28 steps", "seed random", "expand off".
    """
    if not value:
        return ""
    field_type = (field_type or "").upper()
    first_word = (label or "").strip().split(" ")[0].lower() if label else ""
    if field_type == "STEPS":
        return f"{value} steps"
    if field_type == "SEED":
        return f"seed {value}"
    if field_type == "BOOLEAN" and first_word:
        return f"{first_word} {value}"
    return value


def _caps_label(text: str) -> Any:
    """A field's heading: uppercase, small, and quieter than what it heads.

    omni.ui cannot bold an arbitrary label, so a heading is told from a value
    by size and colour alone. Letter-spacing is not available either, and is
    not faked.
    """
    return ui.Label(str(text).upper(), height=0, style=_STYLE_CAPS)


def _number(display: dict, key: str, fallback: float | None = None) -> float | None:
    value = display.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return fallback


class ImageSlot:
    """One image in one image field: a captured view, or something already uploaded.

    A capture holds the PNG bytes taken at the moment the user asked for it, and
    a thumbnail of them on disk. That pinning is what lets one field hold several
    DIFFERENT views: capture, orbit, capture again, and the first image is still
    the first view rather than being redefined by where the camera ended up.

    `id` is stable for the life of the slot and is how the panel keys the asset
    behind a chosen image. Position would be the obvious key and is the wrong
    one: removing an earlier slot renumbers everything after it, so a preview
    keyed by position starts showing its neighbour's picture.
    """

    def __init__(
        self,
        slot_id: int,
        reference: Any = None,
        description: str = "",
        png: bytes | None = None,
        thumbnail: Any = None,
        camera_path: str | None = None,
    ) -> None:
        self.id = slot_id
        #: None means this slot is a capture, and `png` holds it.
        self.reference = reference
        self.description = description
        #: The captured bytes, sent as their own multipart file part.
        self.png = png
        #: A downscaled copy on disk, because omni.ui.Image loads from a path.
        self.thumbnail = thumbnail
        #: The camera prim this view was framed by, when there is one: either a
        #: camera the user had already authored, or one pinned at the moment of
        #: capture. None for a capture from a viewport nobody asked to pin,
        #: which is the ordinary case and stays the ordinary case.
        #:
        #: This is the whole of what makes a framing returnable. It travels
        #: with the slot into the exported image state and from there onto the
        #: result, so a render made this morning can still say where it was
        #: taken from and put the viewport back there.
        self.camera_path = camera_path

    @property
    def is_capture(self) -> bool:
        return self.reference is None


class FieldControl:
    """One bound control: how to read its value, and whether it holds one."""

    def __init__(
        self,
        key: str,
        read: Callable[[], Any],
        is_empty: Callable[[], bool] | None = None,
        restore: Callable[[Any], None] | None = None,
    ) -> None:
        self.key = key
        self._read = read
        self._is_empty = is_empty or (lambda: False)
        self._restore = restore

    def value(self) -> Any:
        return self._read()

    def is_empty(self) -> bool:
        return self._is_empty()

    def restore(self, value: Any) -> None:
        """Put a previously entered value back, if this control can take it."""
        if self._restore is None:
            return
        self._restore(value)


class ToolForm:
    """Builds controls for a tool and collects their values.

    Rendering and collection are deliberately separate, and `collect` reads the
    live widget models rather than a shadow copy, so there is no second source
    of truth to drift.
    """

    def __init__(
        self,
        on_pick_image=None,
        on_render_preview=None,
        on_capture=None,
        on_render_viewfinder=None,
        capture_sources=None,
        on_view_image=None,
        on_values_changed=None,
        viewfinder_aspect=None,
        on_capture_zone_rendered=None,
    ) -> None:
        self._controls: list[FieldControl] = []
        #: Called with nothing, returning the shape the viewport is rendering
        #: at, for the corner of the frame. The panel owns it for the same
        #: reason it owns the viewfinder's picture.
        self._viewfinder_aspect = viewfinder_aspect
        #: Called after the capture zone is redrawn, so the panel can decide
        #: whether anything on screen still wants a live picture.
        self._on_capture_zone_rendered = on_capture_zone_rendered
        #: The frame at the top of the Create tab that holds the viewfinder
        #: and the Capture button. None when the panel has not given one, which
        #: is the case in a test and before the tab is built.
        self._capture_container: Any = None
        #: Which image field the viewfinder captures into. The first image
        #: field unless the person chose another; a tool with one image field,
        #: which is almost all of them, never shows the choice at all.
        self._capture_target: str | None = None
        #: Image fields whose viewfinder the person asked to see again after it
        #: collapsed. For the rest of the session, not for one capture: someone
        #: who pressed Show viewfinder is framing a series of views, and having
        #: the frame fold away after each one would be arguing with them.
        self._viewfinder_opened: set[str] = set()
        #: What the zone was last drawn as, (collapsed, height). A width change
        #: that does not move either is not worth a rebuild.
        self._capture_layout: Any = None
        #: Field keys the tool marks required, for the image header's OPTIONAL.
        self._required_keys: set[str] = set()
        self.image_field_keys: list[str] = []
        #: Called with a field key when the user wants to choose an image from
        #: somewhere other than the viewport. The panel owns the picker, because
        #: the form has no business knowing about windows.
        self._on_pick_image = on_pick_image
        #: Called with (field key, slot, frame) to fill one image's preview,
        #: and with (field key, frame) to fill the viewfinder. The panel owns
        #: both for the same reason: drawing them means an http fetch, a cache
        #: directory and the live viewport, none of which is the form's.
        self._on_render_preview = on_render_preview
        #: Called with a field key when the user wants to capture the viewport
        #: into it. Capturing is the host's business, not the form's.
        self._on_capture = on_capture
        self._on_render_viewfinder = on_render_viewfinder
        #: Called with nothing, returning the places a capture can be taken
        #: from as (label, value) pairs, first one first. The panel owns it: the
        #: form has no more business reading a USD stage than it has opening a
        #: window.
        self._capture_sources = capture_sources
        #: Called with (field key, slot) to show one pinned image at a size
        #: worth looking at. The panel owns it: showing a chosen asset means
        #: downloading it, and showing anything means a window.
        self._on_view_image = on_view_image
        #: Field key -> the capture source chosen for it, where the value is
        #: whatever the panel put in the pairs above and None means the active
        #: viewport. Per field rather than per form, because a tool with a
        #: start frame and an end frame is exactly the case where two different
        #: cameras is the point.
        self._capture_source: dict[str, Any] = {}
        #: Field key -> how many captures are in flight for it.
        #:
        #: Counted rather than flagged. Nothing disables the Capture button
        #: while one is running, every capture now waits sixty frames for the
        #: renderer, and they serialise behind one lock, so two clicks means
        #: two captures a second or more apart. With a flag the first to finish
        #: cleared the line while the second was still going, and the panel
        #: went silent mid-capture: the exact failure the line was added for.
        self._capture_running: dict[str, int] = {}
        #: Field key -> what its capture in flight is saying, or absent.
        #:
        #: Held beside the error rather than written into a label for the same
        #: reason the error is: the image row is rebuilt whenever an image is
        #: added, removed or selected, and a message living only in a widget
        #: would vanish on the next rebuild with the capture still running.
        self._capture_busy: dict[str, str] = {}
        #: Field key -> why its last capture did not happen, or absent.
        #:
        #: Held rather than written straight into a label, because the image
        #: row is rebuilt whenever an image is added, removed or selected, and
        #: a message that lived only in the widget would vanish on the next
        #: rebuild without the thing it was reporting having changed.
        self._capture_error: dict[str, str] = {}
        #: Field key -> the images it will send, in order. A plural field can
        #: hold several, mixing viewport captures with library and upload
        #: references; a singular one holds at most a single slot.
        self.image_slots: dict[str, list[ImageSlot]] = {}
        self.image_field_labels: dict[str, str] = {}
        self._plural_keys: set[str] = set()
        self._image_containers: dict[str, Any] = {}
        #: Image-row rebuilds waiting for the next frame. Every one of them is
        #: reached from a click, and omni.ui refuses a container clear during
        #: event dispatch. See redraw.py.
        self._redraws = redraw_module.Redraws()
        #: The live frame in each image row, redrawn when the camera moves.
        self._viewfinders: dict[str, Any] = {}
        #: Field key -> the slot id the user clicked, or None. Selecting a slot
        #: is what turns the row header into reorder and remove controls.
        self._selected_slot: dict[str, Any] = {}
        self._slot_counter = 0
        #: Called when any control changes, so the panel can re-price the run.
        self._on_values_changed = on_values_changed
        self._plan: Any = None
        self._summary_label: Any = None
        self._more_frame: Any = None
        #: Field key -> (declared sizes, its dropdown), for every output-size
        #: field on screen. Held so a capture can point the dropdown at the
        #: shape the viewport just handed over without the panel needing to
        #: know what a WIDTH_HEIGHT field is.
        self._size_fields: dict[str, Any] = {}
        #: Field key -> the line under its dropdown saying what a capture
        #: moved it to. Auto-matching in silence would be a control changing
        #: under someone's hands, so every match says so where it happened.
        self._size_notes: dict[str, Any] = {}
        #: Output-size fields the user has set themselves. A capture stops
        #: matching one the moment its owner has an opinion: they chose that
        #: size while looking at the same viewport, so it is not a stale value
        #: to be corrected.
        #:
        #: It also decides the shape of the CAPTURE. A size picked by hand is a
        #: statement about the picture the person wants, so a capture is
        #: cropped to it, and the viewfinder shows that crop before the button
        #: is pressed.
        self._size_chosen_by_hand: set[str] = set()
        #: Output-size fields a capture has set and the user has not touched
        #: since: the ones whose heading says `follows the view`.
        self._size_inherited: set[str] = set()
        #: The tool the size state above belongs to, so rebuilding the same
        #: tool keeps it and switching to another drops it.
        self._built_tool_id: str | None = None
        #: True while this class is the one moving a size dropdown, so its own
        #: change is not mistaken for the user having chosen. Also held while
        #: carried-over values are put back, which move dropdowns too.
        self._matching_aspect = False
        #: When the source dropdown last changed, so a click on the frame that
        #: is really the end of choosing a camera does not also capture.
        self._source_changed_at = 0.0
        #: Subscriptions to multiline fields finishing an edit, held because a
        #: carb Subscription cancels itself the moment nothing refers to it.
        #: Wrapping is done on end-of-edit rather than on every keystroke: see
        #: `_wrap_on_end_edit`.
        self._edit_subscriptions: list[Any] = []
        #: Field key -> the frame a multiline field is built inside, and the
        #: field currently in it. A wrap REPLACES the widget rather than
        #: writing to it, so the control bound at build time has to reach the
        #: current one through here rather than closing over the first.
        self._text_frames: dict[str, Any] = {}
        self._text_widgets: dict[str, Any] = {}
        #: Field key -> whether its next viewport capture should also pin the
        #: framing as a camera on the stage. Off unless asked, every time: a
        #: capture that quietly authored a prim would be this panel editing
        #: someone's scene as a side effect of taking a photograph.
        self._pin_view: dict[str, bool] = {}

    # -- rendering ---------------------------------------------------------

    def build(
        self,
        container: Any,
        tool: ToolDetail,
        carry_over: dict[str, Any] | None = None,
        capture_container: Any = None,
    ) -> None:
        """Render `tool` into `container`, replacing whatever was there.

        Laid out by position (see `plan_layout`): prompt, images, two inline
        fields, and one folded frame for the rest. The schema's own grouping is
        deliberately not used, because it is authored per tool and means
        something different in each one.

        `capture_container` is where the viewfinder and the Capture button go.
        It is a separate frame, above the tool row, because capturing is the
        first thing done on this tab and the frame is the widest thing on it;
        the image fields below are about what a run carries rather than about
        how a view is taken.

        `carry_over` holds values from the previously shown tool. A value is
        restored only where the SAME fieldKey exists in this schema too, and is
        dropped otherwise: trying a second model on the same prompt is the most
        common thing this panel does, and retyping the prompt each time is the
        friction that makes it feel like a form rather than a tool.
        """
        self._controls = []
        self.image_field_keys = []
        self.image_field_labels = {}
        # Queued against containers this is about to replace. They would find
        # the new ones and draw the right thing, but booking a frame to redraw
        # a row that is being built from scratch anyway is pure waste.
        self._redraws.cancel()
        self._image_containers = {}
        self._viewfinders = {}
        self._selected_slot = {}
        # Belongs to the tool that was on screen. A camera chosen for one
        # tool's field says nothing about another tool's.
        self._capture_source = {}
        self._capture_error = {}
        self._capture_busy = {}
        self._capture_running = {}
        self._summary_label = None
        self._more_frame = None
        # Output sizes belong to the tool that declared them, and so does the
        # user having chosen one: another tool offers another list. The same
        # tool rebuilt (a tab switch, Reuse its inputs) keeps both, or a size
        # picked by hand would quietly go back to following the viewport, and
        # captures would stop being cropped to it, after a visit to Library.
        self._size_fields = {}
        self._size_notes = {}
        if tool.id != self._built_tool_id:
            self._size_chosen_by_hand = set()
            self._size_inherited = set()
        self._built_tool_id = tool.id
        # An intention about one tool's field, not a standing preference.
        self._pin_view = {}
        # The fields they were watching are about to be destroyed.
        self._edit_subscriptions = []
        self._text_frames = {}
        self._text_widgets = {}
        # Chosen images belong to the tool that was on screen, so they are
        # dropped with it rather than leaking onto the next tool's fields.
        self.image_slots = {}
        # Which fields take a LIST is a property of the schema, so it is read
        # once here rather than passed around with every slot.
        self._plural_keys = {
            f.key for f in tool.fields if f.type.upper() in MULTI_IMAGE_TYPES
        }
        self._required_keys = {f.key for f in tool.fields if f.required}
        self._plan = plan_layout(tool)
        self._capture_container = capture_container
        self._capture_layout = None
        # Kept where the new tool has the same field, which is the case for a
        # rebuild that is not a tool change at all. Otherwise the first image
        # field, which is the one a tool is mostly about.
        image_keys = [f.key for f in self._plan.images]
        if self._capture_target not in image_keys:
            self._capture_target = image_keys[0] if image_keys else None
        # Booked before the form is built rather than after, so a tool that
        # declares no inputs at all still clears the zone the last tool drew.
        self._render_capture_zone()

        container.clear()
        with container:
            with ui.VStack(spacing=6, height=0):
                if not tool.fields:
                    ui.Label("This tool declares no inputs.")
                    return

                if self._plan.prompt is not None:
                    field = self._plan.prompt
                    with ui.VStack(spacing=4, height=0):
                        _caps_label(field.label or "Prompt")
                        self._build_text(
                            field.key, "", field.default_value, multiline=True
                        )

                for field in self._plan.images:
                    self._build_image(field.key, field.label or "Images")

                for field in self._plan.inline:
                    self._build_planned_field(field)

                if self._plan.has_more:
                    self._build_more_settings(self._plan.more)

        if carry_over:
            self._restore(carry_over)
        self._refresh_summary()

    def _build_planned_field(self, field: ToolField) -> None:
        """One field, from the flat plan rather than from the schema tree.

        In a stack of its own, so a label sits closer to its control than to
        the field above it.
        """
        with ui.VStack(spacing=4, height=0):
            self._build_field(
                {
                    "fieldKey": field.key,
                    "type": field.type,
                    "label": field.label,
                    "display": field.display,
                    "defaultValue": field.default_value,
                }
            )

    def _build_more_settings(self, fields: list[ToolField]) -> None:
        """Everything past the first two inputs, folded away but not hidden.

        The heading is a COUNT, never a claim about what the fields are, and the
        line inside the header prints their current values as plain text. So a
        closed frame still tells the truth about what will be sent, which is the
        whole reason folding these away is safe.

        The summary is IN the header rather than under the frame, so it reads as
        a description of the frame instead of as a field of its own.
        """
        title = f"More settings ({len(fields)})"
        self._more_frame = ui.CollapsableFrame(
            title,
            collapsed=True,
            height=0,
            build_header_fn=lambda collapsed, _title, t=title: self._build_more_header(
                collapsed, t
            ),
        )
        with self._more_frame:
            with ui.VStack(spacing=6, height=0):
                for field in fields:
                    self._build_planned_field(field)

    def _build_more_header(self, collapsed: bool, title: str) -> None:
        """The More settings title with the summary beneath it.

        omni.ui calls this again every time the frame opens or closes, and the
        label it makes is a new one each time, so the reference is re-pointed
        here rather than held from the first build.
        """
        with ui.HStack(height=0, spacing=6):
            # The disclosure triangle the default header draws, which a custom
            # header has to draw for itself.
            with ui.VStack(width=8):
                ui.Spacer(height=4)
                ui.Triangle(
                    width=8,
                    height=8,
                    alignment=(
                        ui.Alignment.RIGHT_CENTER if collapsed else ui.Alignment.CENTER_BOTTOM
                    ),
                    style={"background_color": _TEXT_SECONDARY},
                )
                ui.Spacer()
            with ui.VStack(height=0, spacing=2):
                ui.Label(title, height=0, style={"font_size": _SIZE_BODY, "color": _TEXT_EMPHASIS})
                self._summary_label = ui.Label("", height=0, style=_STYLE_META)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        """Redraw the plain-text summary in the folded frame's header."""
        if self._summary_label is None or self._plan is None:
            return
        parts = []
        for field in self._plan.more:
            part = summary_part(field.type, field.label or "", self._value_text(field))
            if part:
                parts.append(part)
        # One line. The header is not the place for a paragraph, and the frame
        # itself is one click away for anyone who wants every value.
        text = " · ".join(parts) if parts else "Tool defaults."
        self._summary_label.text = shorten(text, 64)

    def _value_text(self, field: ToolField) -> str:
        """One field's current value, as a person would read it."""
        control = next((c for c in self._controls if c.key == field.key), None)
        if control is None:
            return ""
        try:
            if control.is_empty():
                return "random" if field.type.upper() == "SEED" else ""
            value = control.value()
        except Exception:  # noqa: BLE001 - a summary is never worth an exception
            return ""
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, dict):
            width, height = value.get("width"), value.get("height")
            return f"{width}x{height}" if width and height else ""
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    def _on_control_changed(self, *_args) -> None:
        """A value changed: refresh the summary, and tell the panel.

        The panel uses this to re-price the run, which is what keeps the cost
        above Generate true without anyone pressing a Check cost button.
        """
        self._refresh_summary()
        if self._on_values_changed is not None:
            try:
                self._on_values_changed()
            except Exception:  # noqa: BLE001
                logger.exception("RunDiffusion: value-changed callback failed.")

    def _restore(self, values: dict[str, Any]) -> None:
        # Putting a carried-over size back moves its dropdown, and that is not
        # the user choosing one. Without this every tab switch marked every
        # size as picked by hand.
        self._matching_aspect = True
        try:
            for control in self._controls:
                if control.key not in values:
                    continue
                try:
                    control.restore(values[control.key])
                except Exception:  # noqa: BLE001 - a value that no longer fits is dropped
                    logger.debug(
                        "RunDiffusion: could not carry %s over to this tool.", control.key
                    )
        finally:
            self._matching_aspect = False
        for key in self._size_inherited:
            note = self._size_notes.get(key)
            if note is not None:
                note.text = SIZE_FOLLOWS_VIEW_TEXT

    def _build_field(self, node: dict) -> None:
        key = str(node["fieldKey"])
        field_type = str(node.get("type") or "").upper()
        label = str(node.get("label") or "").strip()
        display = node.get("display") if isinstance(node.get("display"), dict) else {}
        default = node.get("defaultValue")

        if field_type == "HEADER":
            if label:
                _caps_label(label)
            return
        if field_type == "HTML":
            stripped = _TAGS.sub("", str(display.get("content") or "")).strip()
            if stripped:
                ui.Label(stripped, word_wrap=True)
            return

        if field_type in IMAGE_TYPES:
            self._build_image(key, label or "Image")
            return
        if field_type in ("VIDEO", "ASSET_3D"):
            ui.Label(f"{label or field_type}: not supported by this plugin yet.")
            return
        if field_type == "SEED":
            self._build_seed(key, label or "Seed", default)
            return
        if field_type == "BOOLEAN":
            self._build_boolean(key, label or key, default)
            return
        if field_type == "WIDTH_HEIGHT":
            self._build_width_height(key, label or "Output size", display, default)
            return
        if field_type in SINGLE_SELECT_TYPES:
            self._build_single_select(key, label or key, display, default)
            return
        if field_type in MULTI_SELECT_TYPES:
            self._build_multi_select(key, label or key, display, default)
            return
        if field_type in SLIDER_TYPES:
            self._build_slider(key, label or key, field_type, display, default)
            return
        if field_type in NUMBER_TYPES:
            self._build_number(key, label or key, display, default)
            return

        # TEXT, TEXT_AREA, PROMPT, NEG_PROMPT, and anything unrecognised. A text
        # box is the safe fallback: it holds whatever the tool wanted, where a
        # guessed control would silently constrain it.
        multiline = field_type in ("PROMPT", "NEG_PROMPT", "TEXT_AREA") or field_type not in TEXT_TYPES
        self._build_text(key, label or key, default, multiline=multiline)

    # -- individual controls ----------------------------------------------

    def _build_text(self, key: str, label: str, default: Any, *, multiline: bool) -> None:
        # An empty label still costs a row, and the prompt renders its own
        # heading above the field, so it passes none.
        if label:
            _caps_label(label)

        text = str(default) if isinstance(default, str) else ""
        if not multiline:
            widget = ui.StringField(height=22)
            if text:
                widget.model.set_value(text)
            widget.model.add_value_changed_fn(self._on_control_changed)
            self._text_widgets[key] = widget
            self._controls.append(self._text_control(key, multiline=False))
            return

        # A frame of its own, because wrapping REPLACES this field rather than
        # writing to it. See `_fill_text_field`.
        frame = ui.Frame(height=0)
        self._text_frames[key] = frame
        self._fill_text_field(key, text)
        self._controls.append(self._text_control(key, multiline=True))

    def _text_control(self, key: str, *, multiline: bool) -> FieldControl:
        """Bind a text field by KEY rather than by widget.

        A multiline field is replaced whenever its text is rewrapped, so a
        control closing over the widget it was built with would go on reading
        a field that is no longer on screen: the prompt would look right and
        the run would carry whatever was in the box before the last reflow.
        """

        def read() -> str:
            widget = self._text_widgets.get(key)
            if widget is None:
                return ""
            text = widget.model.get_value_as_string()
            # The soft breaks come back out on the way to the request. They
            # were put there to make the prompt readable in a box that will
            # not wrap, and they are this panel's business rather than the
            # model's. A blank line survives, because that one IS the user's:
            # see `unwrap_text`.
            return unwrap_text(text) if multiline else text

        def restore(value: Any) -> None:
            if multiline:
                # Reused inputs and carried-over values arrive unwrapped,
                # because that is how they were read out. Rebuilt rather than
                # written into for the same reason a reflow is.
                #
                # Straight away rather than next frame, unlike `_rewrap`. Every
                # caller of this reaches it through `build`, which has just
                # cleared the whole form's container in the same call, so one
                # more clear here is not a new thing happening during dispatch.
                # Deferring it would leave a frame in which `collect` reads an
                # empty prompt and the cost line prices the wrong run.
                self._fill_text_field(key, str(value))
                return
            widget = self._text_widgets.get(key)
            if widget is not None:
                widget.model.set_value(str(value))

        return FieldControl(key, read, lambda: not read().strip(), restore)

    def _fill_text_field(self, key: str, text: str) -> None:
        """Build the multiline field for `key`, wrapped, replacing any before it.

        Replacing rather than writing into the existing one, and that is the
        whole point of this method. omni.ui keeps a field's editing state, the
        horizontal scroll offset among it, beside the widget rather than in
        the model. Writing a rewrapped value into a field the user had
        scrolled to column sixty of left that offset behind: clicking back in
        restored it, and since the wrapped lines are all short, the box showed
        blank space until the first keystroke pulled it back. Reported from
        Base Editor as "the text disappears until I start typing again".

        A new widget has no such state to restore. The old one goes with the
        frame it was in.
        """
        frame = self._text_frames.get(key)
        if frame is None:
            return

        wrapped = reflow_text(text, self._columns_for(key))
        frame.clear()
        with frame:
            widget = ui.StringField(
                multiline=True,
                height=text_area_height(wrapped),
                # Set rather than inherited: TEXT_LINE_HEIGHT is measured at
                # this size, and the host's larger default would put three
                # lines of text in a box sized for two.
                style={"font_size": TEXT_FONT_SIZE},
            )
        if wrapped:
            widget.model.set_value(wrapped)
        widget.model.add_value_changed_fn(self._on_control_changed)
        # The box follows the lines on every keystroke rather than on focus
        # loss: a field that grows only after you look away does not grow
        # while you are writing, which is the moment that matters.
        widget.model.add_value_changed_fn(
            lambda _model, k=key: self._resize_text_area(k)
        )
        self._text_widgets[key] = widget
        self._wrap_on_end_edit(key, widget)

    def _wrap_on_end_edit(self, key: str, widget) -> None:
        """Reflow a multiline field's text when the user finishes editing it.

        omni.ui does not wrap a multiline StringField. `word_wrap` belongs to
        Label, and there is no editable widget in the toolkit that wraps, so a
        long prompt is one line that scrolls sideways however tall the box is.
        The only way to wrap it is to put real line breaks in, and
        `unwrap_text` is what keeps those breaks out of the request.

        **On end-of-edit, never on a keystroke.** Rewrapping replaces the
        field, and replacing a field somebody is typing into would take the
        keyboard away from them mid-word. Waiting until the edit is over means
        there is nothing to interrupt.

        The trade is that the line being typed can run past the right edge
        until the user clicks away, and then the whole prompt reflows. That is
        the bargain omni.ui leaves available.
        """
        subscribe = getattr(widget.model, "subscribe_end_edit_fn", None)
        if subscribe is None:
            # A model that cannot say when an edit ended. The field still
            # grows with the breaks the user types; it just does not add any.
            return
        # Held, because carb cancels a Subscription as soon as the last
        # reference to it goes, and a subscription made and dropped inside
        # this method is one that never fires.
        self._edit_subscriptions.append(subscribe(lambda _model, k=key: self._rewrap(k)))

    def _columns_for(self, key: str) -> int:
        """How many characters fit across this field, as best it can be known.

        Asked of the widget rather than fixed, so wrapping follows the panel:
        someone who drags the dock wider gets longer lines the next time the
        prompt reflows. Before the first layout there is no width to report,
        which is why there is a fallback rather than a zero.
        """
        widget = self._text_widgets.get(key)
        try:
            width = float(getattr(widget, "computed_content_width", 0) or 0)
        except Exception:  # noqa: BLE001 - a width that cannot be read is not a failure
            width = 0.0
        if width <= 0:
            return TEXT_FALLBACK_COLUMNS
        return max(TEXT_MIN_COLUMNS, int(width / TEXT_CHAR_WIDTH))

    def _rewrap(self, key: str) -> None:
        """Rebuild this field with its text wrapped, if wrapping moves it.

        Next frame, not now. This is reached from the model's end-of-edit
        callback, and rebuilding clears a container during event dispatch,
        which omni.ui refuses (see redraw.py). Coalesced by key too, so a
        field that ends several edits before the frame arrives is rebuilt
        once.

        Nothing happens at all when the wrapping changes nothing. A field
        clicked into and straight back out of must not flicker, and rebuilding
        it would also push a value change through the form and re-price the
        run for no reason.
        """
        widget = self._text_widgets.get(key)
        if widget is None:
            return
        try:
            current = widget.model.get_value_as_string()
            if reflow_text(current, self._columns_for(key)) == current:
                return
        except Exception:  # noqa: BLE001 - a field that cannot be read is left alone
            logger.exception("RunDiffusion: could not wrap a text field.")
            return
        self._redraws.request(
            f"text:{key}", lambda k=key, value=current: self._fill_text_field(k, value)
        )

    def _resize_text_area(self, key: str) -> None:
        """Set a multiline field's height to fit what is in it.

        A failure here is never worth losing the keystroke over. The height is
        cosmetic: the text is already in the model, the run will carry it
        whatever size the box is, and a toolkit that will not take a height is
        a field that stays the size it was.
        """
        widget = self._text_widgets.get(key)
        if widget is None:
            return
        try:
            widget.height = ui.Pixel(
                text_area_height(widget.model.get_value_as_string())
            )
        except Exception:  # noqa: BLE001 - see the docstring
            logger.exception("RunDiffusion: could not resize a text field.")

    def _build_seed(self, key: str, label: str, default: Any) -> None:
        _caps_label(f"{label} · empty is random")
        widget = ui.StringField(height=22)
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            widget.model.set_value(str(int(default)))
        widget.model.add_value_changed_fn(self._on_control_changed)

        def read() -> Any:
            raw = widget.model.get_value_as_string().strip()
            try:
                return int(raw)
            except ValueError:
                return None

        # Empty means random, so the field is OMITTED rather than sent as 0.
        # Zero is a specific seed and would make every run identical.
        self._controls.append(
            FieldControl(
                key, read, lambda: read() is None, lambda value: widget.model.set_value(str(value))
            )
        )

    def _build_boolean(self, key: str, label: str, default: Any) -> None:
        with ui.HStack(height=22, spacing=6):
            widget = ui.CheckBox(width=20)
            ui.Label(label, style={"font_size": _SIZE_BODY, "color": _TEXT_BODY})
        widget.model.set_value(bool(default))
        widget.model.add_value_changed_fn(self._on_control_changed)
        self._controls.append(
            FieldControl(
                key,
                lambda: widget.model.get_value_as_bool(),
                restore=lambda value: widget.model.set_value(bool(value)),
            )
        )

    def _build_number(self, key: str, label: str, display: dict, default: Any) -> None:
        _caps_label(label)
        minimum = _number(display, "min")
        maximum = _number(display, "max")
        step = _number(display, "step", 1.0) or 1.0
        whole = float(step).is_integer()

        widget = ui.IntField(height=22) if whole else ui.FloatField(height=22)
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            widget.model.set_value(int(default) if whole else float(default))
        widget.model.add_value_changed_fn(self._on_control_changed)

        def read() -> Any:
            value = widget.model.get_value_as_int() if whole else widget.model.get_value_as_float()
            # Clamped rather than submitted out of range: providers reject
            # values off their grid, and finding that out costs a full round
            # trip.
            if minimum is not None:
                value = max(value, int(minimum) if whole else minimum)
            if maximum is not None:
                value = min(value, int(maximum) if whole else maximum)
            return value

        self._controls.append(
            FieldControl(
                key,
                read,
                restore=lambda value: widget.model.set_value(
                    int(value) if whole else float(value)
                ),
            )
        )

    def _build_slider(
        self, key: str, label: str, field_type: str, display: dict, default: Any
    ) -> None:
        fallback = _SLIDER_FALLBACKS.get(field_type, _SLIDER_DEFAULT)
        minimum = _number(display, "min", fallback[0])
        maximum = _number(display, "max", fallback[1])
        step = _number(display, "step", fallback[2]) or fallback[2]
        minimum = fallback[0] if minimum is None else minimum
        maximum = fallback[1] if maximum is None else maximum
        whole = float(step).is_integer()

        _caps_label(label)
        if whole:
            int_widget = ui.IntSlider(min=int(minimum), max=int(maximum), height=22)
            if isinstance(default, (int, float)) and not isinstance(default, bool):
                int_widget.model.set_value(int(default))
            int_widget.model.add_value_changed_fn(self._on_control_changed)
            self._controls.append(
                FieldControl(
                    key,
                    lambda: int_widget.model.get_value_as_int(),
                    restore=lambda value: int_widget.model.set_value(int(value)),
                )
            )
        else:
            float_widget = ui.FloatSlider(min=float(minimum), max=float(maximum), height=22)
            if isinstance(default, (int, float)) and not isinstance(default, bool):
                float_widget.model.set_value(float(default))
            float_widget.model.add_value_changed_fn(self._on_control_changed)
            self._controls.append(
                FieldControl(
                    key,
                    lambda: float_widget.model.get_value_as_float(),
                    restore=lambda value: float_widget.model.set_value(float(value)),
                )
            )

    def _build_single_select(self, key: str, label: str, display: dict, default: Any) -> None:
        options = options_for(display)
        if not options:
            # No declared choices means nothing to pick from. A text box at
            # least lets a value through.
            self._build_text(key, label, default, multiline=False)
            return

        _caps_label(label)
        values = [value for _, value in options]
        index = values.index(default) if isinstance(default, str) and default in values else 0
        widget = ui.ComboBox(index, *[text or value for text, value in options], height=22)
        widget.model.add_item_changed_fn(self._on_control_changed)

        def read() -> Any:
            chosen = widget.model.get_item_value_model().as_int
            return values[chosen] if 0 <= chosen < len(values) else None

        def restore(value: Any) -> None:
            # A value the tool no longer offers is left alone rather than
            # forced to position 0: a carried-over choice from another schema
            # must not silently become "whatever is first".
            if value in values:
                widget.model.get_item_value_model().set_value(values.index(value))

        self._controls.append(FieldControl(key, read, lambda: read() is None, restore))

    def _build_multi_select(self, key: str, label: str, display: dict, default: Any) -> None:
        options = options_for(display)
        if not options:
            self._build_text(key, label, default, multiline=False)
            return

        _caps_label(label)
        selected = default if isinstance(default, list) else []
        boxes: list[tuple[str, Any]] = []
        for text, value in options:
            with ui.HStack(height=22, spacing=6):
                box = ui.CheckBox(width=20)
                ui.Label(text or value, style={"font_size": _SIZE_BODY, "color": _TEXT_BODY})
            box.model.set_value(value in selected)
            box.model.add_value_changed_fn(self._on_control_changed)
            boxes.append((value, box))

        def read() -> Any:
            return [value for value, box in boxes if box.model.get_value_as_bool()]

        def restore(value: Any) -> None:
            chosen = set(value if isinstance(value, list) else [])
            for option, box in boxes:
                box.model.set_value(option in chosen)

        self._controls.append(FieldControl(key, read, lambda: not read(), restore))

    def _build_width_height(self, key: str, label: str, display: dict, default: Any) -> None:
        sizes = size_options_for(display)
        if not sizes:
            # Fall back to the tool's own default object rather than inventing
            # {width, height}: the sub-keys of a composite are private to the
            # tool unless it declares them.
            if isinstance(default, dict):
                self._controls.append(FieldControl(key, lambda: default))
            return

        # The note shares the heading's line rather than taking one of its own
        # under the dropdown. Absent until a capture has something to say, so
        # it costs nothing on a tool nobody has captured into.
        with ui.HStack(height=20, spacing=6):
            _caps_label(label)
            self._size_notes[key] = ui.Label(
                "", width=0, alignment=ui.Alignment.RIGHT_CENTER, style=_STYLE_META
            )
        index = 0
        if isinstance(default, dict):
            for position, (_, width, height) in enumerate(sizes):
                if default.get("width") == width and default.get("height") == height:
                    index = position
                    break
        widget = ui.ComboBox(index, *[text for text, _, _ in sizes], height=24)
        widget.model.add_item_changed_fn(self._on_control_changed)
        # A capture can move this dropdown, so a change has to say which of
        # the two of them made it. Anything the user did stops the matching
        # for this field: see `_size_chosen_by_hand`.
        widget.model.add_item_changed_fn(
            lambda _model, _item, k=key: self._on_size_chosen(k)
        )
        self._size_fields[key] = (sizes, widget)

        def read() -> Any:
            chosen = widget.model.get_item_value_model().as_int
            if not 0 <= chosen < len(sizes):
                return None
            _, width, height = sizes[chosen]
            # {width, height} IS the documented shape for this composite, unlike
            # strength, whose sub-keys are private and cannot be sent at all.
            return {"width": width, "height": height}

        def restore(value: Any) -> None:
            if not isinstance(value, dict):
                return
            for position, (_text, width, height) in enumerate(sizes):
                if value.get("width") == width and value.get("height") == height:
                    widget.model.get_item_value_model().set_value(position)
                    return

        self._controls.append(FieldControl(key, read, lambda: read() is None, restore))

    def _on_size_chosen(self, key: str) -> None:
        """The user moved an output size themselves, so stop matching it.

        Someone who picks a size is looking at the same viewport the match
        would be reading, so a later capture putting it back would be the
        panel arguing with them.
        """
        if self._matching_aspect:
            return
        self._size_chosen_by_hand.add(key)
        self._size_inherited.discard(key)
        note = self._size_notes.get(key)
        if note is not None:
            note.text = ""
        # The chosen size is now the shape a capture takes, so the viewfinder
        # redraws to show the crop before anything is captured.
        self._render_capture_zone()

    def capture_aspect(self) -> float | None:
        """The shape a capture is cropped to, or None to take the viewport's.

        The first output size the user picked by hand. Until they pick one,
        the viewport decides the shape and the size follows it; once they
        have, the size decides and the capture follows it. Either way the
        picture sent and the size asked for agree.
        """
        for key, (sizes, widget) in self._size_fields.items():
            if key not in self._size_chosen_by_hand:
                continue
            try:
                chosen = widget.model.get_item_value_model().as_int
            except Exception:  # noqa: BLE001 - a dropdown mid-teardown has no answer
                continue
            if 0 <= chosen < len(sizes):
                _label, width, height = sizes[chosen]
                if width > 0 and height > 0:
                    return width / height
        return None

    def match_capture_aspect(self, aspect: float | None) -> None:
        """Point the output sizes at the shape of a view just captured.

        Called with the aspect ratio of the pixels that were actually read,
        which is the honest source: it is whatever the renderer produced,
        so it is right whether the viewport is filling its frame or sitting
        letterboxed inside it, and there is no setting for anyone to have to
        turn off first.

        Every output-size field on the form, not only the first. A tool with
        two of them means both, and a tool with none means nothing happens.
        """
        if aspect is None or aspect <= 0:
            return
        for key, (sizes, widget) in self._size_fields.items():
            if key in self._size_chosen_by_hand:
                continue
            position = size_matching_aspect(sizes, aspect)
            if position is None:
                continue
            label, width, height = sizes[position]
            self._matching_aspect = True
            try:
                widget.model.get_item_value_model().set_value(position)
            except Exception:  # noqa: BLE001 - a size that will not move is not a lost capture
                logger.exception("RunDiffusion: could not match the output size.")
                continue
            finally:
                self._matching_aspect = False
            self._size_inherited.add(key)
            note = self._size_notes.get(key)
            if note is not None:
                # Short, and beside the heading. The dropdown itself now shows
                # the size it was moved to, so the note only has to say why,
                # and it disappears the moment the user picks one by hand.
                note.text = SIZE_FOLLOWS_VIEW_TEXT

    def _build_image(self, key: str, label: str) -> None:
        """An image field, showing every image it will actually send.

        A plural field holds SEVERAL images and they can come from anywhere: two
        viewport captures and a library reference is a valid answer, and so is
        none at all. A singular field holds one, so adding replaces rather than
        appends.

        No value is collected for a capture: the panel injects the MULTIPART
        descriptor at submit time, because the bytes do not exist until the
        capture runs. A chosen asset is different, since it is already on the
        server, so it is recorded here as a plain reference.
        """
        self.image_field_keys.append(key)
        self.image_field_labels[key] = label
        self.image_slots.setdefault(key, [])

        # The label lives inside the row's own header, which also carries the
        # count and the reorder controls, so there is no separate title line.
        container = ui.Frame(height=0)
        self._image_containers[key] = container
        self._render_image_field(key)

    def _render_image_field(self, key: str) -> None:
        """Ask for one image row to be redrawn on the next frame.

        Selecting a thumbnail, moving one, removing one and adding one all
        rebuild this row, and all four are click handlers. omni.ui refuses a
        container clear during event dispatch, which is what selecting a
        thumbnail was caught doing in Kit 110.1.2:

            [Error] [omni.ui] Container::clear was called during an event or
            draw, this is not supported
              clicked_fn=lambda k=key, i=slot.id: self._select_slot(k, i)
              self._render_image_field(key)
              container.clear()

        The capture zone is asked for too. It reads the same slots (how many
        will be sent, what the next capture becomes, whether the frame has
        collapsed) and the same capture state, so anything that changes a row
        changes what the zone says.

        The container is checked BEFORE booking the frame, not only inside the
        render. A form with no widgets is the normal state in a test and during
        a tool switch, and booking work for it would need an event loop that a
        plain Python process does not have.
        """
        if key == self._capture_target:
            self._render_capture_zone()
        if self._image_containers.get(key) is None:
            return
        self._redraws.request(f"image:{key}", lambda k=key: self._render_image_field_now(k))

    def _render_image_field_now(self, key: str) -> None:
        """Redraw one image field: header, then the images or the empty state.

        Rebuilt rather than patched, because the number of images changes and a
        stack cannot have a child taken out of the middle of it.

        The row is only about what the run carries. Taking a view has a zone of
        its own at the top of the tab, under the frame it captures, so nothing
        here needs a fixed-width column beside the images any more and the grid
        gets the whole width.

        Wrapping rather than one long line, because a field can hold twelve
        images and a single row of twelve ran off the edge of a docked panel
        into a horizontal scroll. The grid works out its own column count from
        the width it is given.
        """
        container = self._image_containers.get(key)
        if container is None:
            return

        slots = self.image_slots.get(key) or []
        plural = key in self._plural_keys
        selected = self._selected_slot.get(key)
        if selected is not None and not any(slot.id == selected for slot in slots):
            selected = None
            self._selected_slot.pop(key, None)

        container.clear()
        with container:
            with ui.VStack(spacing=4, height=0):
                self._build_image_header(key, slots, selected)

                if not slots:
                    # The empty state says how the field fills, once, in the
                    # place it fills. Gone as soon as there is an image: help
                    # that stays on screen after it has been read is the
                    # permanent prose this layout exists to remove.
                    with ui.HStack(height=ADD_TILE, spacing=8):
                        self._build_add_tile(key)
                        with ui.VStack(spacing=2):
                            ui.Spacer()
                            ui.Label(
                                "Captures land here in send order.",
                                height=0,
                                style=_STYLE_HELP,
                            )
                            ui.Label(
                                "Add image... to bring in a file or an upload.",
                                height=0,
                                style=_STYLE_HELP,
                            )
                            ui.Spacer()
                    return

                # `column_width` rather than a column count, so the number per
                # row follows the panel: several across a narrow dock, more
                # when it is pulled wider, and never one more than fits.
                with ui.VGrid(
                    column_width=PREVIEW_SIZE + PREVIEW_GAP,
                    row_height=PREVIEW_SIZE + PREVIEW_GAP,
                    height=0,
                ):
                    for position, slot in enumerate(slots):
                        self._build_slot(key, slot, position, slot.id == selected)
                    if plural:
                        # Among the images rather than under them, so it wraps
                        # with them and stays after the last one.
                        self._build_add_tile(key)

    def _build_add_tile(self, key: str) -> None:
        """The tile that brings an image in from the library, uploads or a file.

        A bordered square with a plus rather than a button labelled "Add
        image...": it sits among pictures, and a grey button the size of a
        thumbnail read as a picture that had failed to load. omni.ui has no
        dashed border, so the border is solid and hairline.
        """
        with ui.ZStack(width=ADD_TILE, height=ADD_TILE):
            ui.Rectangle(
                style={
                    "background_color": _FIELD,
                    "border_color": _HAIRLINE,
                    "border_width": 1,
                    "border_radius": 3,
                }
            )
            ui.Label(
                "+",
                alignment=ui.Alignment.CENTER,
                style={"font_size": 18, "color": _TEXT_SECONDARY},
            )
            ui.InvisibleButton(
                tooltip="Add image...",
                clicked_fn=lambda k=key: self._pick_into(k),
            )

    # -- the capture zone --------------------------------------------------

    def _render_capture_zone(self) -> None:
        """Ask for the viewfinder and the Capture button to be redrawn.

        Next frame, like every other rebuild here: it is reached from the
        Capture button, the frame itself, the source dropdown and Show
        viewfinder, all of which are dispatching an event when they call it.
        Guarded before booking for the same reason `_render_image_field` is.
        """
        if self._capture_container is None:
            return
        self._redraws.request("capture-zone", self._render_capture_zone_now)

    def capture_zone_layout(self, width: float | None) -> tuple[bool, int]:
        """(collapsed, frame height) for the zone at a panel `width`.

        Collapsed means the one-line strip. It is what the frame becomes once
        the field it serves holds a capture, until Show viewfinder asks for it
        back, and what a panel too narrow for a useful frame gets from the
        start. Asking for the frame back in a panel that narrow gets the
        smallest frame rather than nothing.
        """
        key = self._capture_target
        height = viewfinder_height(width)
        slots = (self.image_slots.get(key) or []) if key else []
        if key in self._viewfinder_opened:
            return False, height or VIEWFINDER_MIN_HEIGHT
        if height is None or any(slot.is_capture for slot in slots):
            return True, STRIP_HEIGHT
        return False, height

    def on_width_changed(self, width: float | None = None) -> None:
        """The panel was resized. Redraw the zone only if its shape moved.

        Most resizes change neither whether the frame is a strip nor its
        height once it has reached the target, and rebuilding a dropdown under
        a pointer that is dragging a dock edge is a flicker for nothing.
        """
        if self._capture_container is None or self._capture_target is None:
            return
        if width is None:
            width = self._zone_width()
        if self.capture_zone_layout(width) != self._capture_layout:
            self._render_capture_zone()

    def _zone_width(self) -> float:
        try:
            return float(getattr(self._capture_container, "computed_width", 0) or 0)
        except Exception:  # noqa: BLE001 - a width that cannot be read is the fallback
            return 0.0

    def _render_capture_zone_now(self) -> None:
        """The frame, what it will send, and the button that sends it.

        Top to bottom: the viewfinder (or its strip), the Capture button, the
        offer to keep the view as a camera, which field a capture goes into
        when there is more than one, and a rule under all of it.

        Empty for a tool with no image field. There is nothing to capture into,
        and a frame that looked live but could not be used would be the most
        prominent dead control on the tab.
        """
        container = self._capture_container
        if container is None:
            return
        container.clear()
        self._viewfinders = {}
        key = self._capture_target
        if key is None or key not in self.image_field_keys:
            self._capture_layout = None
            self._zone_rendered()
            return

        slots = self.image_slots.get(key) or []
        plural = key in self._plural_keys
        collapsed, height = self.capture_zone_layout(self._zone_width())
        self._capture_layout = (collapsed, height)
        sources = self._resolve_sources(key)

        with container:
            with ui.VStack(spacing=6, height=0):
                if collapsed:
                    self._build_viewfinder_strip(key, slots, sources)
                else:
                    self._build_viewfinder(key, slots, sources, height)
                    # The button names its consequence rather than its
                    # mechanism: which image this becomes is the thing the
                    # user is actually deciding.
                    ui.Button(
                        self._capture_label(slots, plural),
                        height=26,
                        style=_STYLE_CAPTURE_BUTTON,
                        clicked_fn=lambda k=key: self._capture_into(k),
                    )
                    self._build_zone_options(key, slots)
                self._build_capture_target()
                ui.Rectangle(height=1, style={"background_color": _SEPARATOR})
        self._zone_rendered()

    def _zone_rendered(self) -> None:
        if self._on_capture_zone_rendered is None:
            return
        try:
            self._on_capture_zone_rendered()
        except Exception:  # noqa: BLE001 - a listener must not take the zone down
            logger.exception("RunDiffusion: capture-zone listener failed.")

    def _band_message(self, key: str, slots: list) -> tuple[str, bool]:
        """(what the frame's band says, whether it is a failure).

        One line for three states, because they answer the same question at
        different moments: a capture that is running, a capture that refused,
        and otherwise what the field will send. A capture that starts clears
        the last refusal, and one that refuses has already stopped running, so
        the first two are never both true.

        This is the only place a capture reports itself. It used to go to the
        status line at the foot of the panel as well, which is off by default
        and a long way from the frame the person is looking at.
        """
        if self._capture_busy.get(key):
            return CAPTURING_TEXT, False
        message = self._capture_error.get(key)
        if message:
            return message, True
        return self._image_caption(slots), False

    def _build_viewfinder(self, key: str, slots: list, sources: list, height: int) -> None:
        """The live view, full width, with its state written on it.

        Everything a person needs to know about the next capture sits ON the
        frame: where it is looking (top left), whether it is live (top right),
        what will be sent and the shape (the bottom band). That is what let
        three lines of help text and a separate aspect note go.

        The whole frame is a Capture button. An invisible button covers the
        picture UNDER the chips, so the source dropdown still takes its own
        clicks: omni.ui routes a click to the topmost widget that handles one,
        and a label, a rectangle or a spacer does not handle one.
        """
        with ui.ZStack(height=height):
            ui.Rectangle(style={"background_color": _CELL_EMPTY})
            frame = ui.Frame()
            self._viewfinders[key] = frame
            # Below the chip row, never under it. A button covering the
            # dropdown's rectangle took the dropdown's clicks in Kit, so the
            # camera could not be chosen at all. Reported from Base Editor.
            with ui.VStack():
                ui.Spacer(height=6 + CHIP_HEIGHT + 4)
                ui.InvisibleButton(clicked_fn=lambda k=key: self._frame_clicked(k))
            with ui.VStack():
                ui.Spacer(height=6)
                with ui.HStack(height=CHIP_HEIGHT):
                    ui.Spacer(width=6)
                    self._build_capture_source(key, sources)
                    ui.Spacer()
                    self._build_live_chip(key)
                    ui.Spacer(width=6)
                ui.Spacer()
                self._build_band(key, slots)

        if self._on_render_viewfinder is not None:
            self._on_render_viewfinder(key, frame)

    def _build_live_chip(self, key: str) -> None:
        """LIVE for the active viewport, Still for a named camera's frame."""
        live = self.capture_source(key) is None
        with ui.ZStack(width=0, height=CHIP_HEIGHT):
            ui.Rectangle(style={"background_color": _CHIP_PLATE, "border_radius": 3})
            with ui.HStack(width=0, spacing=5):
                ui.Spacer(width=7)
                if live:
                    # A shape rather than a bullet character, which is not a
                    # glyph every host font is guaranteed to carry.
                    with ui.VStack(width=6):
                        ui.Spacer()
                        ui.Circle(
                            width=6,
                            height=6,
                            style={"background_color": _AMETHYST_BRIGHT},
                        )
                        ui.Spacer()
                ui.Label(
                    "LIVE" if live else "Still",
                    width=0,
                    style={"font_size": _SIZE_CAPS, "color": _TEXT_EMPHASIS},
                )
                ui.Spacer(width=7)

    def _build_band(self, key: str, slots: list) -> None:
        """The darkened strip along the bottom of the frame, and its words."""
        text, failed = self._band_message(key, slots)
        # The shape the capture will take: the size picked by hand where there
        # is one, otherwise the viewport's.
        shape = self.capture_aspect()
        if shape is None and self._viewfinder_aspect is not None:
            shape = self._viewfinder_aspect()
        aspect = aspect_label(shape)
        with ui.ZStack(height=BAND_HEIGHT):
            ui.Rectangle(
                style={
                    "background_color": _BAND_TOP,
                    "background_gradient_color": _BAND_BOTTOM,
                }
            )
            with ui.VStack():
                ui.Spacer()
                with ui.HStack(height=0, spacing=6):
                    ui.Spacer(width=8)
                    ui.Label(
                        text,
                        word_wrap=True,
                        height=0,
                        style={
                            "font_size": _SIZE_BODY,
                            "color": _DANGER if failed else _TEXT_EMPHASIS,
                        },
                    )
                    ui.Label(
                        aspect,
                        width=0,
                        height=0,
                        alignment=ui.Alignment.RIGHT_BOTTOM,
                        style={"font_size": _SIZE_BODY, "color": _TEXT_BODY},
                    )
                    ui.Spacer(width=8)
                ui.Spacer(height=6)

    def _build_viewfinder_strip(self, key: str, slots: list, sources: list) -> None:
        """The frame, folded to one line once it has done its job.

        After a capture the useful things are the capture itself, where it came
        from, and doing it again. A 205px live frame of a view that is already
        pinned is room the runs and results under the form need more.
        """
        captures = [(i, slot) for i, slot in enumerate(slots) if slot.is_capture]
        latest = captures[-1] if captures else None
        text, failed = self._band_message(key, slots)
        if not failed and not self._capture_busy.get(key):
            source = self._source_label(key, sources)
            text = (
                f"{source} · captured as {latest[0] + 1}"
                if latest is not None
                else f"{source} · nothing captured yet"
            )

        with ui.HStack(height=STRIP_HEIGHT, spacing=8):
            with ui.VStack(width=STRIP_THUMB_WIDTH):
                ui.Spacer()
                with ui.ZStack(width=STRIP_THUMB_WIDTH, height=STRIP_THUMB_HEIGHT):
                    ui.Rectangle(style={"background_color": _CELL_EMPTY})
                    thumb = ui.Frame()
                ui.Spacer()
            with ui.VStack(spacing=2):
                ui.Spacer()
                ui.Label(
                    text,
                    height=0,
                    word_wrap=True,
                    style={
                        "font_size": _SIZE_BODY,
                        "color": _DANGER if failed else _TEXT_BODY,
                    },
                )
                with ui.HStack(height=16, spacing=4):
                    ui.Button(
                        "Recapture" if latest is not None else "Capture",
                        width=0,
                        style=_STYLE_TEXT_BUTTON,
                        clicked_fn=lambda k=key: self._capture_into(k),
                    )
                    ui.Label("·", width=0, style=_STYLE_META)
                    ui.Button(
                        "Show viewfinder",
                        width=0,
                        style=_STYLE_TEXT_BUTTON,
                        clicked_fn=lambda k=key: self.show_viewfinder(k),
                    )
                    ui.Spacer()
                ui.Spacer()

        if latest is not None and self._on_render_preview is not None:
            self._on_render_preview(key, latest[1], thumb)

    def release_capture_zone(self) -> None:
        """Let go of the zone's widgets, because the tab holding them is going.

        A destroyed frame still answers, so the references are dropped rather
        than left pointing at one. Every renderer treats a missing container as
        "not on screen" and does nothing.
        """
        self._capture_container = None
        self._capture_layout = None
        self._viewfinders = {}

    def _frame_clicked(self, key: str) -> None:
        """A click on the picture captures, unless it is the end of choosing a
        camera: the dropdown's list opens over the frame, and the click that
        picks a row must not also take a photograph."""
        if time.monotonic() - self._source_changed_at < SOURCE_CLICK_GRACE_SECONDS:
            return
        self._capture_into(key)

    def _would_fold(self, key: str) -> bool:
        """Whether this field's frame folds to the strip when nobody asks to see it."""
        slots = self.image_slots.get(key) or []
        return viewfinder_height(self._zone_width()) is None or any(
            slot.is_capture for slot in slots
        )

    def _build_zone_options(self, key: str, slots: list) -> None:
        """The line under Capture: Keep this view as a camera, and Hide viewfinder.

        Hide is offered only where Show viewfinder was pressed and hiding would
        actually fold the frame. Without it, a frame opened once stayed open
        for the session with no way back to the strip. Reported from Base
        Editor.
        """
        offer_pin = self._capture_source.get(key) is None
        offer_hide = key in self._viewfinder_opened and self._would_fold(key)
        if not offer_pin and not offer_hide:
            return
        with ui.HStack(height=20, spacing=6):
            if offer_pin:
                self._build_pin_view(key)
            ui.Spacer()
            if offer_hide:
                ui.Button(
                    "Hide viewfinder",
                    width=0,
                    style=_STYLE_TEXT_BUTTON,
                    clicked_fn=lambda k=key: self.hide_viewfinder(k),
                )

    def hide_viewfinder(self, key: str) -> None:
        """Fold the frame back to its strip, undoing Show viewfinder."""
        self._viewfinder_opened.discard(key)
        self._render_capture_zone()

    def show_viewfinder(self, key: str) -> None:
        """Put the full frame back for this field, for the rest of the session."""
        self._viewfinder_opened.add(key)
        self._render_capture_zone()

    def viewfinder_is_live(self) -> bool:
        """Whether a live picture of the active viewport is on screen.

        What the panel asks before paying for a readback every time the camera
        settles. A strip shows a pinned capture and a named camera shows a
        still, so neither of those is a reason to keep reading the viewport.
        """
        key = self._capture_target
        return key in self._viewfinders and self.capture_source(key) is None

    def _build_capture_target(self) -> None:
        """Which image field the frame captures into, when there is a choice.

        Almost every tool has one image field and never shows this. A tool
        with a start frame and an end frame has two, and each can be pointed
        at a different camera: the frame shows the chosen field's source, and
        switching the field switches what the frame is looking through.
        """
        keys = list(self.image_field_keys)
        if len(keys) < 2:
            return
        index = keys.index(self._capture_target) if self._capture_target in keys else 0
        with ui.HStack(height=24, spacing=6):
            ui.Label("CAPTURE INTO", width=90, style=_STYLE_CAPS)
            combo = ui.ComboBox(
                index, *[self.image_field_labels.get(key, key) for key in keys]
            )

        def chosen(model, *_args, field_keys=keys) -> None:
            position = model.get_item_value_model().as_int
            if 0 <= position < len(field_keys):
                self.set_capture_target(field_keys[position])

        combo.model.add_item_changed_fn(chosen)

    def set_capture_target(self, key: str) -> None:
        """Point the frame and the Capture button at another image field."""
        if key == self._capture_target or key not in self.image_field_keys:
            return
        self._capture_target = key
        self._render_capture_zone()

    @property
    def capture_target(self) -> str | None:
        return self._capture_target

    def _build_pin_view(self, key: str) -> None:
        """Offer to keep the framing, for a capture that would otherwise lose it.

        Only under a field pointed at the ACTIVE VIEWPORT, because that is the
        only capture with a framing to lose. A field set to a camera the user
        authored is already reproducible: the camera is in their outliner under
        their own name, and pinning a second copy of it beside the first would
        be clutter offered as a feature.

        Off every time the row is built rather than remembered across tools.
        Writing a prim onto someone's stage is not something to inherit from a
        box that was ticked twenty minutes ago for a different tool.
        """
        if self._capture_source.get(key) is not None:
            return
        with ui.HStack(width=0, height=20, spacing=6):
            box = ui.CheckBox(width=20)
            box.model.set_value(bool(self._pin_view.get(key)))
            box.model.add_value_changed_fn(
                lambda model, k=key: self._pin_view.__setitem__(
                    k, model.get_value_as_bool()
                )
            )
            # Says what it leaves behind, not what it switches on. The cost of
            # ticking it is a camera prim on their stage, so that is the part
            # worth reading before the box is ticked rather than after.
            ui.Label(
                "Keep this view as a camera",
                width=0,
                style={"font_size": _SIZE_BODY, "color": _TEXT_BODY},
            )

    def pin_view(self, key: str) -> bool:
        """Whether a capture from this field should pin its framing."""
        return bool(self._pin_view.get(key))

    def _resolve_sources(self, key: str) -> list:
        """The capture sources on offer, with this field's choice checked.

        Read once per zone build and handed to everything that draws from it,
        because reading them walks the stage (the panel caches it, but not for
        long). The check is what puts a field whose camera has been deleted back
        on the active viewport.

        The choice is recorded here even when there is nothing to choose, and
        that is the whole reason it happens before any widget is drawn:
        deleting the one camera a field was set to takes the dropdown away with
        it, and a field left remembering a camera that no longer exists, with
        no control left to change it, can never capture again. Reported from
        Base Editor.
        """
        sources = self._capture_sources() if self._capture_sources is not None else []
        values = [value for _label, value in sources]
        self._reset_missing_source(key, values)
        if len(sources) < 2:
            self._capture_source[key] = values[0] if values else None
        return sources

    def _source_label(self, key: str, sources: list) -> str:
        """What the field's chosen source is called, for a sentence."""
        chosen = self._capture_source.get(key)
        for label, value in sources:
            if value == chosen:
                return label
        return "Active viewport"

    def _build_capture_source(self, key: str, sources: list | None = None) -> None:
        """Where this field's next capture comes from, as a chip on the frame.

        On the frame rather than beside a button, because it describes what the
        frame is showing as much as it describes what the button will take.

        With only the active viewport to offer there is nothing to choose, so
        this says what will be captured instead of asking. A dropdown holding
        one row is a control that looks like a decision and is not one, and a
        stage with no authored cameras is the common case in Base Editor.

        The chosen index is read back through the model on change rather than
        being mirrored on every frame: the zone is rebuilt whenever an image is
        added or removed, and the value it resolves to on each rebuild is
        recorded at the bottom of this method.
        """
        if sources is None:
            sources = self._resolve_sources(key)
        values = [value for _label, value in sources]

        if len(sources) < 2:
            with ui.ZStack(width=0, height=CHIP_HEIGHT):
                ui.Rectangle(style={"background_color": _CHIP_PLATE, "border_radius": 3})
                with ui.HStack(width=0):
                    ui.Spacer(width=8)
                    ui.Label(
                        sources[0][0] if sources else "Active viewport",
                        width=0,
                        style={"font_size": _SIZE_BODY, "color": _TEXT_EMPHASIS},
                    )
                    ui.Spacer(width=8)
            return

        chosen = self._capture_source.get(key)
        index = values.index(chosen) if chosen in values else 0
        widget = ui.ComboBox(
            index,
            *[label for label, _value in sources],
            width=SOURCE_CHIP_WIDTH,
            height=CHIP_HEIGHT,
            style={
                "background_color": _CHIP_PLATE,
                "secondary_color": _CHIP_PLATE,
                "color": _TEXT_EMPHASIS,
                "font_size": _SIZE_BODY,
                "border_radius": 3,
            },
        )

        def remember(*_args, source_values=values, field=key, box=widget) -> None:
            position = box.model.get_item_value_model().as_int
            self._source_changed_at = time.monotonic()
            self._capture_source[field] = (
                source_values[position] if 0 <= position < len(source_values) else None
            )
            # A different source is a different question, so whatever the
            # last one refused for stops applying.
            self.clear_capture_error(field)
            # The whole zone, not just the picture: the LIVE chip and the
            # offer to keep the view as a camera both depend on which source is
            # chosen. Redrawing only the picture once left that checkbox on
            # screen under a field that had moved to a camera. Reported from
            # Base Editor.
            #
            # Next frame, though. This runs from the ComboBox model's
            # item-changed callback, and rebuilding clears the container the
            # dropdown is sitting in, which omni.ui refuses during dispatch (see
            # redraw.py).
            self._redraws.request(
                f"images:{field}", lambda f=field: self._render_image_field(f)
            )

        widget.model.add_item_changed_fn(remember)
        # Recorded now as well as on change. A rebuilt zone starts on whatever
        # index the value resolved to, and a source held only in the widget
        # would be forgotten by a caller asking before the user touches it.
        self._capture_source[key] = values[index] if values else None

    def _redraw_viewfinder(self, key: str) -> None:
        """Draw one viewfinder again, if it is still on screen.

        Guarded rather than assumed: a booked redraw can arrive after the row
        it was booked for has been rebuilt for another reason, or after the
        tool has been switched out from under it.
        """
        if self._on_render_viewfinder is None:
            return
        frame = self._viewfinders.get(key)
        if frame is not None:
            self._on_render_viewfinder(key, frame)

    def capture_source(self, key: str) -> Any:
        """The capture source chosen for `key`. None means the active viewport."""
        return self._capture_source.get(key)

    def export_capture_sources(self) -> dict[str, Any]:
        """Which source each image field points at, so a rebuild can restore it.

        `build` clears these, and it is right to: a camera chosen for one
        tool's field says nothing about another tool's. But the Create tab is
        rebuilt for reasons that are not a tool change at all: switching to the
        Library and back, using an image from a grid, reusing a past run's
        inputs. Across those the choice has to come back, or a field set to
        /World/Cameras/Lobby quietly returns to the active viewport and the
        next capture records whatever the user happens to be orbiting. The same
        journey `export_image_state` makes, for the same reason.

        Only fields actually pointed somewhere. None IS the default, so
        carrying it would be carrying nothing.
        """
        return {
            key: self._capture_source[key]
            for key in self.image_field_keys
            if self._capture_source.get(key) is not None
        }

    def restore_capture_sources(self, state: dict[str, Any] | None) -> None:
        """Put them back, keeping only fields this tool still has.

        Not checked against the stage here. The row redraws off the back of
        this and `_build_capture_source` validates on the way past, so a camera
        deleted while the user was on another tab puts the field back on the
        active viewport and says so through the one path that already knows how
        to say it.
        """
        if not state:
            return
        for key, source in state.items():
            if key not in self.image_field_keys or source is None:
                continue
            self._capture_source[key] = source
            self._render_image_field(key)

    def _build_image_header(self, key: str, slots: list, selected) -> None:
        """The row's title line, which doubles as the controls for one image.

        With nothing selected it names the field and states the ceiling up
        front: `N of 12`, counted across the whole run because that is what
        the server counts. Saying it before the thirteenth image is kinder than
        refusing the thirteenth.

        Select a thumbnail and the same line becomes View, move-left,
        move-right and Remove, so reordering costs no extra vertical space and
        a stray click cannot delete a capture the way a per-thumbnail X could.
        """
        label = self.image_field_labels.get(key, "Images").upper()
        if key not in self._required_keys:
            label = f"{label} · OPTIONAL"
        with ui.HStack(height=20, spacing=6):
            if selected is None:
                ui.Label(label, style=_STYLE_CAPS)
                ui.Label(
                    f"{self.total_images()} of {MAX_INPUT_FILES}",
                    width=0,
                    alignment=ui.Alignment.RIGHT_CENTER,
                    style=_STYLE_META,
                )
                return

            position = next(
                (i for i, slot in enumerate(slots) if slot.id == selected), 0
            )
            ui.Label(
                f"{position + 1} selected",
                style={"font_size": _SIZE_BODY, "color": _TEXT_EMPHASIS},
            )
            # A pinned image was previously only ever seen at 58px. That is
            # enough to tell two apart and not enough to tell whether a capture
            # came out the way it was meant to, which is exactly what someone
            # checks after framing a shot. Reported from Base Editor while
            # confirming a path-traced capture: the thumbnail was the only way
            # to judge it.
            ui.Button(
                "View",
                width=46,
                style=_STYLE_QUIET_BUTTON,
                clicked_fn=lambda k=key, i=selected: self._view_slot(k, i),
            )
            # Disabled at the ends rather than wrapping, so the buttons
            # themselves say where in the order you are.
            left = ui.Button(
                "<",
                width=22,
                style=_STYLE_QUIET_BUTTON,
                clicked_fn=lambda k=key, i=selected: self._move_slot(k, i, -1),
            )
            left.enabled = position > 0
            right = ui.Button(
                ">",
                width=22,
                style=_STYLE_QUIET_BUTTON,
                clicked_fn=lambda k=key, i=selected: self._move_slot(k, i, 1),
            )
            right.enabled = position < len(slots) - 1
            ui.Button(
                "Remove",
                width=60,
                style=_STYLE_QUIET_BUTTON,
                clicked_fn=lambda k=key, i=selected: self.remove_slot(k, i),
            )

    def _capture_label(self, slots: list, plural: bool) -> str:
        """What capturing would do, said as the result rather than the action."""
        if not plural and slots:
            return "Capture view · replaces"
        return f"Capture view · add as {len(slots) + 1}"

    def _image_caption(self, slots: list) -> str:
        """What this field is sending, said plainly."""
        if not slots:
            return "Nothing sent yet"
        if len(slots) == 1:
            return "1 will be sent"
        return f"{len(slots)} will be sent"

    def _build_slot(self, key: str, slot: ImageSlot, position: int, selected: bool) -> None:
        """One pinned image, clickable, with its send position on it.

        Selected is an amethyst outline, the same mark a selected result
        carries, so the two strips on the tab agree about what selection looks
        like.
        """
        with ui.ZStack(width=PREVIEW_SIZE, height=PREVIEW_SIZE):
            ui.Rectangle(style={"background_color": _CELL_EMPTY})
            preview = ui.Frame(width=PREVIEW_SIZE, height=PREVIEW_SIZE)
            if selected:
                ui.Rectangle(
                    style={
                        "background_color": 0x00000000,
                        "border_color": _AMETHYST,
                        "border_width": 2,
                    }
                )
            # An invisible button over the whole tile, so selecting is a click
            # anywhere on the image rather than on a small control.
            ui.InvisibleButton(
                clicked_fn=lambda k=key, i=slot.id: self._select_slot(k, i)
            )
            with ui.VStack():
                ui.Spacer()
                with ui.HStack(height=14):
                    ui.Spacer(width=3)
                    with ui.ZStack(width=14, height=14):
                        ui.Rectangle(
                            style={"background_color": _CHIP_PLATE, "border_radius": 2}
                        )
                        ui.Label(
                            str(position + 1),
                            alignment=ui.Alignment.CENTER,
                            style={"font_size": _SIZE_META, "color": _TEXT_EMPHASIS},
                        )
                    ui.Spacer()
                ui.Spacer(height=3)
        if self._on_render_preview is not None:
            self._on_render_preview(key, slot, preview)

    def _view_slot(self, key: str, slot_id: int) -> None:
        """Show the selected image at a size worth looking at."""
        if self._on_view_image is None:
            return
        slot = next(
            (s for s in self.image_slots.get(key) or [] if s.id == slot_id), None
        )
        if slot is not None:
            self._on_view_image(key, slot)

    def _select_slot(self, key: str, slot_id: int) -> None:
        """Click a thumbnail to select it; click it again to deselect."""
        if self._selected_slot.get(key) == slot_id:
            self._selected_slot.pop(key, None)
        else:
            self._selected_slot[key] = slot_id
        self._render_image_field(key)

    def _move_slot(self, key: str, slot_id: int, delta: int) -> None:
        """Move one image along the send order.

        The ordinals renumber as it moves, which is the feedback that the ORDER
        changed rather than just the arrangement on screen: a tool handed
        several reference images does not treat them interchangeably.
        """
        slots = self.image_slots.get(key) or []
        position = next((i for i, slot in enumerate(slots) if slot.id == slot_id), None)
        if position is None:
            return
        target = position + delta
        if not 0 <= target < len(slots):
            return
        slots[position], slots[target] = slots[target], slots[position]
        self._render_image_field(key)

    def _pick_into(self, key: str) -> None:
        if self._on_pick_image is not None:
            self._on_pick_image(key)

    # -- image slots -------------------------------------------------------

    def _reset_missing_source(self, key: str, values: list) -> None:
        """Put a field back on the active viewport when its camera has gone.

        Silently changing what a button will do is normally the wrong thing.
        Here the alternative is worse: the camera is not coming back, every
        capture from it is refused, and where it was the only one the control
        for changing it has gone too. So the field is moved and TOLD, which is
        what the message is for.

        Written straight into `_capture_error` rather than through
        `show_capture_error`, which would book a rebuild of the row currently
        being built. The error line is drawn after this in the same pass, so it
        picks the message up without one.
        """
        chosen = self._capture_source.get(key)
        if chosen is None or chosen in values:
            return
        self._capture_source[key] = values[0] if values else None
        # The prim path, which is what `chosen` already is. Looking a friendly
        # label up in the source list was dead code: the line above only runs
        # when `chosen` is NOT among those sources, so the lookup could never
        # match and always fell back to the path. It reads the way a refused
        # capture reads, which names the path too.
        self._capture_error[key] = (
            f"{chosen} is no longer on the stage, so this field is back on the "
            "active viewport."
        )

    def show_capture_busy(self, key: str, message: str) -> None:
        """Say that a capture is under way, under the button that asked for it.

        The status line at the foot of the panel is off by default, so for most
        people this is the only sign the click landed at all. Every capture now
        settles for sixty frames before it reads anything, which on a heavy
        path-traced stage is seconds of a panel that otherwise says nothing.
        """
        self._capture_running[key] = self._capture_running.get(key, 0) + 1
        if self._capture_busy.get(key) == message:
            # Already saying exactly this. The count above is what remembers
            # that there is now more than one to wait for.
            return
        self._capture_busy[key] = message
        self._render_image_field(key)

    def clear_capture_busy(self, key: str) -> None:
        """One capture finished. Drop the line once the LAST one has.

        Symmetrical with `show_capture_busy` on purpose: whoever says a capture
        started says it stopped, and the line goes when the count reaches
        nothing rather than when the first of them lands.
        """
        remaining = max(0, self._capture_running.get(key, 0) - 1)
        if remaining:
            self._capture_running[key] = remaining
            return
        self._capture_running.pop(key, None)
        if self._capture_busy.pop(key, None) is not None:
            self._render_image_field(key)

    def capture_busy(self, key: str) -> str | None:
        """What this field is reporting in progress, if anything."""
        return self._capture_busy.get(key)

    def show_capture_error(self, key: str, message: str) -> None:
        """Say why a capture did not happen, under the button that asked."""
        if self._capture_error.get(key) == message:
            return
        self._capture_error[key] = message
        self._render_image_field(key)

    def clear_capture_error(self, key: str) -> None:
        """Drop the message, because whatever it reported no longer holds."""
        if self._capture_error.pop(key, None) is not None:
            self._render_image_field(key)

    def capture_error(self, key: str) -> str | None:
        """What this field is currently reporting, if anything."""
        return self._capture_error.get(key)

    def _capture_into(self, key: str) -> None:
        # The source is read HERE, on the click, and handed over with the
        # request. The panel must not go back and ask the form later: a capture
        # is asynchronous, and the row can be rebuilt underneath it.
        if self._on_capture is not None:
            self._on_capture(key, self.capture_source(key))

    def add_capture(
        self,
        key: str,
        png: bytes,
        thumbnail: Any = None,
        description: str = "",
        camera_path: str | None = None,
    ) -> int:
        """Pin a captured view into a field. Returns the new slot's id.

        Called by the panel once the capture has actually landed, so a slot only
        ever exists for an image that exists.

        `description` names where the view was taken from. It travels with the
        slot into the exported image state and therefore into the run, which is
        what lets a render made from `/World/Cameras/Lobby` still say so an hour
        later when the form has moved on. Empty for a capture that has nothing
        more specific to say than "the viewport".
        """
        slot = ImageSlot(
            self._next_slot_id(),
            png=png,
            thumbnail=thumbnail,
            description=description,
            camera_path=camera_path,
        )
        return slot.id if self._add(key, slot) else None

    def total_images(self) -> int:
        """Every image pinned across the whole form.

        What the limit is actually about: the request carries all of them, and
        the server counts them together.
        """
        return sum(len(slots) for slots in self.image_slots.values())

    def has_room(self, key: str) -> bool:
        """Whether another image can go in this field.

        Asked before capturing rather than after, so a refused image is not one
        the user has already watched being taken.
        """
        if self._replaces(key):
            # A singular field that already holds one swaps it, so the request
            # carries the same number of files either way. Refusing that at the
            # limit would leave a field that cannot be corrected, only emptied.
            return True
        return self.total_images() < MAX_INPUT_FILES

    def _replaces(self, key: str) -> bool:
        return key not in self._plural_keys and bool(self.image_slots.get(key))

    def add_chosen(self, key: str, reference: Any, description: str = "") -> int | None:
        """Add an image the server already holds. Returns the new slot's id.

        The id is what the panel keys the asset by, because a slot's POSITION
        moves whenever an earlier one is removed, and a preview keyed by
        position would then be showing its neighbour's picture.

        None when the form is full. An id was returned unconditionally before,
        so a refused image was still recorded against a slot that does not
        exist: an entry in `_chosen_assets` that nothing ever draws, reads or
        clears, and no sign to the user that the pick did not take.
        """
        slot = ImageSlot(
            self._next_slot_id(), reference=reference, description=description
        )
        return slot.id if self._add(key, slot) else None

    def _belongs_here(self, key: str) -> bool:
        """Whether `key` is a field of the tool currently on screen.

        Asked because a capture outlives the form that asked for it. A settle
        of sixty frames plus a readback is a second or more, the Capture button
        is not disabled while it runs, and switching tool in the meantime calls
        `build`, which empties `image_slots`. The capture then lands and
        `setdefault` cheerfully recreates the old tool's key.

        Nothing draws or sends that slot, because everything else iterates
        `image_field_keys`. But `total_images` sums the whole dict, so the
        orphan silently eats one of the twelve images the next run is allowed.
        """
        return key in self.image_field_keys

    def _add(self, key: str, slot: ImageSlot) -> bool:
        """True when the slot was taken. False when the form cannot take it."""
        if not self._belongs_here(key):
            logger.info(
                "RunDiffusion: dropped an image for %s, which the tool now on "
                "screen does not have.",
                key,
            )
            return False
        slots = self.image_slots.setdefault(key, [])
        if not self._replaces(key) and self.total_images() >= MAX_INPUT_FILES:
            # Counted across the form, not within the field. Refusing here is
            # kinder than a 413 after the bytes have gone up.
            logger.info(
                "RunDiffusion: the form already holds %s images, which is what "
                "a request can carry.",
                MAX_INPUT_FILES,
            )
            return False
        if key not in self._plural_keys:
            # A singular field takes one image, so a second choice replaces the
            # first rather than being dropped silently at submit time.
            slots.clear()
        slots.append(slot)
        self._render_image_field(key)
        return True

    def remove_slot(self, key: str, slot_id: int) -> None:
        slots = self.image_slots.get(key) or []
        self.image_slots[key] = [slot for slot in slots if slot.id != slot_id]
        self._render_image_field(key)

    def clear_image(self, key: str) -> None:
        """Send nothing for this field, so the tool applies its own behaviour."""
        self.image_slots[key] = []
        self._render_image_field(key)

    def image_selections(self) -> dict[str, list[ImageSelection]]:
        """What each image field will send, in order, for the request builder."""
        return {
            key: [
                ImageSelection(
                    reference=slot.reference,
                    png=slot.png,
                    description=slot.description,
                )
                for slot in self.image_slots.get(key) or []
            ]
            for key in self.image_field_keys
            if self.image_slots.get(key)
        }

    def redraw(self, key: str) -> None:
        """Redraw one image field, for a caller that has changed what it shows."""
        self._render_image_field(key)

    def export_image_state(self) -> dict[str, Any]:
        """The image choices, in a form that survives the widgets being torn down.

        The Create tab is rebuilt on every tab switch, which destroys the
        controls. Typed values carry over through `collect`; these do not live in
        a control at all, so they need their own way across.
        """
        return {
            key: [
                (
                    slot.id,
                    slot.reference,
                    slot.description,
                    slot.png,
                    slot.thumbnail,
                    slot.camera_path,
                )
                for slot in self.image_slots.get(key) or []
            ]
            for key in self.image_field_keys
        }

    def restore_image_state(self, state: dict[str, Any] | None) -> None:
        """Put exported choices back, keeping only fields this tool still has."""
        if not state:
            return
        for key, slots in state.items():
            if key not in self.image_field_keys:
                continue
            self.image_slots[key] = [
                ImageSlot(slot_id, reference, description, png, thumbnail, camera_path)
                for slot_id, reference, description, png, thumbnail, camera_path in slots
            ]
            # Ids come from one counter, so a restored slot must not be able to
            # collide with one added after the restore.
            for slot in slots:
                self._slot_counter = max(self._slot_counter, slot[0])
            self._render_image_field(key)

    def refresh_viewfinders(self) -> None:
        """Redraw the live viewfinders. Called when the camera settles.

        Only the viewfinders: the captured images are pinned bytes that cannot
        have changed, and redrawing a whole field would refetch every library
        thumbnail in it each time the camera moves.
        """
        if self._on_render_viewfinder is None:
            return
        for key, frame in self._viewfinders.items():
            self._on_render_viewfinder(key, frame)

    def _next_slot_id(self) -> int:
        self._slot_counter += 1
        return self._slot_counter

    # -- collection --------------------------------------------------------

    def collect(self) -> dict[str, Any]:
        """The values the user entered, keyed by fieldKey.

        Empty controls are omitted rather than sent as blanks, so a tool falls
        back to its own default instead of being handed an empty string.
        """
        values: dict[str, Any] = {}
        for control in self._controls:
            try:
                if control.is_empty():
                    continue
                value = control.value()
            except Exception:  # noqa: BLE001 - one bad control must not lose the form
                logger.exception("RunDiffusion: could not read field %s.", control.key)
                continue
            if value is not None:
                values[control.key] = value
        return values
