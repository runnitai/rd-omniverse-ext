"""The RunDiffusion window.

A tabbed workbench: **Create**, **Library**, **Uploads**, **Account**, with a
status line pinned under the tab body. Tabs
rather than one long scrolling column because the Create form scrolls and an
asset grid scrolls, and two scrolls nested inside each other is a worse panel
than four tabs. Docked and narrow, so nothing needs more than a phone's width.

omni.ui has no tab widget, so the bar is a RadioCollection of ToolButtons over a
content Frame that rebuilds on change. That is the documented idiom rather than
an invention.

Native toolkit rather than a WebView shell, for the reasons that decided the
other RunDiffusion plugins plus a stronger one here: Kit ships omni.ui as a
first-class Python UI toolkit, so a browser shell would add a Chromium
dependency and a bridge to cross for every capture and every token while
buying nothing.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
import uuid
import webbrowser
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import omni.ui as ui

from . import prefs
from . import redraw as redraw_module
from . import saving
from . import stage
from .api import assets as assets_api
from .api import generate as generate_api
from .api import kits as kits_api
from .api import constants
from .api import token_store, token_wall as wall_api, tools as tools_api
from .api import tool_tags as tool_tags_api
from .api import session as session_api
from .api.session import RdSession
from .api.transport import ApiError, download_async
from .asset_grid import AssetGrid
from .text import shorten
from .theme import (
    AMETHYST as _AMETHYST,
    AMETHYST_BRIGHT as _AMETHYST_BRIGHT,
    CELL as _RUNNING_CELL,
    CELL_EMPTY as _CELL_EMPTY,
    DANGER as _DANGER,
    HAIRLINE as _HAIRLINE,
    RUN_BAR as _RUN_BAR,
    RUN_TRACK as _RUN_TRACK,
    SEPARATOR as _SEPARATOR,
    SIZE_BODY as _SIZE_BODY,
    SIZE_TITLE as _SIZE_TITLE,
    STYLE_CAPS as _STYLE_CAPS,
    STYLE_META as _STYLE_META,
    STYLE_QUIET_BUTTON as _STYLE_QUIET_BUTTON,
    STYLE_SECONDARY as _STYLE_SECONDARY,
    STYLE_TEXT_BUTTON as _STYLE_TEXT_BUTTON,
    STYLE_WEIGHTED_BUTTON as _STYLE_WEIGHTED_BUTTON,
    TAB_INACTIVE as _TAB_INACTIVE,
    TAB_RULE as _TAB_RULE,
    TEXT_BODY as _TEXT_BODY,
    TEXT_EMPHASIS as _TEXT_EMPHASIS,
    TEXT_SECONDARY as _TEXT_SECONDARY,
    SIZE_CAPS as _SIZE_CAPS,
    SIZE_META as _SIZE_META,
    TRANSPARENT as _TRANSPARENT,
)
from .compare import CompareWindow, compare_sources, result_source_name
from .dialogs import AssetPicker, ImageViewer, ToolBrowser
from .form import (
    CAPTURING_TEXT,
    MAX_INPUT_FILES,
    PREVIEW_SIZE,
    ToolForm,
    cameras_in_image_state,
    prompt_text,
)
from .jobs import STATE_FAILED, JobQueue
from .viewport_capture import CaptureError, capture_png, captured_aspect, viewport_aspect
from .viewport_preview import ViewportPreview, crop_png, write_cropped, write_thumbnail
from .camera_stills import CameraStills
from . import cameras as cameras_module
from . import render_cameras

logger = logging.getLogger(__name__)

WINDOW_TITLE = "RunDiffusion"

#: Title bar of each browsing surface torn out into a window of its own. Named
#: for what it holds rather than for the extension, so a Kit window list with a
#: dozen entries in it stays readable.
SURFACE_WINDOW_TITLES = {
    "library": "RunDiffusion library",
    "uploads": "RunDiffusion uploads",
}

#: Shown on the Account tab and the sign-in screen. Kept in step with
#: config/extension.toml by the test suite, because a version that lies is worse
#: than no version at all.
EXTENSION_VERSION = "0.3.0"

#: Output kinds the panel keeps a local copy of. Everything here can be saved
#: and sent even where it cannot be drawn, which is the difference between a
#: result a person can still use and one that is simply lost.
#: Kinds POST /uploads accepts today: jpeg, png, webp, heic and heif. Sending
#: a video or a model is planned, and when that ships adding them here is
#: the whole of this plugin's change.
UPLOADABLE_KINDS = (generate_api.KIND_IMAGE,)

#: The first row of every capture-source dropdown, and what a capture was
#: before named cameras gave it anywhere else to come from. Its value is None
#: throughout: "no particular camera" rather than a path that means the same.
ACTIVE_VIEWPORT_LABEL = "Active viewport"

#: How long a list of the stage's cameras may be reused before it is read again.
#:
#: Listing them means `stage.Traverse()`, which walks EVERY prim on the stage.
#: On the architectural scenes this plugin exists for that is hundreds of
#: thousands of them, on the UI thread, and the image row asks for the list on
#: every rebuild: adding an image, removing one, selecting one, reordering, and
#: every visit to the Create tab. Ordinary clicking was paying for a full
#: traversal each time.
#:
#: Short, because this only has to collapse a BURST. What keeps the list
#: current is `cameras.StageWatch`, which throws this away the moment the stage
#: gains or loses a prim; the window is what stops one click that rebuilds a
#: row twice from walking the stage twice. It is not load-bearing for
#: correctness, and lowering it to zero would cost speed rather than accuracy.
CAMERA_LIST_CACHE_SECONDS = 0.5

#: Viewer buttons that end the look at a picture. Each one takes the user
#: somewhere else: another application, the stage, the Uploads tab, the compare
#: window, or the field the image was just put into. Leaving the viewer sitting
#: over wherever they were taken made "act" and "close" two clicks for one
#: intent, which is the arithmetic half of the problem.
#:
#: Save to computer is deliberately absent. It opens a dialog over the viewer
#: and the render is usually still worth looking at once it is written.
#:
#: Matched by label because that is what the viewer is handed. The labels
#: themselves are declared once in `_result_action_specs`, so a renamed button
#: that drops out of this set is caught by the test that pins the two together.
VIEWER_DISMISSING_ACTIONS = frozenset(
    {
        "Open in system viewer",
        "Compare",
        "Send to RunDiffusion",
        "Add to stage",
        "Use in Create",
    }
)

RECORDABLE_KINDS = (
    generate_api.KIND_IMAGE,
    generate_api.KIND_VIDEO,
    generate_api.KIND_ASSET_3D,
)

RESULT_THUMBNAIL = 62

#: The preview beside the Library and Uploads action row, so the buttons can
#: never be ambiguous about which image they act on.
ASSET_PREVIEW = 70

#: The label column in the filter panel. Fixed so the seven controls line up
#: into one column instead of stepping in and out with the length of their name.
FILTER_LABEL = 88

#: How many items a page of the library or of uploads asks for. The server caps
#: it here too, so asking for more is asking for this.
PAGE_SIZE = 24

#: This session's grid on the Library tab, deliberately the same cell and
#: column count as the library grid below it: they are two collections of the
#: same kind of thing and reading as one page is the point.
SESSION_CELL = 96
SESSION_COLUMNS = 3

#: How many rows of it are on screen before it scrolls. Two, because this frame
#: sits ABOVE the library filters and a session with thirty renders in it must
#: not push the library off the tab.
SESSION_VISIBLE_ROWS = 2

#: Where the extension's own image assets live. Inside the extension folder, so
#: they travel in the packaged zip: CI zips `exts/rundiffusion.omniverse` whole.
DATA_DIR = Path(__file__).resolve().parents[1] / "data"

#: The RunDiffusion mark, rastered from the design system's rd-mark-only.svg.
#:
#: The note that used to be here said omni.ui cannot draw an SVG at all. That
#: is not so: the SDK's own extensions hand `.svg` straight to `ui.Image` and
#: to `image_url` (omni.kit.property.transform does both), and
#: OPEN_IN_NEW_ICON below relies on it. Why THIS artwork is a PNG is not
#: recorded anywhere and is not worth guessing at; it works, and the reason to
#: revisit it would be wanting the mark at more than one size.
MARK_PATH = DATA_DIR / "rd-mark.png"
MARK_SIZE = 88

#: Material Design Icons' `open-in-new`, on every button that tears a browsing
#: surface out into a window of its own.
#:
#: The verb "pop out" is doing a lot of work in a four-word label, and this is
#: the picture every application in the room already uses for it: the same icon
#: sits on "open in new tab" in a browser and on every external-link affordance
#: on the web. Recognising it costs nobody a click.
#:
#: An SVG, unmodified from MDI, because omni.ui renders one: the SDK's own
#: extensions pass `.svg` straight to `image_url`, and an icon that scales is
#: worth more than one rastered for today's button height. Licensing and
#: provenance are in data/ICONS-LICENSE.md.
OPEN_IN_NEW_ICON = DATA_DIR / "open-in-new.svg"

#: Edge of that icon. Smaller than the 22px button it sits in, so it reads as a
#: mark beside the words rather than as a second control.
ICON_SIZE = 14

#: The icon takes the colour of the words it stands next to rather than the
#: white it is drawn in. omni.ui tints a button's image through the
#: "Button.Image" style, which is what lets one asset serve wherever it is used.
#:
#: Neutral grey, so the 0xAABBGGRR ordering that theme.py warns about cannot
#: bite: every channel is the same value.
#:
#: A plain int rather than the whole style dict, because the dict names
#: `ui.Direction` and this module is imported by tests that stub omni.ui down
#: to an empty module. Anything touching `ui.` at import time breaks them.
ICON_TINT = 0xFFCCCCCC


#: Type scale for the landing screen only. The rest of the panel inherits the
#: host's own sizes on purpose: a tool panel that restyles every label stops
#: looking like part of the app it is docked in.
_STYLE_WORDMARK = {"font_size": 26}
_STYLE_SUBTITLE = {"font_size": 13, "color": 0xFF9A9A9A}
_STYLE_TAGLINE = {"font_size": 15}
_STYLE_SUPPORT = {"font_size": 12, "color": 0xFFB4B4B4}
_STYLE_HELPER = {"font_size": 12, "color": 0xFF9A9A9A}


#: How much of a prompt fits on a 96px tile before it stops being readable.
PROMPT_PREVIEW_CHARS = 48

#: And in the viewer, which has a whole window's width for it.
PROMPT_CAPTION_CHARS = 220


_STYLE_PRIMARY = {
    "background_color": _AMETHYST,
    "background_color_hovered": _AMETHYST_BRIGHT,
    "color": 0xFFFFFFFF,
    "font_size": 15,
    "border_radius": 4,
}
_STYLE_SECONDARY = {
    "background_color": _TRANSPARENT,
    "border_color": _HAIRLINE,
    "border_width": 1,
    "color": 0xFFFFFFFF,
    "font_size": 15,
    "border_radius": 4,
}

#: The tab strip. Tabs that read as tabs rather than as four raised buttons
#: competing with the buttons that actually do something: no ground at all,
#: grey words, and the open tab in white over a rule of its own.
_STYLE_TAB = {
    "Button": {"background_color": _TRANSPARENT, "border_radius": 0, "margin": 0},
    "Button:hovered": {"background_color": _TRANSPARENT},
    "Button:pressed": {"background_color": _TRANSPARENT},
    "Button:checked": {"background_color": _TRANSPARENT},
    "Button.Label": {"font_size": 13, "color": _TAB_INACTIVE},
    "Button.Label:hovered": {"color": _TEXT_BODY},
    "Button.Label:checked": {"color": _TEXT_EMPHASIS},
}

#: The rule under the tab that is open, and the absence of one under the rest.
_TAB_RULE_HEIGHT = 2

#: The results strip holds this many square cells, whatever is in them.
RESULT_CELLS = 4

#: The x-offset of the indeterminate bar under a run moves this fraction of
#: its track per second, and the bar is this fraction of the track wide.
RUN_BAR_SPEED = 0.6
RUN_BAR_FRACTION = 0.3
RUN_BAR_HEIGHT = 3

#: Why Hide exists and what it does not do, on the button rather than as a
#: paragraph under the runs. Hiding is the rarer act; the sentence was being
#: read on every run by people who never pressed it.
HIDE_TOOLTIP = (
    "Hiding a run does not stop it. It keeps going, stays billed, and its "
    "renders arrive in This session on the Library tab."
)

#: How long the form has to stay still before the run is re-priced. Every
#: keystroke in a prompt is a change, and preview-cost is a network call.
COST_DEBOUNCE_SECONDS = 0.6


def _account_label(account) -> str:
    """What to call an account wherever one is named.

    The footer and the Account tab describe the same thing to the same person,
    and a team that is "Acme" in one place and a raw id in the other reads as
    two different accounts.
    """
    return account.label or ("Personal" if account.is_personal else account.id or "Team")


def _safe_stem(value: str) -> str:
    """A library id is `run:result`, and a colon is not a filename."""
    return "".join(character if character.isalnum() else "-" for character in value)

#: The two grids of server-side assets. They were one surface's worth of state
#: while they could never be on screen at once, which was true for as long as
#: both were tabs. Either can now be torn out into a window of its own, so a
#: click in one must not redraw the other's action row or steal its selection.
SURFACE_LIBRARY = "library"
SURFACE_UPLOADS = "uploads"

TAB_CREATE, TAB_LIBRARY, TAB_UPLOADS, TAB_ACCOUNT = range(4)
TAB_LABELS = ("Create", "Library", "Uploads", "Account")

#: The surfaces that can be torn out into a window of their own, in tab order.
#:
#: Both for the same reason. A browsing surface is where you go to FIND an
#: image, and what you do with the one you find is on the Create tab, so
#: choosing and using were two tabs apart with no way to put them side by side
#: on a monitor with the room for both. That was the ask for the library,
#: and it is the same sentence about uploads.
POPPABLE_SURFACES = (SURFACE_LIBRARY, SURFACE_UPLOADS)

#: Which tab each surface goes back into when it is docked.
SURFACE_TABS = {SURFACE_LIBRARY: TAB_LIBRARY, SURFACE_UPLOADS: TAB_UPLOADS}

#: What the tab says while its surface is in a window somewhere else.
#:
#: Whole sentences rather than a name dropped into one template: "the library
#: IS" and "your uploads ARE" do not share a verb, and bending the copy to fit
#: one string would read worse than keeping two.
SURFACE_ELSEWHERE = {
    SURFACE_LIBRARY: "The library is in its own window.",
    SURFACE_UPLOADS: "Your uploads are in their own window.",
}


class SessionResult:
    """One render made since Kit started.

    A plugin run never reaches the library, so without this list a render is
    unreachable the moment the inline gallery is replaced. It holds the request
    id and a local copy, never a stored URL: signed URLs expire in 7 days and
    the honest way to refresh one is to re-poll the run.
    """

    def __init__(
        self,
        request_id: str,
        path: Path,
        tool_name: str,
        tool_id: str | None = None,
        values: dict | None = None,
        image_state: dict | None = None,
        prompt: str = "",
        kind: str = generate_api.KIND_IMAGE,
        duration_seconds: float | None = None,
        width: int | None = None,
        height: int | None = None,
        size_bytes: int | None = None,
        mime_type: str | None = None,
        preview_path: Path | None = None,
    ) -> None:
        self.request_id = request_id
        self.path = path
        self.tool_name = tool_name
        #: IMAGE, VIDEO or ASSET_3D. What the file on `path` actually is, which
        #: decides what the panel can draw for it and what it offers to do with
        #: it. Defaulted so every existing caller keeps meaning an image.
        self.kind = kind
        #: Videos only. Shown on the tile, because a still frame gives a viewer
        #: no idea whether they are looking at three seconds or thirty.
        self.duration_seconds = duration_seconds
        #: What the run actually produced. The panel cannot draw a video or a
        #: model, so this is what it can say about one instead of nothing.
        self.width = width
        self.height = height
        self.size_bytes = size_bytes
        #: What the server said this file is. Sent on upload rather than a
        #: guess: the uploads endpoint checks the declared type, and every
        #: render was being declared image/png whatever it was.
        self.mime_type = mime_type
        #: A first-frame still on disk, for a result the panel cannot draw
        #: itself. None for an image (which IS its own picture), for a 3D asset
        #: (the server makes no still for one), and for a video whose
        #: frame could not be extracted. Every drawing site treats None as "use
        #: the tile", which is what the panel did for all of them before.
        self.preview_path = preview_path
        #: What this run was submitted with, so it can be put back on the form.
        #: Kept per result rather than read off the live form, because the form
        #: has usually moved on by the time anyone wants it back.
        self.tool_id = tool_id
        self.values = values
        self.image_state = image_state
        self.created = datetime.now()
        #: What was asked for. Carried on the result rather than dug back out of
        #: `values`, because finding it there needs the tool schema and the form
        #: has usually moved to another tool by the time anyone looks.
        self.prompt = prompt
        #: Taken off the Create tab by Clear finished. The render itself is
        #: untouched and stays in This session on the Library tab, which is
        #: where clearing sends you to find it.
        self.cleared = False

    @property
    def drawable_path(self) -> Path | None:
        """The file omni.ui can actually draw for this result, or None.

        An image is its own picture. A video has one only if the server made a
        first-frame still and it downloaded. A 3D asset never does: the server
        makes no still for one, deliberately, because Kit can import and render
        a `.glb` itself and a software-rasterised thumbnail would be the worse
        of the two pictures.

        None means "there is nothing to draw", which every drawing site already
        had a fallback for, since that was the case for every video and model
        before previews existed.
        """
        if self.kind == generate_api.KIND_IMAGE:
            return self.path
        return self.preview_path

    @property
    def kind_word(self) -> str:
        """What to call this in a caption."""
        if self.kind == generate_api.KIND_VIDEO:
            return "Video"
        if self.kind == generate_api.KIND_ASSET_3D:
            return "3D"
        return "File"

    @property
    def summary(self) -> str:
        """One line describing the file, for a result that cannot be drawn."""
        parts = []
        if self.width and self.height:
            parts.append(f"{self.width} x {self.height}")
        if self.duration_seconds:
            parts.append(f"{self.duration_seconds:.1f}s")
        if self.size_bytes:
            parts.append(f"{self.size_bytes / 1_000_000:.1f} MB")
        parts.append(self.path.suffix.lstrip(".").upper() or "file")
        return "  ".join(parts)


class RunDiffusionPanel:
    """Owns the window and everything in it for the life of the extension."""

    def __init__(self) -> None:
        self._session = RdSession()
        self._window = ui.Window(WINDOW_TITLE, width=420, height=720)

        # Shared chrome
        self._root: ui.Frame | None = None
        self._sign_in_frame: ui.Frame | None = None
        self._sign_in_state = "Not signed in."
        self._sign_in_intent = session_api.INTENT_SIGN_IN
        self._device_prompt: Any = None
        self._sign_in_task: Any = None
        self._wall_frame: ui.Frame | None = None
        self._status: ui.Label | None = None
        self._content: ui.Frame | None = None
        self._current_tab = TAB_CREATE

        # Create tab
        self._tool_frame: ui.Frame | None = None
        #: The control that says which tool is selected and opens the browser.
        #: A button rather than a ComboBox: see `_render_tool_picker`.
        self._tool_button: ui.Button | None = None
        #: Which tool is selected, by id. Held here rather than read back off
        #: the widget, because the widget is now a label and has no notion of
        #: a current index.
        self._selected_tool_id: str | None = None
        self._form_frame: ui.Frame | None = None
        self._form = ToolForm(
            on_pick_image=self._on_pick_image,
            on_render_preview=self._render_image_preview,
            on_capture=self._on_capture_clicked,
            on_render_viewfinder=self._render_viewfinder,
            capture_sources=self._capture_sources,
            on_view_image=self._view_image_slot,
            on_values_changed=self._schedule_cost_refresh,
            viewfinder_aspect=viewport_aspect,
            on_capture_zone_rendered=self._sync_viewport_watch,
        )
        #: The frame at the top of the Create tab that the form draws the
        #: viewfinder and the Capture button into.
        self._capture_frame: ui.Frame | None = None
        #: The viewfinder picture cropped to a chosen output size: which
        #: source and shape it was made from, and where it is.
        self._cropped_previews: dict[str, Any] = {}
        #: The rule under each tab, lit under the one that is open.
        self._tab_rules: list = []
        #: The indeterminate bars under runs still going, and the per-frame
        #: subscription that moves them. Held only while there is a bar on
        #: screen: a permanent per-frame callback would cost every frame of the
        #: session to animate a list that is usually empty.
        self._run_bars: list = []
        self._run_bar_phase = 0.0
        self._run_bar_subscription = None
        #: What the results strip was last drawn at, so a resize that does not
        #: change the cell size does not rebuild it.
        self._result_cell: int | None = None
        self._tool_detail: tools_api.ToolDetail | None = None
        self._generate_button: ui.Button | None = None
        self._num_results: ui.IntSlider | None = None
        self._cost_label: ui.Label | None = None
        self._results_frame: ui.Frame | None = None

        #: The account picker, which lives in the footer rather than on the
        #: Account tab: who pays is worth seeing while you are about to spend,
        #: and that is the Create tab.
        self._account_bar: ui.Frame | None = None
        self._account_combo: ui.ComboBox | None = None
        #: The primary action, in the footer beside BILLED TO rather than at
        #: the bottom of the Create tab's form. See `_render_action_bar`.
        self._action_bar: ui.Frame | None = None
        #: What the status line last said, and whether it was a failure. Held
        #: because the line can be turned off, and a message that arrives while
        #: it is off still has to be there when it is turned back on.
        self._status_message = ""
        self._status_is_failure = False
        self._show_status_log = prefs.get_bool(prefs.SHOW_STATUS_LOG)

        # State
        self._tools: list[tools_api.ToolSummary] = []
        #: The kit curation, fetched with the tools rather than when the
        #: browser opens. Both are account-scoped and the browser is a
        #: reader of them, so making it fetch its own left it showing an
        #: empty grid for a round trip every single time it was opened.
        self._kits: list[kits_api.Kit] = []
        self._session_results: list[SessionResult] = []
        self._tasks: set[asyncio.Task] = set()
        self._result_dir = Path(tempfile.mkdtemp(prefix="rundiffusion-"))
        #: The panel's general-purpose image fetcher: the preview on a chosen
        #: image field, and `_fetch_asset`. NOT a browsing grid.
        #:
        #: The distinction is load-bearing. `cancel_pending` abandons every
        #: fetch in flight on ONE AssetGrid, so whatever shares a grid shares a
        #: cancel. These previews die with the Create tab that holds them,
        #: which is exactly the wrong lifetime for a browsing surface sitting
        #: in a window of its own.
        self._grid = AssetGrid(self._result_dir)
        #: One grid per browsing surface, because one AssetGrid drives one live
        #: grid: it holds the VGrid the last full build made so that Load more
        #: can append to it, and `cancel_pending` bumps a generation that
        #: abandons every thumbnail still in the air.
        #:
        #: Both were harmless while exactly one tab could be on screen. With a
        #: surface in its own window they can be on screen together, and
        #: sharing would mean opening Uploads cancelled the library's
        #: half-loaded thumbnails and pointed its Load more at the wrong widget.
        self._surface_grids = {
            surface: AssetGrid(self._result_dir) for surface in POPPABLE_SURFACES
        }
        #: The stage's cameras and when they were last read. See
        #: CAMERA_LIST_CACHE_SECONDS for why this is a window rather than a
        #: list held for the session.
        self._camera_sources: list[tuple] | None = None
        self._camera_sources_read_at = 0.0
        #: Surface -> the window it has been torn out into, while it has one.
        self._windows: dict[str, Any] = {}
        #: Surface -> whether it belongs in a window rather than the tab strip.
        #: Remembered across sessions: see prefs.torn_out_key.
        self._torn_out = {
            surface: prefs.get_bool(prefs.torn_out_key(surface), False)
            for surface in POPPABLE_SURFACES
        }
        self._compare = CompareWindow()
        #: A live thumbnail of the viewport, so a field that says it is sending
        #: the viewport can show which frame it means.
        self._viewport_preview = ViewportPreview(
            self._result_dir, on_changed=self._on_viewport_preview_changed
        )
        #: Says when the stage has gained or lost prims, so the capture-source
        #: dropdown can stop being whatever it was at the last row rebuild.
        self._camera_watch = cameras_module.StageWatch(
            self._on_stage_cameras_changed
        )
        #: One still per named camera, for a viewfinder that is not showing the
        #: active viewport. Same callback as the live preview, so both arrive at
        #: the panel through one door.
        self._camera_stills = CameraStills(
            self._result_dir, on_changed=self._on_viewport_preview_changed
        )
        #: Assets chosen for image fields, keyed by SLOT id rather than by
        #: field, because a field can hold several at once. Kept whole rather
        #: than as references: a preview needs the url and a reference carries
        #: only the id.
        self._chosen_assets: dict[str, Any] = {}
        #: What was on the Create tab when it was last torn down. The tab is
        #: rebuilt on every switch, which destroys the controls, so without this
        #: a trip to the Library loses the prompt and every image choice.
        self._form_values: dict[str, Any] = {}
        self._image_state: dict[str, Any] = {}
        #: Which camera each image field is pointed at, carried across a
        #: rebuild of the Create tab. `_image_state` makes the same journey for
        #: the same reason: neither lives in a control that `collect` can read
        #: back, so neither survives the widgets being torn down on its own.
        self._capture_state: dict[str, Any] = {}
        #: Which session result the action buttons act on. None means the newest.
        self._selected_result: Any = None
        self._tab_collection: Any = None
        #: Whether the Create tab's widgets exist right now. A tab switch and a
        #: root rebuild both destroy them, and a destroyed model still answers.
        self._form_built = False
        #: How many results the next run asks for. On the panel rather than read
        #: off the slider, because the slider is destroyed on every tab switch.
        self._result_count = 1
        #: The pending re-price, so a burst of keystrokes costs one call.
        self._cost_task: Any = None
        #: Rebuilds waiting for the next frame. omni.ui refuses a container
        #: clear during event dispatch, and every renderer here is reachable
        #: from a click. See redraw.py.
        self._redraws = redraw_module.Redraws()
        self._jobs = JobQueue(on_changed=self._on_jobs_changed)
        self._jobs_frame: ui.Frame | None = None
        #: Surface -> the asset clicked in it, and the frame its actions are
        #: drawn into. Keyed rather than single, because either surface can be
        #: torn out into a window and both can then be on screen at once, each
        #: holding its own selection.
        self._selected_asset: dict[str, Any] = {}
        self._asset_actions_frame: dict[str, Any] = {}
        #: Library paging state, kept on the panel because the tab is rebuilt
        #: every time it is shown and none of this may be lost to that. The
        #: ITEMS are held, not just the cursor: a cursor only points forwards,
        #: so a user who paged six times into their history and glanced at
        #: Create would otherwise come back to page one.
        self._library_items: list[Any] = []
        self._library_cursor: str | None = None
        self._library_has_more = False
        self._library_loading = False
        #: Bumped whenever the result set starts again, so a page still in the
        #: air from the OLD query knows not to land. Without it, applying a
        #: filter while the first page was loading appended the unfiltered
        #: answer under the filtered one.
        self._library_generation = 0
        self._library_pager: ui.Frame | None = None
        #: The grid the Library tab is drawing into right now, so Load more can
        #: append to it from a callback that did not build it.
        self._library_frame: ui.Frame | None = None
        #: What the library is narrowed to, and what the filter panel is being
        #: edited to. Two objects because the panel commits on Apply rather than
        #: per keystroke: seven controls each firing a refetch would spend seven
        #: round trips arriving at one query.
        self._library_filters = assets_api.LibraryFilters()
        self._library_draft = assets_api.LibraryFilters()
        self._filters_frame: Any = None
        #: The rebuildable inside of the frame above. See _build_library_tab.
        self._filters_body: ui.Frame | None = None
        #: The tag vocabulary behind the Model family and Media filters, keyed
        #: by taxonomy. Fetched once per account with the tools.
        self._tool_tags: dict[str, list] = {}
        self._uploads_items: list[Any] = []
        self._uploads_cursor: str | None = None
        self._uploads_has_more = False
        self._uploads_loading = False
        self._uploads_generation = 0
        self._uploads_pager: ui.Frame | None = None
        self._uploads_frame: ui.Frame | None = None
        self._session_frame: Any = None
        #: The rebuildable inside of the frame above. See _build_library_tab.
        self._session_body: ui.Frame | None = None
        self._tool_browser = ToolBrowser(self._session, self._result_dir)
        self._asset_picker = AssetPicker(
            self._session, self._result_dir, on_uploaded=self._forget_uploads
        )
        #: Open shows the image in here rather than handing it to the operating
        #: system, so what you want to do next is in the same window as the
        #: picture.
        self._viewer = ImageViewer()
        #: Counts pictures asked of the viewer, so only the newest lands.
        self._viewer_request = 0

        self._build()
        self._spawn(self._restore_session())

    # -- lifecycle ---------------------------------------------------------

    def _spawn(self, coroutine) -> asyncio.Task:
        """Run a coroutine and KEEP THE REFERENCE.

        An unreferenced task can be garbage collected before it runs, which is a
        silent failure that looks exactly like a button doing nothing.

        The done callback is the other half of that. Nothing awaits these, so an
        exception inside one is reported by asyncio, once, as "Task exception
        was never retrieved" with a traceback and no mention of RunDiffusion.
        That is how a read timeout inside `_load_uploads` came to leave the grid
        on "Loading..." for good: the task died, the panel never heard, and the
        only evidence was a line in a console nobody browsing a library has
        open. Logging it under this plugin's name does not fix the failure, but
        it stops it being anonymous.
        """
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)
        return task

    def _task_finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            # Cancellation is how most of these end: a tab switch, a sign-out,
            # a superseded query.
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "RunDiffusion: a background task failed and nothing was told.",
                exc_info=error,
            )

    def destroy(self) -> None:
        self._redraws.cancel()
        self._run_bars = []
        self._run_bar_subscription = None
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._jobs.cancel_all()
        self._grid.destroy()
        for grid in self._surface_grids.values():
            grid.destroy()
        for surface in POPPABLE_SURFACES:
            self._close_window(surface)
        self._compare.destroy()
        self._tool_browser.destroy()
        self._asset_picker.destroy()
        self._viewer.destroy()
        self._viewport_preview.destroy()
        self._camera_watch.stop()
        self._camera_stills.destroy()
        shutil.rmtree(self._result_dir, ignore_errors=True)
        if self._window is not None:
            self._window.destroy()
            self._window = None

    # -- chrome ------------------------------------------------------------

    def _build(self) -> None:
        """One root frame, holding whichever of the two panels applies.

        Signed out, the sign-in screen owns the whole window: a person who
        cannot generate has nothing to do in a tab, and the code they have to
        read and type elsewhere deserves the room. Signed in, the root holds the
        tabbed workbench.
        """
        with self._window.frame:
            self._root = ui.Frame()
        self._render_root()

    def _render_root(self) -> None:
        if self._root is None:
            return
        # Everything below is about to be destroyed, so nothing may be read back
        # off it afterwards.
        self._form_built = False
        self._root.clear()
        with self._root:
            if self._session.is_signed_in:
                self._build_workbench()
            else:
                self._build_sign_in()

    def _build_workbench(self) -> None:
        """Tabs, a body that fills, and a status line pinned underneath.

        The sizing rule is the one the SDK's own windows use and the one this
        first got wrong: a stack DISTRIBUTES space among children that have no
        explicit size, so every label and frame took an equal share and the
        panel came out evenly spread. `height=0` means "size to content", so the
        chrome shrinks to what it needs and the body gets the remainder.
        """
        with ui.VStack(spacing=6):
            with ui.VStack(height=0, spacing=4):
                with ui.VStack(height=0, spacing=0):
                    with ui.HStack(height=26, spacing=0):
                        # Held on the panel so a tab opened from somewhere else
                        # (the session box's "Open Create tab") moves the
                        # highlight too.
                        collection = self._tab_collection = ui.RadioCollection()
                        self._tab_rules = []
                        for index, label in enumerate(TAB_LABELS):
                            with ui.VStack(spacing=0):
                                ui.ToolButton(
                                    text=label,
                                    radio_collection=collection,
                                    value=index,
                                    style=_STYLE_TAB,
                                    clicked_fn=lambda i=index: self._show_tab(i),
                                )
                                self._tab_rules.append(
                                    ui.Rectangle(
                                        height=_TAB_RULE_HEIGHT,
                                        style={"background_color": _TRANSPARENT},
                                    )
                                )
                    ui.Rectangle(height=1, style={"background_color": _TAB_RULE})
                # A blocked run is explained here, with whatever action can
                # actually fix it. Hidden while there is nothing to say.
                self._wall_frame = ui.Frame(height=0, visible=False)

            # The body takes everything left over.
            self._content = ui.Frame()
            # The viewfinder and the results strip are sized from the width
            # the panel has been given, so they are told when it changes.
            # Deferred: this is called from layout, and both rebuild
            # containers.
            self._content.set_computed_content_size_changed_fn(
                lambda: self._redraws.request("resize", self._on_panel_resized)
            )

            # Pinned under the body, so the last thing that happened stays
            # visible whichever tab is open. Who is being billed sits here for
            # the same reason: it applies to every tab, and a person about to
            # press Generate should not have to leave the Create tab to find out
            # whose tokens they are about to spend.
            with ui.VStack(height=0, spacing=4):
                ui.Separator(height=4)
                # Generate lives here, not at the bottom of the Create tab's
                # form. The form scrolls and the results strip grows under it,
                # so the one button that spends money was the thing most likely
                # to be off screen at the moment someone wanted it. In the
                # footer it is pinned, it is in the same place on every visit,
                # and it sits directly above the line naming who pays for it.
                #
                # Empty on every other tab: a Generate button on the Library tab
                # would either do nothing or act on a form nobody is looking at.
                self._action_bar = ui.Frame(height=0)
                self._account_bar = ui.Frame(height=0)
                self._render_account_bar()
                self._status = ui.Label("", word_wrap=True, height=0, visible=False)
                self._refresh_status()

        # Before the first tab is shown, so a tab switch that spares a torn-out
        # surface's widgets is sparing widgets that exist. Torn out is
        # remembered across sessions, and the window is what makes that memory
        # mean anything.
        for surface in POPPABLE_SURFACES:
            if self._torn_out[surface]:
                self._open_window(surface)
        self._show_tab(self._current_tab)

    def _render_action_bar(self) -> None:
        """Ask for the footer's action row to be redrawn on the next frame.

        Reached from every tab switch.

        omni.ui refuses a container clear during event dispatch, so the rebuild
        waits a frame. See redraw.py.
        """
        if self._action_bar is None:
            return
        self._redraws.request("action-bar", self._render_action_bar_now)

    def _render_action_bar_now(self) -> None:
        """Cost, how many, and Generate. Only while the Create tab is open.

        The whole row moved here together rather than the button alone. The
        price and the count are what the button is about to act on, and a
        Generate that floats away from the number it is going to multiply is
        worse than one that scrolls with it.
        """
        frame = self._action_bar
        if frame is None:
            return
        frame.clear()
        if self._current_tab != TAB_CREATE:
            self._cost_label = None
            self._num_results = None
            self._generate_button = None
            return

        with frame:
            with ui.VStack(height=0, spacing=4):
                with ui.HStack(height=20, spacing=6):
                    self._cost_label = ui.Label(
                        "", elided_text=True, style={"font_size": _SIZE_BODY, "color": _TEXT_BODY}
                    )
                    with ui.HStack(width=120, spacing=4):
                        ui.Label("Results", width=48, style={"font_size": _SIZE_BODY, "color": _TEXT_BODY})
                        self._num_results = ui.IntSlider(min=1, max=8)
                        self._num_results.model.set_value(self._result_count)
                        self._num_results.model.add_value_changed_fn(
                            lambda *_: self._on_result_count_changed()
                        )
                # The same style the sign-in screen's primary action uses. This
                # is the one thing on the panel that spends money, and until now
                # it looked like the Save and Compare buttons above it.
                self._generate_button = ui.Button(
                    "Generate",
                    height=34,
                    style=_STYLE_PRIMARY,
                    clicked_fn=self._on_generate_clicked,
                )
        self._schedule_cost_refresh()

    def _render_account_bar(self) -> None:
        """Who pays, on every tab, and the one place to change it.

        One picker rather than two. The Account tab used to carry its own, and
        two combo boxes over one setting is a synchronisation bug waiting to
        happen: change it in the footer while the Account tab is open and the
        other one is quietly showing the wrong account.
        """
        if self._account_bar is None:
            return
        self._account_bar.clear()
        self._account_combo = None
        accounts = self._session.accounts
        with self._account_bar:
            with ui.HStack(height=22, spacing=6):
                ui.Label("BILLED TO", width=68, style=_STYLE_CAPS)
                if len(accounts) > 1:
                    current = self._session.selected_account
                    index = accounts.index(current) if current in accounts else 0
                    self._account_combo = ui.ComboBox(
                        index, *[_account_label(a) for a in accounts], height=22
                    )
                    self._account_combo.model.add_item_changed_fn(
                        self._on_account_changed
                    )
                elif accounts:
                    # One account is not a choice, so it is stated rather than
                    # offered. It still says whose tokens a run spends.
                    ui.Label(_account_label(accounts[0]))
                else:
                    ui.Label("Loading your accounts...")

    # -- sign-in screen ----------------------------------------------------

    def _build_sign_in(self) -> None:
        """The whole panel, before there is an account to spend.

        Centred and vertically balanced: this is the first thing anyone sees of
        the product inside their 3D app, and a left-aligned column of controls
        reads as a settings form rather than as a front door.
        """
        with ui.VStack(spacing=0):
            ui.Spacer()

            with ui.VStack(height=0, spacing=6):
                with ui.HStack(height=MARK_SIZE):
                    ui.Spacer()
                    if MARK_PATH.exists():
                        ui.Image(
                            str(MARK_PATH),
                            width=MARK_SIZE,
                            height=MARK_SIZE,
                            fill_policy=ui.FillPolicy.PRESERVE_ASPECT_FIT,
                        )
                    ui.Spacer()
                ui.Label(
                    "RunDiffusion",
                    height=0,
                    alignment=ui.Alignment.CENTER,
                    style=_STYLE_WORDMARK,
                )
                ui.Label(
                    "For Omniverse",
                    height=0,
                    alignment=ui.Alignment.CENTER,
                    style=_STYLE_SUBTITLE,
                )

            ui.Spacer(height=20)
            # Named for the job in THIS host rather than for the technology.
            # An architect in Omniverse came to see their model as something
            # finished, and the panel that says so is the one they understand
            # before signing in.
            ui.Label(
                "Render any view of your scene in any style.",
                height=0,
                word_wrap=True,
                alignment=ui.Alignment.CENTER,
                style=_STYLE_TAGLINE,
            )
            ui.Spacer(height=8)
            ui.Label(
                "Frame a shot in the viewport, describe the look you want, and "
                "get finished images back in this panel. Your scene is never "
                "changed.",
                height=0,
                word_wrap=True,
                alignment=ui.Alignment.CENTER,
                style=_STYLE_SUPPORT,
            )

            ui.Spacer(height=24)
            self._sign_in_frame = ui.Frame(height=0)
            self._render_sign_in_steps()

            ui.Spacer()

            with ui.VStack(height=0, spacing=6):
                ui.Separator(height=4)
                with ui.HStack(height=22, spacing=6):
                    ui.Button(
                        "Setup guide",
                        clicked_fn=lambda: webbrowser.open(constants.ONBOARDING_URL),
                    )
                    ui.Button(
                        "Privacy",
                        clicked_fn=lambda: webbrowser.open(constants.PRIVACY_POLICY_URL),
                    )
                    ui.Button(
                        "Terms",
                        clicked_fn=lambda: webbrowser.open(constants.TERMS_OF_SERVICE_URL),
                    )
                ui.Label(
                    self._version_line(),
                    height=0,
                    word_wrap=True,
                    alignment=ui.Alignment.CENTER,
                    style=_STYLE_HELPER,
                )
                self._status = ui.Label(
                    "", word_wrap=True, height=0, alignment=ui.Alignment.CENTER
                )

    def _render_sign_in_steps(self) -> None:
        """Ask for this to be redrawn on the next frame.

        Reached from Cancel on the sign-in screen.

        omni.ui refuses a container clear during event dispatch, so the rebuild
        waits a frame. See redraw.py, and `_render_filters` for the trace that
        proved it.

        The guard is repeated here rather than left to the render alone: with
        nothing to draw into there is no reason to book a frame at all.
        """
        if self._sign_in_frame is None:
            return
        self._redraws.request("sign-in", self._render_sign_in_steps_now)

    def _render_sign_in_steps_now(self) -> None:
        """The two steps and the status row, redrawn as the flow moves on."""
        frame = self._sign_in_frame
        if frame is None:
            return
        frame.clear()

        prompt = self._device_prompt
        with frame:
            with ui.VStack(spacing=10, height=0):
                if prompt is None:
                    # Two doors, one flow. The sign-up button carries an intent
                    # so a brand-new user lands in the web sign-up form rather
                    # than on a login wall, which is exactly what the other
                    # RunDiffusion plugins do.
                    with ui.HStack(height=40):
                        ui.Spacer()
                        with ui.VStack(width=240, spacing=10):
                            ui.Button(
                                "Sign in",
                                height=40,
                                style=_STYLE_PRIMARY,
                                clicked_fn=lambda: self._on_sign_in_clicked(
                                    session_api.INTENT_SIGN_IN
                                ),
                            )
                        ui.Spacer()
                    ui.Spacer(height=10)
                    with ui.HStack(height=40):
                        ui.Spacer()
                        with ui.VStack(width=240):
                            ui.Button(
                                "Create a free account",
                                height=40,
                                style=_STYLE_SECONDARY,
                                clicked_fn=lambda: self._on_sign_in_clicked(
                                    session_api.INTENT_SIGN_UP
                                ),
                            )
                        ui.Spacer()
                    ui.Spacer(height=14)
                    ui.Label(
                        "Opens app.rundiffusion.com in your browser to approve "
                        "this device. It only takes a few seconds.",
                        word_wrap=True,
                        height=0,
                        alignment=ui.Alignment.CENTER,
                        style=_STYLE_HELPER,
                    )
                    if self._sign_in_state not in ("Not signed in.", ""):
                        ui.Spacer(height=10)
                        ui.Label(
                            self._sign_in_state,
                            word_wrap=True,
                            height=0,
                            alignment=ui.Alignment.CENTER,
                        )
                    return

                with ui.VStack(spacing=6, height=0):
                    ui.Label(
                        "STEP 1 · YOUR CODE", height=0, alignment=ui.Alignment.CENTER
                    )
                    # A read-only field rather than a label: it can be selected
                    # and copied by hand, which is the fallback for a host
                    # without the clipboard extension.
                    code = ui.StringField(height=36, read_only=True)
                    code.model.set_value(prompt.user_code)
                    with ui.HStack(height=24, spacing=6):
                        ui.Button(
                            "Copy code",
                            clicked_fn=lambda c=prompt.user_code: self._copy_code(c),
                        )
                    ui.Label(
                        "Codes expire. Start again if this one stops working.",
                        word_wrap=True,
                        height=0,
                    )

                with ui.VStack(spacing=6, height=0):
                    ui.Label(
                        "STEP 2 · AUTHORIZE IN YOUR BROWSER",
                        height=0,
                        alignment=ui.Alignment.CENTER,
                    )
                    url = session_api.verification_url_for_intent(
                        prompt.verification_url, self._sign_in_intent
                    )
                    ui.Button(
                        f"Open {prompt.verification_url}",
                        height=28,
                        clicked_fn=lambda u=url: webbrowser.open(u),
                    )
                    ui.Label(
                        "Did not open? Type that address into any browser, on "
                        "this machine or your phone, and enter the code.",
                        word_wrap=True,
                        height=0,
                    )

                with ui.HStack(height=24, spacing=6):
                    ui.Label(self._sign_in_state, word_wrap=True)
                    ui.Button("Cancel", width=70, clicked_fn=self._cancel_sign_in)

    def _copy_code(self, code: str) -> None:
        """Put the user code on the clipboard, or say why that is not possible.

        `omni.kit.clipboard` ships in the stock SDK and in the customer build,
        checked in both, but it is imported here rather than declared as an
        extension dependency: a host that strips it should lose the button, not
        the whole extension. The code is in a selectable field either way.
        """
        try:
            import omni.kit.clipboard

            omni.kit.clipboard.copy(code)
        except Exception:  # noqa: BLE001 - a missing clipboard is not a failure
            logger.info("RunDiffusion: no clipboard in this app.")
            self._set_error("Copy is unavailable here. Select the code and press Ctrl+C.")
            return
        self._set_status(f"Copied {code}.")

    def _cancel_sign_in(self) -> None:
        task = self._sign_in_task
        self._sign_in_task = None
        self._device_prompt = None
        self._sign_in_state = "Not signed in."
        if task is not None:
            task.cancel()
        self._render_sign_in_steps()

    def _version_line(self) -> str:
        """Version and host build, for a support conversation."""
        build = ""
        try:
            import omni.kit.app

            build = omni.kit.app.get_app().get_build_version() or ""
        except Exception:  # noqa: BLE001 - a version line never breaks a panel
            build = ""
        suffix = f" · Kit {build}" if build else ""
        return f"RunDiffusion for Omniverse {EXTENSION_VERSION}{suffix}"

    def _show_tab(self, index: int) -> None:
        # Read the Create tab BEFORE tearing it down. Switching tabs rebuilds
        # the body, which destroys every control, so what the user typed and
        # which image each field is sending have to be carried out by hand.
        if self._current_tab == TAB_CREATE and self._form_built:
            # Only while the widgets are alive. Reading a destroyed model
            # returns junk, and the junk would overwrite the good snapshot.
            self._form_values = self._form.collect()
            self._image_state = self._form.export_image_state()
            self._capture_state = self._form.export_capture_sources()
        self._form_built = False
        # These belong to the tab about to be destroyed, and a job running in
        # the background renders into them every time its state changes. A
        # destroyed frame still answers, so the reference is dropped rather than
        # left pointing at one; every renderer below treats None as "not on
        # screen" and does nothing.
        #
        # A rebuild booked against a frame this is about to destroy has nothing
        # left to draw into. A surface's are spared while it is in a window of
        # its own, which this switch does not touch.
        self._redraws.cancel(keep_prefixes=self._torn_out_prefixes())
        self._results_frame = None
        self._jobs_frame = None
        self._cost_label = None
        self._capture_frame = None
        self._form.release_capture_zone()
        # Only the surfaces this switch actually destroys. One in a window of
        # its own is still on screen, still being drawn into, and still has
        # thumbnails legitimately in the air. Every cell of a grid that IS
        # going away has a fetch that may still land, and a fetch that returns
        # after this would paint into a widget nobody is looking at; the next
        # build re-draws from the cache rather than re-downloading.
        for surface in self._docked_surfaces():
            self._drop_surface_frames(surface)
        # The Create tab's chosen-image previews go through the panel's general
        # fetcher rather than a surface grid, and they die with the tab body
        # whatever is torn out.
        self._grid.cancel_pending()

        self._current_tab = index
        if self._tab_collection is not None:
            self._tab_collection.model.set_value(index)
        self._light_tab_rule(index)
        # The bars belong to the runs list this switch is about to destroy.
        self._run_bars = []
        self._run_bar_subscription = None
        if index != TAB_CREATE:
            # Nothing on screen is showing the viewport, so stop capturing it,
            # and nothing on screen lists the cameras, so stop watching for
            # them.
            self._viewport_preview.stop()
            self._camera_watch.stop()
        if self._content is None:
            return
        # The footer's action row belongs to whichever tab is open, and it is
        # the tab switch rather than the tab build that decides that: every
        # other tab wants it empty, and only this method knows they were the
        # one chosen.
        self._render_action_bar()
        self._content.clear()
        with self._content:
            if index == TAB_CREATE:
                self._build_create_tab()
            elif index == TAB_LIBRARY:
                self._build_library_tab()
            elif index == TAB_UPLOADS:
                self._build_uploads_tab()
            else:
                self._build_account_tab()

    def _light_tab_rule(self, index: int) -> None:
        """Put the amethyst rule under the open tab and take it from the rest.

        A colour change rather than a visibility change: a rule that is hidden
        gives its two pixels back, and the tab labels would jump as the open
        tab moved.
        """
        for position, rule in enumerate(self._tab_rules):
            try:
                rule.set_style(
                    {"background_color": _AMETHYST if position == index else _TRANSPARENT}
                )
            except Exception:  # noqa: BLE001 - a rule mid-teardown is not worth a traceback
                logger.debug("RunDiffusion: could not light a tab rule.")

    def _on_panel_resized(self) -> None:
        """The panel changed size. Tell the two things drawn from its width."""
        if self._current_tab != TAB_CREATE:
            return
        self._form.on_width_changed()
        if self._result_cell != self._result_cell_size():
            self._render_results()

    def _set_status(self, message: str) -> None:
        """Say what just happened. Shown only if the status log is turned on.

        This is a running commentary, and most of it is chatter: "Downloading...",
        "Copied abc-123.", "Reused the inputs from 14:05." It was useful while
        the panel was being built and it is noise to someone using it, so it is
        off by default and there is a switch for it on the Account tab.

        `_set_error` is the other half. A failure is never chatter, and hiding
        one behind a preference would mean a person whose save failed is told
        nothing at all.
        """
        self._status_message = message
        self._status_is_failure = False
        self._refresh_status()

    def _set_error(self, message: str) -> None:
        """Say what went wrong. Shown whether the status log is on or not.

        Every caller of this is a path where something the user asked for did
        not happen. The status line is the only place the panel has to say so:
        there is no dialog, and a message in the Kit console is one nobody
        browsing a library has open.
        """
        self._status_message = message
        self._status_is_failure = True
        self._refresh_status()

    def _refresh_status(self) -> None:
        """Put the held message on screen, or take the line away.

        The message is held rather than written straight to the label, because
        the line can be turned on after it arrived: switching the log on should
        show what the panel last said, not an empty row waiting for the next
        thing to happen.
        """
        if self._status is None:
            return
        showing = bool(self._status_message) and (
            self._show_status_log or self._status_is_failure
        )
        self._status.text = self._status_message if showing else ""
        # Hidden rather than blank. An empty label still takes its spacing, and
        # a footer with a gap where nothing is ever written looks like something
        # failed to load.
        self._status.visible = showing

    def _set_status_log(self, enabled: bool) -> None:
        self._show_status_log = enabled
        prefs.set_bool(prefs.SHOW_STATUS_LOG, enabled)
        self._refresh_status()

    def _set_cost(self, message: str) -> None:
        if self._cost_label is not None:
            self._cost_label.text = message

    # -- Create tab --------------------------------------------------------

    def _build_create_tab(self) -> None:
        """The viewfinder, the tool and its form scroll; runs and results do not.

        Top to bottom: the frame and its Capture button, the tool, the form,
        then pinned under all of it what runs are going and what they made.

        The viewfinder leads because framing a view is the first thing done
        here, and it is the widest thing on the tab because judging a view in a
        34px thumbnail was the complaint. It scrolls with the form rather than
        being pinned: once a capture is taken it folds to a 40px strip anyway,
        and pinning 205px above a long form would leave the form a slot to
        scroll through.

        The split underneath is the older point of the layout. A complex tool
        makes the form long, and when everything scrolled together the one
        button that spends money scrolled away with it.
        """
        with ui.VStack(spacing=6):
            with ui.ScrollingFrame():
                with ui.VStack(height=0, spacing=6):
                    self._capture_frame = ui.Frame(height=0)

                    self._tool_frame = ui.Frame(height=0)
                    with self._tool_frame:
                        self._build_tool_row("Loading tools...")
                    if self._tools:
                        self._render_tool_picker()

                    self._form_frame = ui.Frame(height=0)
                    with self._form_frame:
                        ui.Label("Pick a tool to see its inputs.", style=_STYLE_SECONDARY)
                    # Rebuilt from the schema already in hand, so switching
                    # tabs does not refetch or lose what was typed. The values
                    # and the image choices are put back from the snapshot
                    # taken when the tab was last torn down.
                    if self._tool_detail is not None:
                        self._form.build(
                            self._form_frame,
                            self._tool_detail,
                            self._form_values,
                            capture_container=self._capture_frame,
                        )
                        self._form.restore_image_state(self._image_state)
                        # Before `_sync_viewport_watch` below, which decides
                        # whether to read the viewport at all by asking the
                        # form what the frame is pointed at.
                        self._form.restore_capture_sources(self._capture_state)

            # Cost, Results and Generate used to sit here. They are in the
            # footer now, which is why this tab ends with what a run PRODUCED
            # rather than with the button that starts one.
            with ui.VStack(height=0, spacing=6):
                ui.Rectangle(height=1, style={"background_color": _SEPARATOR})
                self._jobs_frame = ui.Frame(height=0)
                self._render_jobs()

                self._results_frame = ui.Frame(height=0)
                self._render_results()

            self._form_built = self._tool_detail is not None
            self._sync_viewport_watch()
            # The cost line is in the footer, and the footer is redrawn by the
            # tab switch that got here rather than by this method.

    def _render_tool_picker(self) -> None:
        """Ask for this to be redrawn on the next frame.

        Reached from picking a tool in the browser window.

        omni.ui refuses a container clear during event dispatch, so the rebuild
        waits a frame. See redraw.py, and `_render_filters` for the trace that
        proved it.

        The guard is repeated here rather than left to the render alone: with
        nothing to draw into there is no reason to book a frame at all.
        """
        if self._tool_frame is None or not self._tools:
            return
        self._redraws.request("tool-picker", self._render_tool_picker_now)

    def _render_tool_picker_now(self) -> None:
        """The selected tool, as a button that opens the browser.

        A ComboBox used to live here with a Browse button beside it, and the
        dropdown was the worse half of that pair by every measure: it lists
        names with no avatars, no descriptions and no kits, it holds only the
        page of the catalogue this panel happens to have loaded, and picking
        out of fifty names in a native popup is the exact task the browser was
        built to replace.

        So the control keeps the dropdown's place and its job of naming the
        current tool, and hands the choosing to the browser. It is a button
        because omni.ui gives no way to intercept a ComboBox's own popup:
        clicking one opens its list and nothing can stop it, so a dropdown
        that opens a window instead is not something this toolkit can express.
        """
        if self._tool_frame is None or not self._tools:
            return
        # Frame is a Container, so the label is replaced by rebuilding.
        self._tool_frame.clear()
        selected = self._selected_tool()
        with self._tool_frame:
            self._build_tool_row(selected.name if selected else "Choose a tool...")

    def _build_tool_row(self, name: str) -> None:
        """TOOL over the tool's name, and Browse... beside it.

        The tool is a STATE, so it reads as a label rather than as a full-width
        button one row above another full-width button. The only thing to do
        with it is choose another, and that is the quiet button at the end.
        """
        with ui.HStack(height=0, spacing=6):
            with ui.VStack(height=0, spacing=1):
                ui.Label("TOOL", height=0, style=_STYLE_CAPS)
                ui.Label(
                    name,
                    height=0,
                    elided_text=True,
                    style={"font_size": _SIZE_TITLE, "color": _TEXT_EMPHASIS},
                )
            with ui.VStack(width=64, height=0):
                ui.Spacer(height=5)
                self._tool_button = ui.Button(
                    "Browse...",
                    width=64,
                    height=24,
                    style=_STYLE_QUIET_BUTTON,
                    clicked_fn=self._on_browse_clicked,
                )

    def _on_browse_clicked(self) -> None:
        if not self._tools:
            self._set_error("Sign in to browse tools.")
            return
        self._tool_browser.show(self._tools, self._on_browsed_tool, kits=self._kits)

    def _on_browsed_tool(self, tool: tools_api.ToolSummary) -> None:
        """Point the dropdown at the browsed tool, so one selection drives both.

        A browsed tool is not always in the dropdown. The panel loads a page of
        the catalogue, and a kit curates from ALL of it, so a kit can offer a
        tool this list has never seen. That used to fall down a `return` and do
        nothing at all: the window closed on a click that had no effect.

        It is adopted instead, because the server has already said this account
        can run it, which is the only question the dropdown's contents answer.
        """
        if tool.id not in [t.id for t in self._tools]:
            self._tools.append(tool)
        self._selected_tool_id = tool.id
        self._render_tool_picker()
        self._spawn(self._load_tool_detail())

    def _on_pick_image(self, field_key: str) -> None:
        """Let a field take an image from the library, uploads or a file."""

        def _picked(asset) -> None:
            source = asset.kind.split("_")[0].lower()
            name = asset.name or f"an image from your {source}"
            slot_id = self._form.add_chosen(
                field_key, asset.as_input_ref(), f"Sending {name}."
            )
            if slot_id is None:
                # The one image-add path with no check of its own. The other
                # two ask before the work; this one can only ask after, because
                # the picker is a window the user has already been through.
                self._set_error(
                    f"This run already carries {MAX_INPUT_FILES} images, which "
                    "is all one request can. Remove one first."
                )
                return
            # Keyed by slot rather than by field, because a plural field holds
            # several images at once and each needs its own preview.
            self._chosen_assets[slot_id] = asset
            # Redrawn AFTER the asset is recorded: the slot was added first, so
            # its preview was drawn while this dictionary still had nothing to
            # look up, and it would sit there as a placeholder otherwise.
            self._form.redraw(field_key)

        self._asset_picker.show(_picked)

    # -- image previews ----------------------------------------------------

    def _render_image_preview(self, field_key: str, slot, frame: ui.Frame) -> None:
        """Draw the one image this slot will send.

        A capture is already on disk as its own thumbnail, taken when the user
        asked for it. Nothing here is live: what is drawn is what will be sent.
        """
        frame.clear()

        if slot.is_capture:
            with frame:
                if slot.thumbnail is not None:
                    ui.Image(
                        str(slot.thumbnail),
                        fill_policy=ui.FillPolicy.PRESERVE_ASPECT_FIT,
                    )
                else:
                    ui.Label("Captured view", alignment=ui.Alignment.CENTER)
            return

        asset = self._chosen_assets.get(slot.id)
        url = asset.preview_url if asset is not None else None
        with frame:
            ui.Label("..." if url else "Chosen image", alignment=ui.Alignment.CENTER)
        if url:
            # Same fetch, same cache, and the same WebP-to-PNG decode the grids
            # use: a library thumbnail is not a PNG, and Kit cannot load what the
            # server actually sends.
            self._grid.load_image_into(frame, url, size=PREVIEW_SIZE)

    def _render_viewfinder(self, field_key: str, frame: ui.Frame) -> None:
        """Draw what a capture from this field's source would take.

        Two different things wear one frame, and which one is showing follows
        the source dropdown beside it.

        On the active viewport it is LIVE, and it is the only live thing in an
        image field: it follows the camera so a shot can be framed and seen
        before it is pinned.

        On a named camera it is a STILL, because a live view of a camera that
        is not the active one means borrowing the viewport for every refresh
        and making the user's own view flicker while they type. The still is
        taken when the camera is chosen and again after each capture from it.
        See camera_stills for the whole of that reasoning.
        """
        frame.clear()
        camera_path = self._form.capture_source(field_key)
        if camera_path is None:
            path = self._viewport_preview.path
            message = self._viewport_preview.message
        else:
            # Asked for on every redraw, and `ensure` is what makes that
            # affordable: it captures only where there is nothing to show yet.
            self._camera_stills.ensure(camera_path, self._spawn)
            path = self._camera_stills.path(camera_path)
            message = self._camera_stills.message(camera_path)

        # Shown at the shape a capture will take. Where the user picked an
        # output size by hand the capture is cropped to it, and a frame showing
        # the whole viewport would be showing a picture that will not be sent.
        shape = self._form.capture_aspect()
        if path is not None and shape is not None:
            path = self._cropped_preview(path, shape)

        with frame:
            if path is not None:
                ui.Image(str(path), fill_policy=ui.FillPolicy.PRESERVE_ASPECT_FIT)
            else:
                ui.Label(
                    message or "Looking...",
                    alignment=ui.Alignment.CENTER,
                    word_wrap=True,
                )
        self._sync_viewport_watch()

    def _cropped_preview(self, path: Path, shape: float) -> Path:
        """A copy of a viewfinder picture cropped to `shape`, made once per pair.

        A new file per source and shape rather than one rewritten in place,
        because omni.ui caches an image by its path and a rewritten file would
        keep showing the old crop. Only the newest is kept.
        """
        key = (str(path), round(shape, 4))
        if self._cropped_previews.get("key") == key:
            return self._cropped_previews["path"]
        target = self._result_dir / f"viewfinder-crop-{uuid.uuid4().hex}.png"
        if not write_cropped(path, target, shape):
            return path
        previous = self._cropped_previews.get("path")
        if previous is not None:
            try:
                previous.unlink()
            except OSError:
                pass
        self._cropped_previews = {"key": key, "path": target}
        return target

    def _capture_sources(self) -> list[tuple]:
        """Where a capture can be taken from, as (label, value) pairs.

        The active viewport leads and carries None, because it is what every
        capture was before this and what a stage with no authored cameras still
        offers.

        Re-read as the stage changes rather than held for the session, because
        cameras are created and deleted while the panel is open and a list
        cached at sign-in would go stale silently. Not re-read on every ASK,
        though: see CAMERA_LIST_CACHE_SECONDS for what one ask costs and how
        many of them one click used to make.
        """
        now = time.monotonic()
        if (
            self._camera_sources is not None
            and now - self._camera_sources_read_at < CAMERA_LIST_CACHE_SECONDS
        ):
            return list(self._camera_sources)

        sources: list[tuple] = [(ACTIVE_VIEWPORT_LABEL, None)]
        sources.extend(
            (camera.label, camera.path) for camera in cameras_module.stage_cameras()
        )
        self._camera_sources = sources
        self._camera_sources_read_at = now
        # Copied out, so a caller that sorts or trims its answer cannot edit
        # the list every later caller is going to be handed.
        return list(sources)

    def _camera_label(self, camera_path: str | None) -> str:
        """What to call a capture source in a sentence.

        Looked up against the stage rather than carried around on the choice,
        so a camera renamed since the field was pointed at it is named as it is
        now. `_capture_into` asks once, on the click, and uses that one answer
        for both the line it shows and the words it records: see the note
        there, and CAMERA_LIST_CACHE_SECONDS for what asking costs.
        """
        if camera_path is None:
            return ACTIVE_VIEWPORT_LABEL
        for label, value in self._capture_sources():
            if value == camera_path:
                return label
        # Deleted between the click and the capture landing. The path is still
        # true and still identifies the framing, which is what the label is for.
        return camera_path

    def _on_capture_clicked(self, field_key: str, camera_path: str | None = None) -> None:
        """Take a view now and pin it into `field_key`.

        `camera_path` is whatever the field's source dropdown was on when the
        button was pressed, read by the form on the click and handed over. None
        means the active viewport.
        """
        if not self._form.has_room(field_key):
            self._refuse_capture(
                field_key,
                f"This run already carries {MAX_INPUT_FILES} images, which is "
                "all one request can. Remove one to capture another.",
            )
            return
        self._spawn(self._capture_into(field_key, camera_path))

    def _refuse_capture(self, field_key: str, message: str) -> None:
        """Say why a capture did not happen, in the frame that was captured.

        Only there. It used to go to the status line at the foot of the panel
        as well, which put the same sentence in two places a panel's height
        apart, and the one under BILLED TO was never where anyone was looking
        when they pressed Capture.
        """
        self._form.show_capture_error(field_key, message)

    async def _capture_into(self, field_key: str, camera_path: str | None = None) -> None:
        # Looked up ONCE, for the line under the button and for the words
        # recorded with the slot, where it used to be asked for twice. Each
        # lookup walks the stage (see CAMERA_LIST_CACHE_SECONDS), and this is
        # also the label the user was reading when they pressed the button,
        # which is the right name for a sentence about what they just asked.
        label = self._camera_label(camera_path)
        # Said in the frame's band, which is where the person is looking.
        # Every capture settles for sixty frames before it reads anything,
        # which on a path-traced stage is seconds, and a frame that says
        # nothing for that long looks like a click that missed. Not in the
        # status line: that is off by default and a panel's height away.
        self._form.show_capture_busy(field_key, CAPTURING_TEXT)
        try:
            png = await capture_png(camera_path)
        except CaptureError as error:
            self._refuse_capture(field_key, str(error))
            return
        except Exception as error:  # noqa: BLE001 - see below
            # Anything else: a viewport getter that moved, an optional import
            # that is not there, a USD call that raised. Without this the task
            # dies, the busy line is cleared by the `finally`, and the panel
            # says NOTHING at all, because the status line is off by default.
            # A button that goes quiet is indistinguishable from a click that
            # missed. CancelledError is a BaseException and passes through.
            logger.exception("RunDiffusion: the capture failed.")
            self._refuse_capture(field_key, f"The capture failed. {error}")
            return
        finally:
            # Whatever happened, the capture is no longer in flight. Cleared
            # after `_refuse_capture` has had its say, so the coalesced redraw
            # draws the refusal rather than a blank where the busy line was.
            self._form.clear_capture_busy(field_key)

        # Cropped to the output size the user picked by hand, when they have
        # picked one, so the picture sent is the shape the run asks for. Read
        # after the capture rather than on the click: the size is a setting on
        # the form, not something that moved with the camera.
        shape = self._form.capture_aspect()
        png = crop_png(png, shape)
        thumbnail = self._result_dir / f"capture-{uuid.uuid4().hex}.png"
        if not write_thumbnail(png, thumbnail):
            # The bytes are still good, so the run is not lost: the slot shows a
            # label instead of a picture.
            thumbnail = None
        # Named so the slot, the compare picker and anything else reading the
        # run's image state can say which camera this framing came from. A
        # capture from the active viewport has nothing more specific to say
        # than "captured view", so it says nothing and keeps the old words.
        # A framing worth keeping is kept HERE, a moment after the pixels were
        # read, rather than when the run is submitted. The viewport is still
        # where it was when the button was pressed; by submit time the user has
        # usually orbited somewhere else, and a camera pinned then would be a
        # camera of the wrong place carrying the right name.
        framed_by = camera_path
        not_kept = False
        if camera_path is None and self._form.pin_view(field_key):
            framed_by = render_cameras.save_active_view()
            if framed_by is None:
                # The capture itself is fine and its image is already in hand.
                # Said out loud all the same, once the capture has landed: a
                # box that was ticked and did nothing is worse than one that
                # was never offered.
                not_kept = True
            else:
                label = render_cameras.camera_label(framed_by)
        description = "" if framed_by is None else f"Captured from {label}"
        if (
            self._form.add_capture(field_key, png, thumbnail, description, framed_by)
            is None
        ):
            # The room was checked before the viewport was read, so this means
            # something else filled the form while the capture was being taken.
            self._refuse_capture(
                field_key,
                f"This run already carries {MAX_INPUT_FILES} images, which is "
                "all one request can. Remove one to capture another.",
            )
            return
        # It worked, so whatever the last one refused for no longer holds.
        self._form.clear_capture_error(field_key)
        if not_kept:
            self._form.show_capture_error(
                field_key, "Captured, but that view could not be kept as a camera."
            )
        # The view has a shape and the run should have the same one. Done here
        # rather than at submit time, so the size on screen is the size that
        # will be used and the user can disagree with it before they pay for
        # the run. A tool with no output-size field takes no notice.
        # A capture cropped to a chosen size already has that size's shape.
        self._form.match_capture_aspect(shape or captured_aspect(png))
        # The scene under the camera may have moved since its still was taken,
        # and the picture just captured is the most current thing there is.
        if camera_path is not None:
            self._camera_stills.forget(camera_path)
            self._camera_stills.refresh(camera_path, self._spawn)

    def _sync_viewport_watch(self) -> None:
        """Watch the stage only while something on screen is showing it.

        Two watches, and they are scoped differently on purpose.

        The LIVE PREVIEW is the narrow one. A tool with no image field at all
        should not be paying for a readback every time the user looks around,
        and neither should a frame pointed at a named camera (a still) or one
        folded to its strip (a pinned capture): a live readback would be
        feeding nothing.

        The CAMERA LIST is wanted wherever a capture-source dropdown exists,
        whichever source it happens to be set to. The dropdown is the thing
        that goes stale, and a field pointed at a named camera is exactly the
        one that needs to hear the camera has been deleted.
        """
        fields = self._form.image_field_keys
        on_create = self._current_tab == TAB_CREATE
        watching = on_create and self._form.viewfinder_is_live()
        if watching:
            self._viewport_preview.start()
        else:
            self._viewport_preview.stop()
        if on_create and fields:
            self._camera_watch.start()
        else:
            self._camera_watch.stop()

    def _on_stage_cameras_changed(self) -> None:
        """Prims were added or removed, so the camera list may be out of date.

        Booked rather than acted on. One edit arrives as a burst of notices and
        an import arrives as thousands, and `Redraws` collapses a burst into a
        single pass on the next frame. Without that this would walk the whole
        stage per notice, which is the cost CAMERA_LIST_CACHE_SECONDS exists to
        avoid in the first place.
        """
        self._redraws.request("cameras", self._refresh_capture_sources)

    def _refresh_capture_sources(self) -> None:
        """Re-read the stage, and redraw the image rows only if it moved.

        The comparison is what keeps ordinary work from rebuilding an image row
        under the user's hands. Moving a camera, renaming a mesh, adding a
        light, dropping in a reference: each of those resyncs something and
        none of them changes what the dropdown offers.

        Where it HAS moved, the rebuild does more than add a row to a list. A
        field pointed at a camera that has just been deleted is put back on the
        active viewport and told so, through `_reset_missing_source`, which
        until now only ran when the row happened to be rebuilt for some other
        reason.
        """
        # `Redraws` collapses a burst of notices into one call per FRAME, not
        # one per burst, and a stage that resyncs for several seconds (a large
        # reference dropped in, a heavy payload loading progressively) keeps
        # notifying for all of them. Walking every prim on each of those frames
        # is the cost this was supposed to avoid, so a re-read waits out the
        # same window an ordinary ask does and re-books itself meanwhile. The
        # last notice in the burst still gets its read.
        if time.monotonic() - self._camera_sources_read_at < CAMERA_LIST_CACHE_SECONDS:
            self._redraws.request("cameras", self._refresh_capture_sources)
            return
        previous = self._camera_sources
        # Past the cache: being told is the whole reason to look again.
        self._camera_sources = None
        if self._capture_sources() == previous:
            return
        for key in self._form.image_field_keys:
            self._form.redraw(key)

    def _on_viewport_preview_changed(self) -> None:
        self._form.refresh_viewfinders()

    def _view_image_slot(self, field_key: str, slot) -> None:
        """Show one pinned input image in the viewer.

        A capture is already bytes in hand, so it goes straight up at full
        size. The 58px thumbnail in the row is enough to tell two images apart
        and not enough to judge whether a capture came out the way it was
        meant to, which is the thing someone checks right after framing a shot.

        A chosen asset takes the same route the library takes for it, so a
        library image looks the same here as it does there and costs one
        download rather than a second copy of the fetching.
        """
        if slot.is_capture:
            if not slot.png:
                self._set_error("That capture has no image to show.")
                return
            path = self._result_dir / f"input-{slot.id}.png"
            try:
                path.write_bytes(slot.png)
            except OSError as error:
                self._set_error(f"Could not open that capture: {error}")
                return
            # No actions. This window is for looking at an input before it is
            # sent, and every verb that belongs to a capture (reorder, remove)
            # is already on the row it came from.
            self._viewer.show(
                path,
                title=slot.description or "Captured view",
                caption="This is what will be sent, at full size.",
            )
            return

        asset = self._chosen_assets.get(slot.id)
        if asset is None:
            self._set_error("That image is no longer available to show.")
            return
        # No Use in Create, for the same reason the capture branch above offers
        # no actions at all: this image is ALREADY in the field it was opened
        # from. Offering to use it would add a second slot holding the same
        # picture and send it twice, which is not what "have a proper look at
        # what I pinned" asked for.
        self._spawn(self._open_asset(asset, offer_use=False))

    def _selected_tool(self) -> tools_api.ToolSummary | None:
        if not self._tools:
            return None
        for tool in self._tools:
            if tool.id == self._selected_tool_id:
                return tool
        # Nothing chosen yet, or the chosen one is gone with the account that
        # could run it. The first tool is what the dropdown opened on.
        return self._tools[0]

    def _requested_results(self) -> int:
        return max(1, self._result_count)

    def _on_result_count_changed(self) -> None:
        if self._num_results is not None:
            self._result_count = max(1, self._num_results.model.get_value_as_int())
        self._schedule_cost_refresh()

    # -- Library / Uploads tabs -------------------------------------------

    # -- the library, in the tab strip or in its own window ---------------

    def _drop_surface_frames(self, surface: str) -> None:
        """Let go of the widgets one browsing surface owns.

        Called where they are actually destroyed: a tab switch while the
        surface is docked, and closing or opening its window. A destroyed frame
        still answers, so the reference is dropped rather than left pointing at
        one, and every renderer treats None as "not on screen".

        The two surfaces own different widgets, which is why this branches
        rather than looping over a list. The library carries the session box
        and the filter panel; uploads carries neither, because /uploads takes a
        limit and a cursor and nothing to filter on.
        """
        if surface == SURFACE_LIBRARY:
            self._session_frame = None
            self._session_body = None
            self._library_frame = None
            self._library_pager = None
            self._filters_frame = None
            self._filters_body = None
        else:
            self._uploads_frame = None
            self._uploads_pager = None
        # Kept per surface, so these are released with a pop rather than by
        # being set to None. Both matter: the frame is a destroyed widget, and
        # the selection is what that frame would be rebuilt to show.
        self._asset_actions_frame.pop(surface, None)
        self._selected_asset.pop(surface, None)
        self._grid_for(surface).cancel_pending()

    def _docked_surfaces(self) -> tuple[str, ...]:
        """The asset surfaces living in the tab body right now.

        Only the ones not torn out: in a window of its own a surface survives a
        tab switch, and a selection made in it is still on screen afterwards.
        """
        return tuple(
            surface
            for surface in POPPABLE_SURFACES
            if not self._torn_out[surface]
        )

    def _torn_out_prefixes(self) -> tuple[str, ...]:
        """Redraw keys a tab switch must leave alone.

        Everything a surface books is keyed under its own name, so one prefix
        per surface currently in a window spares exactly the rebuilds that are
        still looking at something.

        The layout moves are spared whatever the surface is doing. One of them
        is the request to tear a surface OUT, booked while it is still docked
        and therefore not covered by the rule above: a tab switch landing in
        that one frame used to drop it, and the Pop out button simply did
        nothing, with no window, no message and no preference written.
        """
        return tuple(f"{surface}:layout" for surface in POPPABLE_SURFACES) + tuple(
            f"{surface}:"
            for surface in POPPABLE_SURFACES
            if self._torn_out[surface]
        )

    def _request_layout(self, surface: str, move) -> None:
        """Move a surface between its tab and its own window, next frame.

        Every caller is a click handler or a window callback, and every one of
        these moves rebuilds a container: the tab body that holds the button,
        the window's own frame, or both. omni.ui refuses a container clear
        during dispatch (see redraw.py), and the button that asked is usually
        inside the container that goes away.

        Booked under the surface's own prefix, so a tab switch that spares its
        pending rebuilds spares the move with them. One key per surface,
        because these are answers to the same question and the newest answer is
        the one that counts.
        """
        self._redraws.request(f"{surface}:layout", move)

    def _tear_out(self, surface: str) -> None:
        self._torn_out[surface] = True
        prefs.set_bool(prefs.torn_out_key(surface), True)
        # The tab body is about to be rebuilt as a placeholder, so the widgets
        # the surface had in it are going away whether or not it is the tab on
        # screen. Let go before the window builds a second set.
        self._drop_surface_frames(surface)
        self._open_window(surface)
        if self._current_tab == SURFACE_TABS[surface]:
            self._show_tab(self._current_tab)

    def _dock(self, surface: str, *, show_it: bool = True) -> None:
        """Put a surface back in the tab strip.

        `show_it` is what tells the two callers apart, and they are asking
        different things. Pressing "Bring it back into this tab" IS a request
        to look at the surface, so it goes there. Closing the window with its
        title-bar X is a request to get it off the screen, and answering that
        by jumping someone off the Create tab they were typing in onto the tab
        they had just tried to be rid of is the opposite of what they asked.

        The surface's tab is still rebuilt where it is the one on screen,
        whoever asked: it is showing the note saying the surface is somewhere
        else, and that note stopped being true on the line above.
        """
        self._torn_out[surface] = False
        prefs.set_bool(prefs.torn_out_key(surface), False)
        self._close_window(surface)
        tab = SURFACE_TABS[surface]
        if show_it or self._current_tab == tab:
            self._show_tab(tab)

    def _open_window(self, surface: str) -> None:
        """Build one surface into a window of its own.

        A window rather than a second panel, because Kit windows dock and float
        on their own: someone who wants the library beside the Create form gets
        that by dragging its title bar, and this code does not have to know
        anything about where they put it.
        """
        window = self._windows.get(surface)
        if window is None:
            window = ui.Window(
                SURFACE_WINDOW_TITLES[surface], width=460, height=720
            )
            self._windows[surface] = window
            # Closing it with the title bar's X is the same request as pressing
            # Bring it back, and a window the user has shut must not leave the
            # tab claiming the surface is somewhere else.
            window.set_visibility_changed_fn(
                lambda visible, s=surface: self._on_window_visibility(s, visible)
            )
        else:
            # Asked for again: Show the window, or a sign-out and back in that
            # rebuilds the workbench. A window's frame holds one child, so
            # building into it again replaces what was there, and the frames
            # from the previous body would be left as references to widgets
            # nothing can reach.
            self._drop_surface_frames(surface)
        with window.frame:
            self._build_surface_body(surface, in_window=True)
        window.visible = True

    def _close_window(self, surface: str) -> None:
        window = self._windows.pop(surface, None)
        if window is None:
            return
        self._drop_surface_frames(surface)
        # Unsubscribed first: destroying a visible window reports a visibility
        # change, and answering it would dock the surface a second time.
        window.set_visibility_changed_fn(None)
        window.destroy()

    def _on_window_visibility(self, surface: str, visible: bool) -> None:
        """A window was shut with its own X, which docks its surface.

        Deferred a frame like every other move, and here the reason is sharper
        than a container clear: this IS the window's visibility callback, so
        docking inline would call `destroy()` on the window whose dispatch is
        still on the stack, and then rebuild a tab body underneath it.
        Unsubscribing first stops the handler being re-entered; it does not
        make destroying a window mid-dispatch safe.
        """
        if visible or not self._torn_out[surface]:
            return
        self._request_layout(surface, lambda: self._dock(surface, show_it=False))

    def _rebuild_surface(self, surface: str) -> None:
        """Show a surface's contents again, wherever it happens to be living.

        Every caller that drops what a surface was HOLDING has to come through
        here, because dropping the held pages redraws nothing by itself. A
        docked surface is redrawn by rebuilding its tab; one in a window is
        redrawn into the window, which no tab switch can reach.

        Both halves have been bugs. The window half was an account switch
        leaving the previous account's thumbnails on screen under a footer
        naming the new one; the tab half was the same thing one tab over, and
        the same again after sending a render to a torn-out Uploads.

        Nothing is rebuilt for a docked surface that is not the tab on screen:
        its body is built fresh on the next visit anyway.
        """
        if self._torn_out[surface]:
            # Only where a window actually exists. Torn out is remembered
            # across sessions, so the preference can say yes before the
            # workbench has built anything.
            if self._windows.get(surface) is not None:
                self._open_window(surface)
            return
        if self._current_tab == SURFACE_TABS[surface]:
            self._show_tab(self._current_tab)

    def _build_surface_body(self, surface: str, *, in_window: bool) -> None:
        """One surface, built into whichever container is open."""
        if surface == SURFACE_LIBRARY:
            self._build_library_body(in_window=in_window)
        else:
            self._build_uploads_body(in_window=in_window)

    def _build_library_tab(self) -> None:
        """The Library tab: the library itself, or a note saying where it is."""
        if self._torn_out[SURFACE_LIBRARY]:
            self._build_elsewhere_notice(SURFACE_LIBRARY)
            return
        self._build_library_body(in_window=False)

    def _build_pop_out_button(self, surface: str) -> None:
        """Offered from the tab only. In the window the title bar's X is the
        same request, and a button repeating it is clutter.

        One method for both surfaces, which is what keeps the label, the icon,
        the height and the deferral from being decided twice and drifting.

        The icon is skipped rather than the button where the asset is missing.
        A stripped or half-copied install should still be able to pop the
        surface out; losing the picture costs recognition, losing the button
        costs the feature.
        """
        icon = (
            {
                "image_url": str(OPEN_IN_NEW_ICON),
                "image_width": ICON_SIZE,
                "image_height": ICON_SIZE,
                # Between the icon and the words. Without it they touch.
                "spacing": 6,
                "style": {
                    # Beside the words, not above them. omni.ui stacks a
                    # button's image and its text TOP_TO_BOTTOM by default, so
                    # the first version of this put the icon on its own line
                    # and left a two-line button with a picture stranded over
                    # the label. Reported off the branch. Kit's own icon
                    # buttons set this for the same reason: see the button
                    # style in omni.kit.window.file's read_only_options_window.
                    #
                    # The alignments are left at their defaults on purpose, so
                    # the icon and the words centre as a pair the way the text
                    # does on every other button in this panel.
                    "Button": {"stack_direction": ui.Direction.LEFT_TO_RIGHT},
                    "Button.Image": {"color": ICON_TINT},
                },
            }
            if OPEN_IN_NEW_ICON.exists()
            else {}
        )
        ui.Button(
            "Pop out into its own window",
            height=22,
            clicked_fn=lambda: self._request_layout(
                surface, lambda: self._tear_out(surface)
            ),
            **icon,
        )

    def _build_elsewhere_notice(self, surface: str) -> None:
        """What a tab shows while its surface is in its own window.

        The tab stays in the strip rather than disappearing. A tab that comes
        and goes moves the three beside it, and someone who tore a surface out
        and forgot has to be told where it went by something.
        """
        with ui.VStack(spacing=8, height=0):
            ui.Label(SURFACE_ELSEWHERE[surface], word_wrap=True, height=0)
            ui.Label(
                "Drag its title bar to dock it beside this panel, or bring it "
                "back into this tab.",
                word_wrap=True,
                height=0,
            )
            ui.Button(
                "Bring it back into this tab",
                height=24,
                clicked_fn=lambda: self._request_layout(
                    surface, lambda: self._dock(surface)
                ),
            )
            ui.Button(
                "Show the window",
                height=24,
                clicked_fn=lambda: self._request_layout(
                    surface, lambda: self._open_window(surface)
                ),
            )

    def _build_library_body(self, *, in_window: bool) -> None:
        """The library surface itself, built into whatever container is open.

        One surface, in one place at a time: the tab or the window, never both.
        That is what lets the frames it fills stay single attributes rather
        than becoming another thing keyed by where it is.
        """
        with ui.VStack(spacing=8):
            if not in_window:
                # Offered only from the tab. In the window, the title bar's X
                # is the same request and a button repeating it is clutter.
                self._build_pop_out_button(SURFACE_LIBRARY)

            with ui.VStack(height=0, spacing=6):
                # This session comes FIRST and is its own box, because renders
                # made here are not in the server library at all and never will
                # be. Listing them together would imply otherwise.
                self._session_frame = ui.CollapsableFrame(
                    self._session_title(), collapsed=False, height=0
                )
                # The contents are a plain Frame INSIDE the collapsable one,
                # and that indirection is load-bearing rather than tidy. A
                # CollapsableFrame owns a header and a body and builds them when
                # it is constructed; re-entering it later from another method to
                # rebuild its child is the one thing in this panel that did not
                # draw. Every frame the panel redraws on a timer or a callback is
                # a plain Frame for exactly this reason, and this one is now too.
                with self._session_frame:
                    self._session_body = ui.Frame(height=0)
                self._render_session_section()

                ui.Label("YOUR LIBRARY · SAVED TO YOUR ACCOUNT", height=0)

                # Filters, not a search. The library surface is explicit that
                # its parameters are a FILTER and carry no free-text query, so a
                # search box would promise something the API does not do.
                #
                # Collapsed, and open only when something is set, for the same
                # reason the web client puts them behind a button: seven
                # controls standing open above the grid leave a docked panel
                # showing one row of pictures.
                self._filters_frame = ui.CollapsableFrame(
                    self._filters_title(),
                    collapsed=not self._library_filters.is_active,
                    height=0,
                )
                with self._filters_frame:
                    self._filters_body = ui.Frame(height=0)
                # The draft starts from what is actually applied. A control left
                # part-way through a change and abandoned by switching tabs must
                # not come back looking as though it had been applied.
                self._library_draft = replace(self._library_filters)
                self._render_filters()

            with ui.ScrollingFrame():
                self._library_frame = ui.Frame(height=0)

            with ui.VStack(height=0, spacing=6):
                self._library_pager = ui.Frame(height=0)
                self._asset_actions_frame[SURFACE_LIBRARY] = ui.Frame(height=0)
                self._render_asset_actions(SURFACE_LIBRARY)

            # Redrawn from what is already held rather than refetched. The tab
            # is rebuilt on every visit, and someone who paged deep into their
            # history and stepped away for one generate should find it as they
            # left it.
            if self._library_items:
                self._show_library_items(self._library_items, append=False)
            else:
                with self._library_frame:
                    ui.Label("Loading...")
                self._spawn(self._load_library(reset=True))

    # -- the library filters ------------------------------------------------

    def _filters_title(self) -> str:
        """Named by how many filters are on, because the panel is usually shut.

        Without the count, a library narrowed to one tool in one aspect reads as
        a library with four things in it.
        """
        count = self._library_filters.active_count
        return f"Filters · {count} on" if count else "Filters"

    def _render_filters(self) -> None:
        """Ask for the filter panel to be redrawn on the next frame.

        Every caller is a click handler: Apply, Clear all, the Clear beside the
        tool, and the tool browser's pick. Rebuilding the frame from inside the
        event that is still being dispatched is what omni.ui refuses, and this
        is the panel where it was actually caught doing it:

            [Error] [omni.ui] Container::clear was called during an event or
            draw, this is not supported
              self._apply_filters()
              self._render_filters()
              frame.clear()
        """
        self._redraws.request("library:filters", self._render_filters_now)

    def _render_filters_now(self) -> None:
        """The filter controls, drafted here and committed on Apply.

        Every control writes to `_library_draft` and to nothing else. Apply is
        what copies the draft over the live filters and refetches, which is both
        what the web client does and what stops seven controls costing seven
        round trips on the way to one query.
        """
        frame = self._filters_body
        if frame is None:
            return
        if self._filters_frame is not None:
            try:
                self._filters_frame.title = self._filters_title()
            except Exception:  # noqa: BLE001 - a stale title beats a dead tab
                logger.debug("RunDiffusion: could not retitle the filter frame.")
        frame.clear()

        draft = self._library_draft
        families = self._tool_tags.get(tool_tags_api.TYPE_MODEL_FAMILY, [])
        media = self._tool_tags.get(tool_tags_api.TYPE_MEDIA, [])

        with frame:
            with ui.VStack(spacing=4, height=0):
                # The tool filter is a button into the tool browser rather than
                # a dropdown. The catalogue runs to a couple of hundred entries,
                # and a ComboBox that long is a list you scroll rather than a
                # filter you use. The browser already searches it and groups it
                # by kit, which is the control this needs and the one the web
                # client's autocomplete stands in for.
                with ui.HStack(height=22, spacing=6):
                    ui.Label("Tool", width=FILTER_LABEL)
                    ui.Button(
                        self._tool_filter_label(draft.tool_id),
                        clicked_fn=self._choose_filter_tool,
                    )
                    clear_tool = ui.Button(
                        "Clear", width=52, clicked_fn=self._clear_filter_tool
                    )
                    clear_tool.enabled = bool(draft.tool_id)

                self._tag_filter_row(
                    "Model family",
                    families,
                    draft.model_family_tool_tag_id,
                    lambda value: setattr(
                        self._library_draft, "model_family_tool_tag_id", value
                    ),
                )
                self._tag_filter_row(
                    "Media",
                    media,
                    draft.media_tool_tag_id,
                    lambda value: setattr(self._library_draft, "media_tool_tag_id", value),
                )
                self._choice_filter_row(
                    "Aspect ratio",
                    assets_api.ASPECT_CHOICES,
                    draft.aspect_ratio,
                    lambda value: setattr(self._library_draft, "aspect_ratio", value),
                )
                self._choice_filter_row(
                    "Resolution",
                    assets_api.RESOLUTION_CHOICES,
                    draft.resolution,
                    lambda value: setattr(self._library_draft, "resolution", value),
                )
                self._choice_filter_row(
                    "Created",
                    assets_api.DATE_CHOICES,
                    draft.within_days,
                    lambda value: setattr(self._library_draft, "within_days", value),
                )
                self._choice_filter_row(
                    "Favorites",
                    assets_api.FAVORITE_CHOICES,
                    draft.favorited,
                    lambda value: setattr(self._library_draft, "favorited", bool(value)),
                )

                with ui.HStack(height=22, spacing=6):
                    ui.Button("Apply", clicked_fn=self._apply_filters)
                    clear_all = ui.Button("Clear all", clicked_fn=self._clear_filters)
                    clear_all.enabled = (
                        self._library_draft.is_active or self._library_filters.is_active
                    )

    def _tag_filter_row(self, label: str, tags: list, selected_id, on_change) -> None:
        """One taxonomy as a dropdown, or a line saying there is not one.

        A vocabulary that did not arrive is said rather than drawn as an empty
        control: a dropdown holding only "Any" looks like an account with no
        model families in it, which is never what happened.
        """
        if not tags:
            with ui.HStack(height=22, spacing=6):
                ui.Label(label, width=FILTER_LABEL)
                ui.Label("Unavailable", width=0)
            return
        choices = ((f"Any {label.lower()}", None), *((str(tag), tag.id) for tag in tags))
        self._choice_filter_row(label, choices, selected_id, on_change)

    def _choice_filter_row(self, label: str, choices, selected, on_change) -> None:
        """A labelled ComboBox over `(label, value)` pairs, reporting the VALUE.

        The value is what the API takes, so the mapping from row to parameter
        happens once, here, rather than being redone wherever the request is
        built.
        """
        values = [value for _text, value in choices]
        index = values.index(selected) if selected in values else 0
        with ui.HStack(height=22, spacing=6):
            ui.Label(label, width=FILTER_LABEL)
            combo = ui.ComboBox(index, *[text for text, _value in choices])

            def _changed(*_args, values=values, on_change=on_change, combo=combo) -> None:
                chosen = combo.model.get_item_value_model().as_int
                if 0 <= chosen < len(values):
                    on_change(values[chosen])

            combo.model.add_item_changed_fn(_changed)

    def _tool_filter_label(self, tool_id: str | None) -> str:
        if not tool_id:
            return "All tools"
        for tool in self._tools:
            if tool.id == tool_id:
                return tool.name or tool.id
        # Filtered on a tool that is not in the catalogue this account can see.
        # The id is still a valid filter, so it is named by what is known.
        return tool_id

    def _choose_filter_tool(self) -> None:
        if not self._tools:
            self._set_error("The tool catalogue has not loaded yet.")
            return

        def _picked(tool) -> None:
            self._library_draft.tool_id = tool.id
            self._render_filters()

        self._tool_browser.show(self._tools, _picked, kits=self._kits)

    def _clear_filter_tool(self) -> None:
        self._library_draft.tool_id = None
        self._render_filters()

    def _apply_filters(self) -> None:
        """Commit the draft and start the result set again.

        Everything already loaded is dropped, because it was loaded under the
        OLD query: keeping it would leave the grid showing images that do not
        match the filters that are on.
        """
        self._library_filters = replace(self._library_draft)
        self._render_filters()
        self._spawn(self._load_library(reset=True))

    def _clear_filters(self) -> None:
        self._library_draft = assets_api.LibraryFilters()
        self._apply_filters()

    def _session_title(self) -> str:
        """Named by what is in it AND what is still coming.

        The count of runs in flight belongs in the title because this frame is
        where they will land, and a person who hid a run needs to see that it is
        still on its way without opening anything.
        """
        parts = []
        count = len(self._session_results)
        if count:
            parts.append(f"{count} unsaved")
        running = self._jobs.running_count
        if running:
            parts.append(f"{running} running")
        return f"This session · {' · '.join(parts)}" if parts else "This session"

    def _render_session_section(self) -> None:
        """Ask for this to be redrawn on the next frame.

        Reached from Hide and from Clear finished, both of which change what
        this section is showing.

        omni.ui refuses a container clear during event dispatch, so the rebuild
        waits a frame. See redraw.py, and `_render_filters` for the trace that
        proved it.

        The guard is repeated here rather than left to the render alone: with
        nothing to draw into there is no reason to book a frame at all.
        """
        if self._session_body is None:
            return
        self._redraws.request("library:session", self._render_session_section_now)

    def _render_session_section_now(self) -> None:
        """Renders made since Kit started, and the fact that they are temporary.

        Its own frame with a count and a Save all button, because the collection
        that can be LOST needs to look different from the one that cannot. A
        paragraph of explanation under a heading was doing that job before, and
        a paragraph is not a warning.
        """
        frame = self._session_body
        if frame is None:
            return
        if self._session_frame is not None:
            try:
                self._session_frame.title = self._session_title()
            except Exception:  # noqa: BLE001 - a stale title beats a dead tab
                logger.debug("RunDiffusion: could not retitle the session frame.")
        frame.clear()
        running = [job for job in self._jobs.jobs() if not job.is_finished]
        results = list(reversed(self._session_results))
        with frame:
            with ui.VStack(spacing=6, height=0):
                if not running and not results:
                    ui.Label(
                        "Renders made in this Kit session appear here. They are "
                        "not added to your library, so save the ones you want.",
                        word_wrap=True,
                        height=0,
                    )
                    return

                ui.Label(
                    "Not in your library and lost when Kit closes. Save the "
                    "ones you want to keep.",
                    word_wrap=True,
                    height=0,
                )
                self._build_session_grid(running, results)

                selected = self._selected_result
                if selected is not None and selected in self._session_results:
                    ui.Label(
                        f"Selected: {selected.tool_name} at "
                        f"{selected.created:%H:%M}",
                        word_wrap=True,
                        height=0,
                    )
                    self._build_result_buttons(selected)
                if results:
                    with ui.HStack(height=22, spacing=4):
                        ui.Button("Save all", clicked_fn=self._save_all_results)
                        ui.Button(
                            "Open Create tab",
                            clicked_fn=lambda: self._show_tab(TAB_CREATE),
                        )

    def _build_session_grid(self, running: list, results: list) -> None:
        """Runs in flight and finished renders, in one grid, newest first.

        A run in flight gets a CELL rather than a line of text above the grid.
        It is the same thing at an earlier stage, and a cell is what makes the
        wait legible: the tile is already in the position its render will take,
        so nothing appears from nowhere and nothing has to be counted.

        Bounded and scrolling, because this frame sits above the library
        filters. An unbounded grid here would push the library itself off the
        tab after a dozen renders, which is the opposite of the point.
        """
        cells = len(running) + len(results)
        rows = max(1, -(-cells // SESSION_COLUMNS))
        height = min(rows, SESSION_VISIBLE_ROWS) * (SESSION_CELL + 4)
        with ui.ScrollingFrame(height=height):
            with ui.VGrid(
                column_count=SESSION_COLUMNS, row_height=SESSION_CELL + 4, height=0
            ):
                for job in running:
                    self._build_running_cell(job)
                for result in results:
                    self._build_result_thumbnail(
                        result, result is self._selected_result, size=SESSION_CELL
                    )

    def _build_running_cell(self, job) -> None:
        """A tile for a run that has not landed yet, in the server's own words.

        No spinner and no bar. The server sends a state word and a poll
        interval, so the word is what is actually known, and a bar would be
        drawing a number nobody sent.
        """
        with ui.ZStack(width=SESSION_CELL, height=SESSION_CELL):
            ui.Rectangle(style={"background_color": _RUNNING_CELL})
            with ui.VStack(spacing=2):
                ui.Spacer()
                ui.Label(
                    job.tool_name,
                    height=0,
                    word_wrap=True,
                    alignment=ui.Alignment.CENTER,
                )
                if job.prompt:
                    # Under the tool, because the tool says what kind of thing
                    # is coming and the prompt says which one. Two runs of one
                    # tool are otherwise identical tiles.
                    ui.Label(
                        shorten(job.prompt, PROMPT_PREVIEW_CHARS),
                        height=0,
                        word_wrap=True,
                        alignment=ui.Alignment.CENTER,
                        style=_STYLE_HELPER,
                    )
                ui.Label(
                    job.detail or "working",
                    height=0,
                    word_wrap=True,
                    alignment=ui.Alignment.CENTER,
                    style=_STYLE_HELPER,
                )
                ui.Spacer()

    def _save_all_results(self) -> None:
        """Write every session render into one folder, named by the run.

        One dialog for the folder rather than one per image: the point of the
        button is to rescue a session in a single decision.
        """
        if not self._session_results:
            return

        def _write_all(target: Path) -> None:
            folder = target.parent
            written = 0
            for result in self._session_results:
                # The file's own suffix, not `.png`. This branch keeps video and
                # 3D results, and Save all was the one path still writing every
                # one of them as a picture.
                name = (
                    f"rundiffusion-{result.request_id[:8]}"
                    f"-{result.created:%H%M%S}{result.path.suffix}"
                )
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(result.path, folder / name)
                    written += 1
                except OSError:
                    logger.exception("RunDiffusion: could not save %s.", name)
            self._set_status(f"Saved {written} of {len(self._session_results)} to {folder}")

        # Only the folder is taken from this dialog; every file inside it is
        # named from its own result. The suffix here is what the dialog opens
        # on, and a session is a mixture, so it opens on the commonest thing.
        suggested = "rundiffusion-session.png"
        if not saving.open_save_dialog(suggested_name=suggested, on_chosen=_write_all):
            _write_all(Path.home() / "Pictures" / "RunDiffusion" / suggested)

    @staticmethod
    def _merge_page(held: list, arriving: list) -> list:
        """`held` plus whatever in `arriving` is not already in it.

        Deduplicated by id because a keyset page can repeat a row: two results
        generated in the same instant order arbitrarily against each other, so
        the last row of one page can also be the first row of the next. Without
        this the grid shows the same picture twice and the count above it is
        wrong by however many times that happened.
        """
        seen = {asset.id for asset in held}
        return held + [asset for asset in arriving if asset.id not in seen]

    async def _load_library(self, *, reset: bool) -> None:
        """One page of the library. `reset` starts over; otherwise it adds.

        Adding rather than replacing is the whole point of Load more: the page
        you were looking at stays on screen, so going deeper into a history
        feels like going deeper rather than like being moved.
        """
        # A Load more while one is already in flight would spend the same
        # cursor twice. A RESET is never dropped, though: it is a new query,
        # and refusing it because the old one is still running is how Apply
        # came to do nothing when it was pressed early.
        if not reset and self._library_loading:
            return
        if reset:
            self._library_generation += 1
            self._library_items = []
            self._library_cursor = None
            self._library_has_more = False
            self._selected_asset.pop(SURFACE_LIBRARY, None)
            self._render_asset_actions(SURFACE_LIBRARY)
        generation = self._library_generation
        cursor = self._library_cursor
        self._library_loading = True
        self._render_pager(
            self._library_pager,
            count=len(self._library_items),
            has_more=self._library_has_more,
            loading=True,
            load_more=self._load_more_library,
            refresh=self._refresh_library,
        )
        try:
            page = await assets_api.list_library(
                self._session,
                PAGE_SIZE,
                cursor=cursor,
                filters=self._library_filters,
            )
        except ApiError as error:
            if generation != self._library_generation:
                return
            self._render_library_error(f"Could not load your library. {error.message}")
            return
        finally:
            # Only the newest query owns the flag. An older one finishing later
            # must not declare the panel idle while its replacement is running.
            if generation == self._library_generation:
                self._library_loading = False

        if generation != self._library_generation:
            # Answered a query nobody is looking at any more.
            return

        added = self._merge_page(self._library_items, page.items)
        arrived = added[len(self._library_items):]
        self._library_items = added
        self._library_cursor = page.next_cursor
        self._library_has_more = bool(page.has_more and page.next_cursor)
        self._show_library_items(arrived if cursor else added, append=bool(cursor))

    def _render_library_error(self, message: str) -> None:
        """Say what went wrong without discarding what is already on screen.

        A page that failed to load is not a reason to throw away the six that
        did, so the message goes in the pager line rather than over the grid,
        unless the grid is empty and the message is all there is to show.
        """
        if self._library_items:
            self._render_pager(
                self._library_pager,
                count=len(self._library_items),
                has_more=self._library_has_more,
                loading=False,
                load_more=self._load_more_library,
                refresh=self._refresh_library,
                message=message,
            )
            return
        frame = self._library_frame
        if frame is None:
            return
        frame.clear()
        with frame:
            ui.Label(message, word_wrap=True)

    def _show_library_items(self, assets: list, *, append: bool) -> None:
        frame = self._library_frame
        if frame is None:
            return
        empty = (
            "Nothing matches those filters."
            if self._library_filters.is_active
            else "Nothing in your library yet."
        )
        if not append:
            # A full redraw destroys every cell, so the thumbnails still
            # in the air for them have nowhere legitimate to land.
            self._grid_for(SURFACE_LIBRARY).cancel_pending()
        self._grid_for(SURFACE_LIBRARY).build(
            frame,
            assets,
            on_pick=lambda asset: self._on_asset_clicked(asset, SURFACE_LIBRARY),
            actions_for=self._asset_cell_actions,
            empty_message=empty,
            append=append,
        )
        self._render_pager(
            self._library_pager,
            count=len(self._library_items),
            # Read rather than assumed. This also runs when the tab is rebuilt
            # from held items, which can happen while a page is still in the
            # air, and a Load more that looks enabled but is dropped on click
            # is worse than one that is visibly waiting.
            has_more=self._library_has_more,
            loading=self._library_loading,
            load_more=self._load_more_library,
            refresh=self._refresh_library,
        )

    def _load_more_library(self) -> None:
        self._spawn(self._load_library(reset=False))

    def _render_pager(
        self,
        pager: ui.Frame | None,
        *,
        count: int,
        has_more: bool,
        loading: bool,
        load_more,
        refresh,
        message: str | None = None,
    ) -> None:
        """How much is on screen, and the buttons that change it.

        NOT "1-24 of 312": these endpoints are cursor-paginated and publish no
        total, so a total would be a number this panel made up. What it can say
        honestly is how many are loaded and whether the server says there are
        more, which is what Load more is enabled by.

        Refresh is here because these grids are now held rather than refetched
        on every visit. That is what makes paging deep survive a tab switch, and
        it also means an image added from somewhere else (the web app, or this
        panel's own file upload) will not appear on its own. Refresh is the way
        to ask, and it is explicit rather than automatic because the cost of
        being wrong is a history the user paged through being thrown away.
        """
        if pager is None:
            return
        pager.clear()
        with pager:
            with ui.VStack(height=0, spacing=4):
                with ui.HStack(height=22, spacing=6):
                    shown = f"Showing {count}"
                    if has_more:
                        shown += " so far"
                    ui.Label(shown, word_wrap=True)
                    again = ui.Button("Refresh", width=70, clicked_fn=refresh)
                    again.enabled = not loading
                    more = ui.Button(
                        "Loading..." if loading else "Load more",
                        width=90,
                        clicked_fn=load_more,
                    )
                    # Disabled while a page is in flight as well as when there
                    # is none to ask for: a second click during the wait would
                    # spend the same cursor twice and land the same page twice.
                    more.enabled = bool(has_more) and not loading
                if message:
                    ui.Label(message, word_wrap=True, height=0)

    def _refresh_library(self) -> None:
        """Start the library again from the newest page.

        Everything paged in is dropped, which is the honest cost of asking for
        what is there now: a cursor is a position in one answer, and there is no
        way to re-ask for six pages as one request.
        """
        self._spawn(self._load_library(reset=True))

    def _refresh_uploads(self) -> None:
        self._spawn(self._load_uploads(reset=True))

    # -- what you can do with a library or upload item ---------------------

    def _on_asset_clicked(self, asset, surface: str) -> None:
        self._selected_asset[surface] = asset
        self._render_asset_actions(surface)

    def _render_asset_actions(self, surface: str) -> None:
        """Ask for one surface's action row to be redrawn on the next frame.

        Reached from clicking a cell in the Library or Uploads grid.

        omni.ui refuses a container clear during event dispatch, so the rebuild
        waits a frame. See redraw.py, and `_render_filters` for the trace that
        proved it.

        The guard is repeated here rather than left to the render alone: with
        nothing to draw into there is no reason to book a frame at all.

        Booked under a key carrying the surface, so a click in the library and
        a click in uploads do not collapse into one redraw that only one of
        them gets.
        """
        if self._asset_actions_frame.get(surface) is None:
            return
        # Under the surface's own prefix, so a tab switch that spares the
        # library's pending rebuilds spares this one with them.
        self._redraws.request(
            f"{surface}:actions",
            lambda s=surface: self._render_asset_actions_now(s),
        )

    def _render_asset_actions_now(self, surface: str) -> None:
        """The row of actions for the clicked item, with a preview of it.

        The preview is what makes the row unambiguous: at three cells to a row
        and two dozen on screen, a filename alone does not tell you which image
        the buttons are about to act on.
        """
        frame = self._asset_actions_frame.get(surface)
        if frame is None:
            return
        frame.clear()

        asset = self._selected_asset.get(surface)
        if asset is None:
            with frame:
                ui.Label("Click an image to use, download or open it.", word_wrap=True)
            return

        with frame:
            with ui.HStack(height=ASSET_PREVIEW, spacing=8):
                preview = ui.Frame(width=ASSET_PREVIEW, height=ASSET_PREVIEW)
                with ui.VStack(spacing=4):
                    ui.Label("SELECTED", height=0)
                    ui.Label(self._asset_detail(asset), word_wrap=True, height=0)
                    with ui.HStack(height=22, spacing=4):
                        # Use in Create only for a picture. An image field takes
                        # an IMG reference, so offering it on a library video is
                        # a button whose only outcome is a rejected run. Same
                        # rule the session results follow.
                        if asset.is_image:
                            ui.Button(
                                "Use in Create",
                                clicked_fn=lambda a=asset: self._use_asset_as_input(a),
                            )
                        ui.Button(
                            "Download",
                            clicked_fn=lambda a=asset: self._spawn(self._download_asset(a)),
                        )
                        ui.Button(
                            "Open",
                            clicked_fn=lambda a=asset: self._spawn(self._open_asset(a)),
                        )
        # The drawable one, so clicking a video does not download the video to
        # find out it is not a picture. The line beside it already says so.
        url = asset.drawable_preview_url
        if url:
            # Through the grid this surface owns, not whichever grid came to
            # hand. `cancel_pending` bumps one grid's generation and abandons
            # every fetch in the air on it, so loading the library window's
            # preview on the uploads grid meant an unrelated tab switch left
            # that preview permanently blank. The two grids exist to keep the
            # library and uploads apart; this was the one place still crossing
            # them.
            self._grid_for(surface).load_image_into(preview, url, size=ASSET_PREVIEW)

    def _grid_for(self, surface: str):
        """The AssetGrid belonging to one surface.

        One AssetGrid drives one live grid: it holds the VGrid the last build
        made so Load more can append to it, and cancelling abandons the
        thumbnails still arriving on it. So which one a fetch goes through
        decides what a cancel elsewhere is allowed to take with it.
        """
        return self._surface_grids[surface]

    def _asset_cell_actions(self, asset) -> list[tuple]:
        """What a library or uploads cell offers while the pointer is on it.

        The same three actions as the row under the grid and in the same order,
        because they are the same actions: this is that row without the click
        that used to be spent selecting the thing first. Short labels, since
        three buttons share the width of one 96px cell.

        Use is offered on the same rule the row uses. An image field takes an
        image reference, so a video here would be a button whose only outcome
        is a rejected run.

        Each callback takes the asset rather than closing over it. The grid
        holds these for the life of a cell and passing it back is what keeps a
        rebuilt cell from acting on the asset that used to be in its place.
        """
        actions: list[tuple] = []
        if asset.is_image:
            actions.append(("Use", self._use_asset_as_input))
        actions.append(("Save", lambda a: self._spawn(self._download_asset(a))))
        actions.append(("Open", lambda a: self._spawn(self._open_asset(a))))
        return actions

    def _asset_detail(self, asset) -> str:
        """Name, what it is, and origin. No dimensions: none are published.

        The kind is in the line because the grid cannot show it. A video's
        thumbnail is a still, so without a word for it the only way to find out
        is to press a button and watch it do something else.
        """
        origin = (
            "in your library"
            if asset.kind == "LIBRARY_REF"
            else "sent to RunDiffusion"
        )
        kind = "" if asset.is_image else f" · {self._asset_kind_word(asset)}"
        created = f" · {asset.created}" if asset.created else ""
        return f"{asset.name or 'Untitled'}{kind} · {origin}{created}"

    def _build_uploads_tab(self) -> None:
        """The Uploads tab: the uploads themselves, or a note saying where."""
        if self._torn_out[SURFACE_UPLOADS]:
            self._build_elsewhere_notice(SURFACE_UPLOADS)
            return
        self._build_uploads_body(in_window=False)

    def _build_uploads_body(self, *, in_window: bool) -> None:
        """The uploads surface, built into whatever container is open.

        One surface, in one place at a time: the tab or the window, never both.
        That is what lets the frames it fills stay single attributes rather
        than becoming another thing keyed by where it is.
        """
        with ui.VStack(spacing=8):
            if not in_window:
                self._build_pop_out_button(SURFACE_UPLOADS)

            ui.Label(
                "Source images you have sent to RunDiffusion, from this panel "
                "or elsewhere. Click one to use, download or open it.",
                word_wrap=True,
                height=0,
            )
            # No filters here. /uploads takes a limit and a cursor and nothing
            # else, so a From-this-panel or sort dropdown would be a control
            # that cannot do what it says.

            with ui.ScrollingFrame():
                self._uploads_frame = ui.Frame(height=0)

            with ui.VStack(height=0, spacing=6):
                self._uploads_pager = ui.Frame(height=0)
                self._asset_actions_frame[SURFACE_UPLOADS] = ui.Frame(height=0)
                self._render_asset_actions(SURFACE_UPLOADS)

            # Same as the library: what has been paged in is redrawn rather
            # than refetched, so a tab switch does not undo the paging.
            if self._uploads_items:
                self._show_uploads_items(self._uploads_items, append=False)
            else:
                with self._uploads_frame:
                    ui.Label("Loading...")
                self._spawn(self._load_uploads(reset=True))

    async def _load_uploads(self, *, reset: bool) -> None:
        if not reset and self._uploads_loading:
            return
        if reset:
            self._uploads_generation += 1
            self._uploads_items = []
            self._uploads_cursor = None
            self._uploads_has_more = False
        generation = self._uploads_generation
        cursor = self._uploads_cursor
        self._uploads_loading = True
        self._render_pager(
            self._uploads_pager,
            count=len(self._uploads_items),
            has_more=self._uploads_has_more,
            loading=True,
            load_more=self._load_more_uploads,
            refresh=self._refresh_uploads,
        )
        try:
            page = await assets_api.list_uploads(
                self._session, PAGE_SIZE, cursor=cursor
            )
        except ApiError as error:
            if generation != self._uploads_generation:
                return
            self._render_uploads_error(f"Could not load uploads. {error.message}")
            return
        finally:
            if generation == self._uploads_generation:
                self._uploads_loading = False

        if generation != self._uploads_generation:
            return

        added = self._merge_page(self._uploads_items, page.items)
        arrived = added[len(self._uploads_items):]
        self._uploads_items = added
        self._uploads_cursor = page.next_cursor
        self._uploads_has_more = bool(page.has_more and page.next_cursor)
        self._show_uploads_items(arrived if cursor else added, append=bool(cursor))

    def _render_uploads_error(self, message: str) -> None:
        if self._uploads_items:
            self._render_pager(
                self._uploads_pager,
                count=len(self._uploads_items),
                has_more=self._uploads_has_more,
                loading=False,
                load_more=self._load_more_uploads,
                refresh=self._refresh_uploads,
                message=message,
            )
            return
        frame = self._uploads_frame
        if frame is None:
            return
        frame.clear()
        with frame:
            ui.Label(message, word_wrap=True)

    def _show_uploads_items(self, assets: list, *, append: bool) -> None:
        frame = self._uploads_frame
        if frame is None:
            return
        if not append:
            # A full redraw destroys every cell, so the thumbnails still
            # in the air for them have nowhere legitimate to land.
            self._grid_for(SURFACE_UPLOADS).cancel_pending()
        self._grid_for(SURFACE_UPLOADS).build(
            frame,
            assets,
            on_pick=lambda asset: self._on_asset_clicked(asset, SURFACE_UPLOADS),
            actions_for=self._asset_cell_actions,
            empty_message="You have no uploads yet.",
            append=append,
        )
        self._render_pager(
            self._uploads_pager,
            count=len(self._uploads_items),
            has_more=self._uploads_has_more,
            loading=self._uploads_loading,
            load_more=self._load_more_uploads,
            refresh=self._refresh_uploads,
        )

    def _load_more_uploads(self) -> None:
        self._spawn(self._load_uploads(reset=False))

    def _use_asset_as_input(self, asset) -> None:
        """Put this image into the current tool's image field.

        The primary image field, named in the status line rather than guessed at
        silently: a tool with a start frame and an end frame has two, and the
        Create tab is where either can be chosen precisely.
        """
        if not asset.is_image:
            # Reachable only through a stale button, since the row hides it, but
            # the two rules would otherwise have to agree by coincidence.
            self._set_error(
                f"{self._asset_kind_word(asset)} cannot go in an image field."
            )
            return
        tool = self._tool_detail
        if tool is None:
            self._set_error("Pick a tool on the Create tab first.")
            return

        field = tool.image_field
        if field is None:
            self._set_error(f"{tool.name} does not take an image input.")
            return
        if not self._form.has_room(field.key):
            self._set_error(
                f"This run already carries {MAX_INPUT_FILES} images, which is "
                "all one request can. Remove one first."
            )
            return

        slot_id = self._form.add_chosen(
            field.key, asset.as_input_ref(), f"Sending {self._asset_name(asset)}."
        )
        if slot_id is None:
            # `has_room` said yes a moment ago, so this is unreachable. Checked
            # anyway: the two rules would otherwise have to agree by
            # coincidence, and the cost of them not agreeing is an asset
            # recorded against a slot that does not exist.
            self._set_error("That image could not be added.")
            return
        self._chosen_assets[slot_id] = asset
        # The Create tab is not built right now, so the form has state but no
        # widgets. Re-snapshot, or switching back would restore the state from
        # before this was added and drop it. This has to happen BEFORE the tab
        # switch below, which is what rebuilds the form from it.
        self._image_state = self._form.export_image_state()
        # Taken to where the image landed rather than told where it went.
        # The status line naming a tab was the only confirmation, and reading it
        # cost the user a click the picker route never charges: choosing through
        # the field's own picker leaves them looking at the field it filled.
        # Two routes to one outcome that ended somewhere different was the
        # complaint behind this, and it is the half of it that is
        # about agreement rather than about arithmetic.
        #
        # Next frame, not now. This is reached from a click, and one of those
        # clicks is the Use button INSIDE a grid cell: `_show_tab` clears the
        # tab body, which destroys the VGrid, the cell, and the very button
        # still being dispatched. omni.ui refuses a container clear during
        # dispatch whichever container it is (see redraw.py), and this is the
        # version of that where the widget goes out from under the event.
        self._redraws.request("tab", lambda: self._show_tab(TAB_CREATE))
        self._set_status(f"Added to {field.label or 'the image field'}.")

    def _asset_name(self, asset) -> str:
        source = "library" if asset.kind == "LIBRARY_REF" else "upload"
        return asset.name or f"an image from your {source}"

    async def _fetch_asset(self, asset) -> Path | None:
        """Download the FULL file, not the thumbnail, into the session folder.

        Named by what it is. This wrote `asset-<uuid>.png` for everything,
        which is the same defect `_save_to_computer` was fixed for: a library
        video landed on disk as a PNG, and every later step, the viewer, the
        system-viewer hand-off, the save dialog, inherited the lie.
        """
        url = asset.url or asset.thumb_url
        if not url:
            self._set_error("That image has no downloadable URL.")
            return None

        self._set_status("Downloading...")
        try:
            data = await download_async(url)
        except Exception as error:  # noqa: BLE001 - a dead signed URL is normal
            logger.exception("RunDiffusion: could not download an asset.")
            self._set_error(f"Could not download: {error}")
            return None

        path = self._result_dir / f"asset-{uuid.uuid4().hex}{asset.extension}"
        try:
            path.write_bytes(data)
        except OSError as error:
            self._set_error(f"Could not write the download: {error}")
            return None
        self._set_status("")
        return path

    async def _download_asset(self, asset) -> None:
        """Save it where the person chooses, like a result from a run."""
        path = await self._fetch_asset(asset)
        if path is None:
            return

        # From the file that was just written, so the name, the dialog's format
        # and the bytes all agree. Appending `.png` to whatever the listing
        # called it was how an .mp4 came to be suggested as `clip.mp4.png`.
        suggested = asset.name or f"rundiffusion-{_safe_stem(asset.id)}"
        if Path(suggested).suffix.lower() != path.suffix:
            suggested = f"{Path(suggested).stem}{path.suffix}"

        def _write(target: Path) -> None:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                self._set_status(f"Saved to {target}")
            except OSError as error:
                logger.exception("RunDiffusion: could not save an asset.")
                self._set_error(f"Could not save: {error}")

        if not saving.open_save_dialog(suggested_name=suggested, on_chosen=_write):
            # No picker in this host. A known folder beats a button that does
            # nothing, and the status names where it went.
            _write(Path.home() / "Pictures" / "RunDiffusion" / suggested)

    async def _open_asset(self, asset, *, offer_use: bool = True) -> None:
        # Numbered, so stepping quickly through large images cannot land them
        # out of order: only the newest request may put a picture in the viewer.
        self._viewer_request += 1
        request = self._viewer_request
        path = await self._fetch_asset(asset)
        if path is None or request != self._viewer_request:
            return
        if not asset.is_image:
            # The viewer draws images, so a video or a model would open as an
            # empty frame over a row of buttons. The host can play the one and
            # the stage can hold the other, so it goes where it can be seen.
            self._open_path(path)
            self._set_status(
                f"{self._asset_kind_word(asset)} opened outside Kit. "
                "Save it to keep a copy."
            )
            return
        self._view_asset(asset, path, offer_use=offer_use)

    @staticmethod
    def _asset_kind_word(asset) -> str:
        """What to call it in a sentence."""
        if asset.media_kind == generate_api.KIND_VIDEO:
            return "Video"
        if asset.media_kind == generate_api.KIND_ASSET_3D:
            return "3D asset"
        return "File"

    # -- Account tab -------------------------------------------------------

    def _build_account_tab(self) -> None:
        """Who you are, who pays, this install, and the small print.

        Four groups rather than one column of buttons. BILLING names the account
        that pays and sends you to the footer to change it, because the picker
        belongs where it is visible while you spend rather than on a tab you
        have to remember to visit.
        """
        with ui.VStack(spacing=10):
            with ui.VStack(height=0, spacing=10):
                with ui.VStack(height=0, spacing=4):
                    ui.Label("SIGNED IN", height=0)
                    ui.Label(self._session.display_name or "Unknown", height=0)

                with ui.VStack(height=0, spacing=4):
                    ui.Label("BILLING", height=0)
                    accounts = self._session.accounts
                    account = self._session.selected_account
                    if account is not None:
                        ui.Label(
                            "Runs from this panel are billed to "
                            f"{_account_label(account)}.",
                            word_wrap=True,
                            height=0,
                        )
                    if len(accounts) > 1:
                        ui.Label(
                            "Change it with BILLED TO at the bottom of the "
                            "panel, where it is visible from every tab.",
                            word_wrap=True,
                            height=0,
                        )

                    if account is not None and not account.allows_this_plugin:
                        ui.Label(
                            "RunDiffusion for Omniverse is not enabled for this "
                            "account. An account admin can turn it on in team "
                            "settings.",
                            word_wrap=True,
                            height=0,
                        )
                    # No balance figure. /me publishes permissions and accounts,
                    # never a token balance, so a number here would be invented.
                    ui.Button(
                        "Get tokens",
                        height=24,
                        clicked_fn=lambda: webbrowser.open(wall_api.SUBSCRIPTIONS_URL),
                    )

                with ui.VStack(height=0, spacing=4):
                    ui.Label("PANEL", height=0)
                    with ui.HStack(height=22, spacing=6):
                        log_box = ui.CheckBox(width=20)
                        log_box.model.set_value(self._show_status_log)
                        log_box.model.add_value_changed_fn(
                            lambda model: self._set_status_log(
                                model.get_value_as_bool()
                            )
                        )
                        ui.Label("Show the status line", height=0)
                    ui.Label(
                        "A running commentary at the bottom of the panel: what "
                        "was saved, copied, or sent. Off by default because most "
                        "of it repeats what is already on screen. Failures are "
                        "shown either way.",
                        word_wrap=True,
                        height=0,
                        style=_STYLE_HELPER,
                    )

                with ui.VStack(height=0, spacing=4):
                    ui.Label("THIS INSTALL", height=0)
                    ui.Button(
                        "Setup guide",
                        height=24,
                        clicked_fn=lambda: webbrowser.open(constants.ONBOARDING_URL),
                    )
                    # This host is one of the devices that page lists, which is
                    # what makes it worth linking from here.
                    ui.Button(
                        "Manage devices",
                        height=24,
                        clicked_fn=lambda: webbrowser.open(constants.MANAGE_DEVICES_URL),
                    )
                    ui.Button("Sign out", height=24, clicked_fn=self._on_sign_out_clicked)

            ui.Spacer()

            with ui.VStack(height=0, spacing=4):
                ui.Separator(height=4)
                # Reachable from inside the plugin, not only from a web page the
                # user would have to know to go and find. A customer's legal
                # review asks for exactly that.
                with ui.HStack(height=22, spacing=6):
                    ui.Button(
                        "Privacy",
                        clicked_fn=lambda: webbrowser.open(constants.PRIVACY_POLICY_URL),
                    )
                    ui.Button(
                        "Terms",
                        clicked_fn=lambda: webbrowser.open(constants.TERMS_OF_SERVICE_URL),
                    )
                    ui.Button(
                        "Cookies",
                        clicked_fn=lambda: webbrowser.open(constants.COOKIE_POLICY_URL),
                    )
                ui.Label(self._version_line(), word_wrap=True, height=0)

    def _render_results(self) -> None:
        """Ask for this to be redrawn on the next frame.

        Reached from clicking a thumbnail in the strip (`_select_result`) and
        from Clear finished.

        omni.ui refuses a container clear during event dispatch, so the rebuild
        waits a frame. See redraw.py, and `_render_filters` for the trace that
        proved it.

        The guard is repeated here rather than left to the render alone: with
        nothing to draw into there is no reason to book a frame at all.
        """
        if self._results_frame is None:
            return
        self._redraws.request("results", self._render_results_now)

    def _render_results_now(self) -> None:
        """This session's renders, newest first, with actions for the selected one.

        A strip rather than one big preview: generating again used to replace
        the only result on screen, so comparing two attempts meant not making
        the second one.

        Always four square cells, whatever is in them. A run still going takes
        a cell of its own with its state word in it, so a render in flight has
        a visible destination and nothing appears from nowhere when it lands.
        """
        if self._results_frame is None:
            return
        showing = [r for r in self._session_results if not r.cleared]
        running = [job for job in self._jobs.jobs() if not job.is_finished]
        cell = self._result_cell_size()
        self._result_cell = cell
        self._results_frame.clear()
        with self._results_frame:
            if not showing and not running:
                ui.Label("Results appear here.", height=0, style=_STYLE_SECONDARY)
                return

            recent = list(reversed(showing))
            selected = self._selected_result if self._selected_result in recent else None
            if selected is None and recent:
                selected = recent[0]
            pending = running[:RESULT_CELLS]
            finished = recent[: RESULT_CELLS - len(pending)]
            with ui.VStack(spacing=4, height=0):
                with ui.HStack(height=16, spacing=6):
                    ui.Label("RESULTS · THIS SESSION", style=_STYLE_CAPS)
                    ui.Label(
                        "newest first",
                        width=0,
                        alignment=ui.Alignment.RIGHT_CENTER,
                        style=_STYLE_META,
                    )
                with ui.HStack(height=cell, spacing=4):
                    for job in pending:
                        self._build_pending_result_cell(job, cell)
                    for result in finished:
                        self._build_result_thumbnail(result, result is selected, size=cell)
                    for _ in range(RESULT_CELLS - len(pending) - len(finished)):
                        ui.Rectangle(
                            width=cell,
                            height=cell,
                            style={"background_color": _CELL_EMPTY},
                        )
                if selected is None:
                    return
                ui.Label(
                    self._selected_caption(selected),
                    height=0,
                    elided_text=True,
                    style={"font_size": _SIZE_META, "color": _TEXT_SECONDARY},
                )
                self._build_result_strip_actions(selected)

    def _result_cell_size(self) -> int:
        """The edge of one square cell in the results strip, from the panel width.

        omni.ui has no aspect-locked widget, so a square is a width worked out
        here and used as the height too. The fallback is the width a panel is
        usually docked at, for the first frame before anything is laid out.
        """
        width = 0.0
        if self._content is not None:
            try:
                width = float(getattr(self._content, "computed_width", 0) or 0)
            except Exception:  # noqa: BLE001 - a width that cannot be read is the fallback
                width = 0.0
        if width <= 0:
            width = 380.0
        # The scroll bar's gutter and the three gaps between four cells.
        usable = width - 16 - 4 * (RESULT_CELLS - 1)
        return max(48, min(120, int(usable / RESULT_CELLS)))

    def _selected_caption(self, result: SessionResult) -> str:
        """`Selected · <tool> at HH:MM · from <source>`, the last part only
        where the run was given an image to start from."""
        caption = f"Selected · {result.tool_name} at {result.created:%H:%M}"
        source = result_source_name(result.image_state)
        return f"{caption} · from {source}" if source else caption

    def _build_pending_result_cell(self, job, size: int) -> None:
        """The cell a render in flight will land in, with the server's word for it."""
        with ui.ZStack(width=size, height=size):
            ui.Rectangle(style={"background_color": _CELL_EMPTY})
            ui.Label(
                job.detail,
                alignment=ui.Alignment.CENTER,
                word_wrap=True,
                style={"font_size": _SIZE_CAPS, "color": _TEXT_SECONDARY},
            )

    def _build_result_strip_actions(self, result: SessionResult) -> None:
        """Two rows: the verbs every result has, then the ones this one has.

        Save carries the weight, because keeping a render is the reason most
        people press anything here: it is wider and tinted, and the rest stay
        quiet and equal. The seven equal buttons this replaces made every verb
        look like the same size of decision.

        The second row holds what depends on where the result came from: Reuse
        its inputs always, Go to its view when a camera framed it, Add to stage
        when it is a model. Reuse is always there, so the row is never empty.
        """
        specs = {short: action for short, _full, action in self._result_action_specs(result)}
        with ui.HStack(height=26, spacing=4):
            for short in ("Save", "Compare", "Open", "Send"):
                action = specs.get(short)
                if action is None:
                    continue
                weighted = short == "Save"
                ui.Button(
                    short,
                    width=ui.Fraction(1.4 if weighted else 1.0),
                    style=_STYLE_WEIGHTED_BUTTON if weighted else _STYLE_QUIET_BUTTON,
                    clicked_fn=action,
                )
        with ui.HStack(height=24, spacing=4):
            ui.Button(
                "Reuse its inputs",
                style=_STYLE_QUIET_BUTTON,
                clicked_fn=lambda r=result: self._reuse_result(r),
            )
            # Only for a run whose inputs were framed by a camera. A button
            # offering to go back to a place nobody recorded is worse than no
            # button, and most runs have no camera behind them at all.
            if self._view_camera(result) is not None:
                ui.Button(
                    "Go to its view",
                    style=_STYLE_QUIET_BUTTON,
                    clicked_fn=lambda r=result: self._look_through_result(r),
                )
            stage_it = specs.get("Add to stage")
            if stage_it is not None:
                ui.Button("Add to stage", style=_STYLE_QUIET_BUTTON, clicked_fn=stage_it)

    def _build_result_thumbnail(
        self, result: SessionResult, selected: bool, size: int = RESULT_THUMBNAIL
    ) -> None:
        with ui.ZStack(width=size, height=size):
            ui.Rectangle(style={"background_color": _CELL_EMPTY})
            drawable = result.drawable_path
            if drawable is not None:
                ui.Image(str(drawable), fill_policy=ui.FillPolicy.PRESERVE_ASPECT_FIT)
                if result.kind != generate_api.KIND_IMAGE:
                    # The still says what the video looks like and nothing about
                    # how long it is, so the duration stays on top of it. Losing
                    # that was the one thing the old tile did better.
                    self._build_preview_caption(result)
            else:
                # omni.ui draws images and nothing else, so a model, or a video
                # whose still could not be made, gets a tile that says what it
                # is. A blank square would read as a render that failed.
                self._build_non_image_tile(result)
            if selected:
                # An outline rather than a word under the picture: it costs no
                # room in a cell this small, and it is the same mark a selected
                # input image carries in the form.
                ui.Rectangle(
                    style={
                        "background_color": _TRANSPARENT,
                        "border_color": _AMETHYST,
                        "border_width": 2,
                    }
                )
            ui.InvisibleButton(clicked_fn=lambda r=result: self._select_result(r))

    def _build_preview_caption(self, result: SessionResult) -> None:
        """The one fact a still cannot carry, over the bottom of the still."""
        label = result.kind_word
        if result.duration_seconds:
            label = f"{label} · {result.duration_seconds:.0f}s"
        with ui.VStack():
            ui.Spacer()
            ui.Label(label, alignment=ui.Alignment.CENTER, height=0, style=_STYLE_HELPER)

    def _build_non_image_tile(self, result: SessionResult) -> None:
        """What a video or a model looks like in a strip of thumbnails."""
        ui.Rectangle(style={"background_color": _RUNNING_CELL, "border_radius": 4})
        with ui.VStack():
            ui.Spacer()
            ui.Label(
                result.kind_word,
                alignment=ui.Alignment.CENTER,
                height=0,
            )
            ui.Label(
                result.path.suffix.lstrip(".").upper(),
                alignment=ui.Alignment.CENTER,
                style=_STYLE_HELPER,
                height=0,
            )
            if result.duration_seconds:
                ui.Label(
                    f"{round(result.duration_seconds)}s",
                    alignment=ui.Alignment.CENTER,
                    style=_STYLE_HELPER,
                    height=0,
                )
            ui.Spacer()

    async def _add_result_to_stage(self, result: SessionResult) -> None:
        """Put a generated asset on the stage the user has open."""
        self._set_status("Adding to the stage...")
        path = await stage.add_to_stage(result.path, self._result_dir)
        if path is None:
            # The file is still on disk, so saving it is still an option and
            # saying so is more use than naming the API that refused.
            self._set_error(
                "Could not add that to the stage. You can still save it to your "
                "computer."
            )
            return
        # Selected, so "added" is something the user can see rather than take
        # on trust: a referenced asset can land anywhere in a large scene.
        stage.select(path)
        self._set_status(f"Added to the stage at {path} and selected it. Undo removes it.")

    def _view_camera(self, result: SessionResult) -> str | None:
        """The camera a result can send the viewport back to, or None.

        The FIRST of them where a run had several. A tool taking a start frame
        and an end frame was framed from two places, and picking between them
        is a question the button cannot ask; the first input is the one the run
        is mostly about, and the others are still on the stage under their own
        names for anyone who wants them.
        """
        cameras = cameras_in_image_state(result.image_state)
        return cameras[0] if cameras else None

    def _look_through_result(self, result: SessionResult) -> None:
        """Put the viewport back where this result was captured from."""
        camera_path = self._view_camera(result)
        if camera_path is None:
            return
        name = render_cameras.camera_label(camera_path)
        if render_cameras.look_through(camera_path):
            self._set_status(f"Looking through {name}.")
            return
        # The commonest reason by far is that the camera has been deleted since
        # the run, which is the user's own doing and not a fault to apologise
        # for. Said as the fact, in the place every other outcome is said.
        self._set_error(f"{name} is no longer on the stage.")

    def _select_result(self, result: SessionResult) -> None:
        self._selected_result = result
        self._render_results()
        self._render_session_section()

    def _reuse_result(self, result: SessionResult) -> None:
        """Put a finished run's inputs back on the form.

        The values and the image choices are the ones that run was SUBMITTED
        with, captured at the click rather than read back from the form now, so
        reusing a run that was started an hour ago does not pick up edits made
        since.
        """
        if self._tool_detail is None or result.values is None:
            self._set_error("That run's inputs are no longer available.")
            return
        if result.tool_id and result.tool_id != self._tool_detail.id:
            self._set_error(
                f"Switch to {result.tool_name} first: those inputs belong to it."
            )
            return

        # Read before the build, which clears it. The run being reused carries
        # the images it was SUBMITTED with; where the field is pointed is a
        # live setting about the next capture, and the same tool is still on
        # screen, so it stays where the user put it.
        sources = self._form.export_capture_sources()
        self._form.build(
            self._form_frame,
            self._tool_detail,
            dict(result.values),
            capture_container=self._capture_frame,
        )
        self._form.restore_image_state(result.image_state)
        self._form.restore_capture_sources(sources)
        self._form_built = True
        self._form_values = dict(result.values)
        self._image_state = dict(result.image_state or {})
        self._capture_state = sources
        self._sync_viewport_watch()
        self._schedule_cost_refresh()
        self._set_status(f"Reused the inputs from {result.created:%H:%M}.")

    def _save_to_computer(self, result: SessionResult) -> None:
        """A real dialog, so the person picks the name and the folder."""
        # The file's own suffix, not `.png`. A video saved as a .png is a file
        # the operating system will not open and the user cannot fix.
        suggested = f"rundiffusion-{result.request_id[:8]}{result.path.suffix}"

        def _write(target: Path) -> None:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(result.path, target)
                self._set_status(f"Saved to {target}")
            except OSError as error:
                logger.exception("RunDiffusion: could not save a result.")
                self._set_error(f"Could not save: {error}")

        if not saving.open_save_dialog(suggested_name=suggested, on_chosen=_write):
            # No picker in this host. Fall back to a known folder rather than
            # leave a button that does nothing, and name where it went.
            _write(Path.home() / "Pictures" / "RunDiffusion" / suggested)

    async def _use_result_in_create(self, result: SessionResult) -> None:
        """Feed a render back into the current tool as an input image.

        An image field takes a reference to something the server holds, and a
        render is a file on this machine, so it is sent first. That is the same
        upload Send makes, and the render lands in Uploads either way.
        """
        asset = await self._send_to_rundiffusion(result)
        if asset is not None:
            self._use_asset_as_input(asset)

    async def _send_to_rundiffusion(self, result: SessionResult):
        """Upload a render so it outlives the Generate bucket. Returns the upload.

        The copy becomes usable as an input to another generation, which is
        what Use in Create in the compare window does with what this returns.
        None when it did not happen, which has already been said.
        """
        if result.kind not in UPLOADABLE_KINDS:
            # No button reaches this, since the action is only offered for a
            # kind that can be uploaded. Kept because the two rules would
            # otherwise have to agree by coincidence, and a later caller that
            # forgets gets an explanation instead of a server rejection.
            self._set_error(
                "RunDiffusion uploads take images for now, so this one has to "
                "be saved to your computer instead."
            )
            return None

        self._set_status("Sending to RunDiffusion...")
        try:
            asset = await assets_api.upload_image(
                self._session,
                filename=result.path.name,
                content=result.path.read_bytes(),
                mime_type=result.mime_type or "image/png",
            )
        except ApiError as error:
            self._set_error(f"Could not send: {error.message}")
            return None
        except OSError as error:
            self._set_error(f"Could not read the render: {error}")
            return None

        self._set_status("Sent to RunDiffusion. It is in your uploads now.")
        self._forget_uploads()
        # Through the helper, so this reaches Uploads in a window of its own.
        # Rebuilding the tab was enough while a surface could only be a tab: a
        # window is never rebuilt by a visit, so it would have gone on showing
        # a list that no longer had the upload just made in it.
        self._rebuild_surface(SURFACE_UPLOADS)
        return asset

    def _view_result(self, result: SessionResult) -> None:
        """Open a render in the viewer, with what you would do to it next.

        Save and Send to RunDiffusion are here because this is where a person
        decides a render is worth keeping, and both were previously only
        reachable from a strip of 62px thumbnails behind the window they were
        just looking at it in.
        """
        caption = shorten(result.prompt, PROMPT_CAPTION_CHARS)
        if result.kind != generate_api.KIND_IMAGE:
            # Still said, even with a preview on screen. A first frame looks
            # exactly like a picture, and someone who does not know it is a
            # video will wonder why Compare is missing and why saving it gives
            # them an .mp4.
            note = (
                "First frame. Open in system viewer to play it."
                if result.preview_path is not None
                else "Video. Open in system viewer to play it."
            ) if result.kind == generate_api.KIND_VIDEO else (
                "3D asset. Add to stage to see it."
            )
            caption = "\n".join(part for part in (f"{note}  ({result.summary})", caption) if part)

        self._viewer.show(
            # The still where there is one. The viewer draws images, so handing
            # it an .mp4 gave a blank frame under a caption explaining that it
            # was a video, which is a worse way to say the same thing.
            result.drawable_path or result.path,
            title=f"{result.tool_name} · {result.created:%H:%M}",
            caption=caption,
            actions=self._dismissing(self._result_actions(result)),
        )

    def _result_action_specs(self, result: SessionResult) -> list[tuple]:
        """(short label, full label, action) for one result, in display order.

        One definition, because there are three places a result offers its
        actions: the session section, the results strip, and the viewer window.
        The first two were written before a result could be anything but an
        image and offered Compare on a 3D model, which opened a wipe over
        nothing.

        What is offered follows from what the result is:

        - Add to stage leads for a model. It is the reason to generate one
          inside Omniverse rather than anywhere else, and for a model with no
          preview it is also the only way to see it.
        - Open is for a file the operating system can show. There is nothing
          behind it for a model, and exposing one exposes the rest.
        - Compare wipes one picture over another, so it needs a picture.
        """
        save = ("Save", "Save to computer", lambda r=result: self._save_to_computer(r))
        send = (
            "Send",
            "Send to RunDiffusion",
            lambda r=result: self._spawn(self._send_to_rundiffusion(r)),
        )
        open_in_host = (
            "Open",
            "Open in system viewer",
            lambda r=result: self._open_path(r.path),
        )
        compare = ("Compare", "Compare", lambda r=result: self._start_compare(r))
        stage_it = (
            "Add to stage",
            "Add to stage",
            lambda r=result: self._spawn(self._add_result_to_stage(r)),
        )

        if result.kind == generate_api.KIND_ASSET_3D:
            actions = [stage_it, save]
        elif result.kind == generate_api.KIND_VIDEO:
            # Open is what plays it: omni.ui has no widget that can.
            actions = [save, open_in_host]
        else:
            actions = [save, open_in_host, compare]

        # Only where the upload can actually complete. POST /uploads takes
        # images, so offering this on a model puts a button in front of someone
        # whose only outcome is an apology. Widening the endpoint is planned,
        # and this reappears on its own when UPLOADABLE_KINDS grows.
        if result.kind in UPLOADABLE_KINDS:
            actions.append(send)
        return actions

    def _result_actions(self, result: SessionResult) -> list:
        """Full labels, for the viewer window's action row."""
        return [(full, action) for _short, full, action in self._result_action_specs(result)]

    def _dismissing(self, actions: list[tuple]) -> list[tuple]:
        """Wrap viewer actions so the ones that end the look close the window.

        Hidden BEFORE the action runs, which is the ordering the tool browser
        and the asset picker already use: what an action has to say about
        itself belongs over the panel rather than under a window the user has
        finished with. It also means a slow action cannot leave the viewer up
        looking like the click missed.

        Only the viewer's own row is wrapped. The same actions on the results
        strip and in the session grid have no window to close.
        """
        wrapped: list[tuple] = []
        for label, action in actions:
            if label not in VIEWER_DISMISSING_ACTIONS:
                wrapped.append((label, action))
                continue

            def dismiss_then_act(action=action):
                self._viewer.hide()
                action()

            wrapped.append((label, dismiss_then_act))
        return wrapped

    def _build_result_buttons(self, result: SessionResult, height: int = 22) -> None:
        """The same actions as short buttons, for the two compact rows."""
        with ui.HStack(height=height, spacing=4):
            for short, _full, action in self._result_action_specs(result):
                ui.Button(short, clicked_fn=action)

    def _view_asset(self, asset, path: Path, *, offer_use: bool = True) -> None:
        """The same window for something already on the server.

        No Send: it is already there, and a button that re-uploaded a library
        image as a new upload would be making a duplicate, not saving anything.

        `offer_use` is off for the one caller that opened this FROM an image
        field. Use in Create would add the picture the user is looking at to
        the field they are looking at it from, and the run would carry it
        twice.
        """
        actions = [
            ("Save to computer", lambda a=asset: self._spawn(self._download_asset(a))),
        ]
        if asset.is_image and offer_use:
            actions.append(
                ("Use in Create", lambda a=asset: self._use_asset_as_input(a))
            )
        actions.append(("Open in system viewer", lambda p=path: self._open_path(p)))
        # Stepping only for a picture opened from a grid. One opened from an
        # image field is a single input being checked, not a place in a list.
        siblings = self._stepping_list(asset) if offer_use else []
        index = next((i for i, item in enumerate(siblings) if item.id == asset.id), None)
        on_previous = on_next = None
        position = ""
        if index is not None:
            position = f"{index + 1} of {len(siblings)}"
            if index > 0:
                on_previous = lambda a=siblings[index - 1]: self._spawn(self._open_asset(a))
            if index < len(siblings) - 1:
                on_next = lambda a=siblings[index + 1]: self._spawn(self._open_asset(a))
        self._viewer.show(
            path,
            title=asset.name or "RunDiffusion image",
            caption=self._asset_detail(asset),
            actions=self._dismissing(actions),
            on_previous=on_previous,
            on_next=on_next,
            position=position,
        )

    def _stepping_list(self, asset) -> list:
        """The pictures a library or uploads image can be stepped through.

        What that surface has paged in so far, images only: the viewer draws
        pictures, and a video in the sequence would step out of the window into
        the system player. It stops at the last page loaded rather than
        fetching more, so stepping never spends a request the grid did not.
        """
        for items in (self._library_items, self._uploads_items):
            if any(item.id == asset.id for item in items):
                return [item for item in items if item.is_image]
        return []

    def _open_path(self, path: Path) -> None:
        try:
            webbrowser.open(path.resolve().as_uri())
        except Exception as error:  # noqa: BLE001
            logger.exception("RunDiffusion: could not open a result.")
            self._set_error(f"Could not open: {error}")

    # -- compare -----------------------------------------------------------

    def _start_compare(self, result: SessionResult) -> None:
        self._spawn(self._open_compare(result))

    async def _open_compare(self, result: SessionResult) -> None:
        """Open the comparison on a render and the images that run was given.

        Everything is resolved to a file on disk BEFORE the window opens, since
        a library image is a URL and omni.ui draws paths. Few enough to do
        serially: a field holds at most twelve images and most hold one.
        """
        sources: list[tuple[str, Path, str]] = []
        for source in compare_sources(result.image_state):
            path = await self._compare_source_path(source)
            if path is not None:
                sources.append((source.label, path, source.side_label))
        self._compare.show(
            result.path,
            sources,
            title=result.tool_name,
            actions=self._compare_actions(result),
        )

    def _compare_actions(self, result: SessionResult) -> list[tuple[str, Any]]:
        """Save, Send and Use in Create, so judging a render can end in keeping it.

        The same actions the strip offers, not copies of them: Save here writes
        the file Save in the panel writes. Send and Use in Create only where an
        upload can complete, which today means an image.
        """
        actions: list[tuple[str, Any]] = [
            ("Save", lambda r=result: self._save_to_computer(r)),
        ]
        if result.kind in UPLOADABLE_KINDS:
            actions.append(
                ("Send", lambda r=result: self._spawn(self._send_to_rundiffusion(r)))
            )
            # Closes the window as it goes, like every viewer action that takes
            # the user somewhere else: this one ends on the Create tab.
            actions.append(
                (
                    "Use in Create",
                    lambda r=result: (
                        self._compare.hide(),
                        self._spawn(self._use_result_in_create(r)),
                    ),
                )
            )
        return actions

    async def _compare_source_path(self, source) -> Path | None:
        """One input image as a file, at the best size available.

        A capture kept its full bytes, so it is written out at full size rather
        than reusing the thumbnail the form row draws: that one is 256px and
        this is a comparison. The thumbnail is the fallback for a capture whose
        bytes have been let go.
        """
        if source.is_capture:
            if source.png:
                path = self._result_dir / f"compare-{source.slot_id}.png"
                try:
                    if not path.exists():
                        path.write_bytes(source.png)
                    return path
                except OSError:
                    logger.debug("RunDiffusion: could not write a compare image.")
            return Path(source.thumbnail) if source.thumbnail else None

        asset = self._chosen_assets.get(source.slot_id)
        url = asset.preview_url if asset is not None else None
        if not url:
            return None
        return await self._grid.fetch_to_path(url)

    # -- walls -------------------------------------------------------------

    def _clear_wall(self) -> None:
        if self._wall_frame is not None:
            self._wall_frame.visible = False

    def _render_wall(self, wall: wall_api.TokenWall) -> None:
        if self._wall_frame is None:
            return
        self._wall_frame.clear()
        with self._wall_frame:
            with ui.VStack(spacing=4, height=0):
                ui.Label(wall.message, word_wrap=True)
                if wall.action_label and wall.action_url:
                    url = wall.action_url
                    ui.Button(
                        wall.action_label, height=24, clicked_fn=lambda: webbrowser.open(url)
                    )
        self._wall_frame.visible = True

    def _check_plugin_access(self) -> bool:
        """Say so when this account may not use the plugin, rather than letting
        the first gated call fail opaquely.

        /me is exempt from that gate for exactly this reason. The server stays
        the authority, so this decides what to SAY, never what to allow.
        """
        account = self._session.selected_account
        if account is None or account.allows_this_plugin:
            self._clear_wall()
            return True

        who = account.label or ("this team" if not account.is_personal else "this account")
        self._render_wall(
            wall_api.TokenWall(
                message=(
                    f"RunDiffusion for Omniverse is not enabled for {who}. "
                    "An account admin can turn it on in team settings."
                ),
                action_label=None,
                action_url=None,
                variant="plugin_not_enabled",
            )
        )
        return False

    # -- sign-in -----------------------------------------------------------

    def _render_signed_out(self, reason: str = "Not signed in.") -> None:
        """Show the sign-in screen, with `reason` as its status line.

        The reason matters: secure storage being unreadable is NOT the same as
        being signed out, and saying "not signed in" would invite the
        user to re-authenticate to fix something re-authenticating cannot fix.
        """
        self._sign_in_state = reason
        self._device_prompt = None
        self._render_root()
        self._set_error(reason if reason != "Not signed in." else "")

    def _render_signed_in(self) -> None:
        self._device_prompt = None
        self._sign_in_task = None
        self._render_root()
        if not self._check_plugin_access():
            return
        self._spawn(self._load_tools())
        self._spawn(self._load_kits())
        self._spawn(self._load_tool_tags())

    async def _restore_session(self) -> None:
        try:
            if await self._session.restore():
                self._render_signed_in()
            else:
                self._render_signed_out()
        except token_store.SecureStoreUnavailable as error:
            # NOT the same as signed out. Saying "not signed in"
            # would invite the user to re-authenticate to fix something that
            # re-authenticating cannot fix.
            logger.exception("RunDiffusion: the secure store could not be read.")
            self._render_signed_out(f"Secure storage unavailable. {error}")
        except ApiError as error:
            logger.info(
                "RunDiffusion: stored session could not be refreshed. %s", error.message
            )
            self._render_signed_out()
        except Exception:  # noqa: BLE001
            logger.exception("RunDiffusion: restoring the session failed.")
            self._render_signed_out()

    def _on_sign_in_clicked(self, intent: str = session_api.INTENT_SIGN_IN) -> None:
        self._sign_in_task = self._spawn(self._sign_in(intent))

    async def _sign_in(self, intent: str = session_api.INTENT_SIGN_IN) -> None:
        try:
            self._sign_in_intent = intent
            self._sign_in_state = "Asking for a code..."
            self._render_sign_in_steps()

            prompt = await self._session.start_device_flow()

            # Open the browser AND show the code. The browser can fail to open,
            # or open in a profile signed into a different account, and a
            # visible code is what makes that recoverable without a restart.
            self._device_prompt = prompt
            self._sign_in_state = "Waiting for authorization..."
            self._render_sign_in_steps()

            try:
                webbrowser.open(
                    session_api.verification_url_for_intent(
                        prompt.verification_url, intent
                    )
                )
            except Exception:  # noqa: BLE001 - the code on screen is the fallback
                logger.exception("RunDiffusion: could not open the browser.")

            await self._session.await_device_authorization(prompt)
            self._render_signed_in()
        except ApiError as error:
            logger.warning("RunDiffusion: sign-in failed. %s", error.message)
            self._render_signed_out(f"Sign-in failed. {error.message}")
        except asyncio.CancelledError:
            # Cancel already put the screen back; re-rendering here would fight
            # the click that caused it.
            raise
        except Exception as error:  # noqa: BLE001
            logger.exception("RunDiffusion: sign-in failed.")
            self._render_signed_out(f"Sign-in failed. {error}")

    def _on_sign_out_clicked(self) -> None:
        self._spawn(self._sign_out())

    async def _sign_out(self) -> None:
        try:
            await self._session.sign_out()
        finally:
            self._tools = []
            self._kits = []
            self._tool_tags = {}
            self._selected_tool_id = None
            self._tool_detail = None
            self._form_values = {}
            self._image_state = {}
            self._capture_state = {}
            self._forget_browsing()
            self._current_tab = TAB_CREATE
            # The sign-in screen owns the panel's own window, and it has no
            # reach at all into a window the library was torn out into. Left
            # open, that window goes on showing the grid it was built with and
            # its Use, Save and Open buttons go on working against signed URLs
            # that outlive the session, so the person who just signed out is
            # still browsing and still downloading the account they left.
            #
            # Torn out stays remembered: this closes the window, not the
            # preference, so signing back in reopens it where they had it.
            # `_close_window` unsubscribes before destroying, so this does not
            # read as the user docking it.
            for surface in POPPABLE_SURFACES:
                self._close_window(surface)
            # The sign-in screen replaces the whole root, so nothing that would
            # normally turn these off ever runs: no tab switch, no viewfinder
            # render. Left alone, a globally registered USD listener goes on
            # walking the stage on every prim edit for the rest of the session,
            # redrawing image rows that were destroyed with the workbench.
            self._viewport_preview.stop()
            self._camera_watch.stop()
            # Back to the sign-in screen, which now owns the whole window: the
            # tabs would have nothing behind them.
            self._render_signed_out()

    def _on_account_changed(self, *_args) -> None:
        if self._account_combo is None:
            return
        index = self._account_combo.model.get_item_value_model().as_int
        accounts = self._session.accounts
        if not 0 <= index < len(accounts):
            return
        # Through the session rather than by assignment, so choosing an
        # account is also remembering it.
        self._session.choose_account(accounts[index].id)
        # The Account tab spells out who pays, so it is stale the moment this
        # changes. Rebuilt rather than patched, and safe to rebuild from here
        # because the picker that fired this lives in the footer instead.
        if self._current_tab == TAB_ACCOUNT:
            self._show_tab(TAB_ACCOUNT)
        self._clear_wall()
        self._set_cost("")
        # The account decides which tools exist and who pays, so the list is
        # refetched rather than reused.
        self._tools = []
        self._kits = []
        self._tool_tags = {}
        self._selected_tool_id = None
        self._tool_detail = None
        # The library and the uploads belong to the account too, so what has
        # been paged in is dropped rather than left on screen under a new payer.
        # The FILTERS go with it: a tool id or a tag id from one account's
        # catalogue narrows another account's library to nothing.
        self._forget_browsing()
        self._library_filters = assets_api.LibraryFilters()
        self._library_draft = assets_api.LibraryFilters()
        # Dropping the held pages redraws nothing by itself, so without this
        # both surfaces go on showing the previous account's thumbnails, with
        # an action row still holding one of them selected, and Save or Open
        # fetches another account's asset while the footer says this one is
        # paying. The in-cell Save and Open make that reachable without even
        # selecting anything first.
        #
        # BOTH surfaces, and wherever each of them lives: the tab rebuild
        # above only covers the Account tab, so a user sitting on the Library
        # tab was left with the old account's grid whether or not anything was
        # torn out.
        #
        # Before the access check rather than after it: an account this plugin
        # is not enabled for is exactly the case where someone else's library
        # must not be left on screen.
        for surface in POPPABLE_SURFACES:
            self._rebuild_surface(surface)
        if not self._check_plugin_access():
            return
        self._spawn(self._load_tools())
        self._spawn(self._load_kits())
        self._spawn(self._load_tool_tags())

    # -- tools -------------------------------------------------------------

    async def _load_kits(self) -> None:
        """Fetch the curation up front. A failure here is not a failure.

        The browser works without kits: it falls back to the flat catalogue,
        which is the whole reason that fallback exists. So this logs and gives
        up rather than surfacing anything, and the browser will try again on
        its own if it opens with nothing.
        """
        try:
            self._kits = await kits_api.list_kits(self._session)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the flat list still works
            logger.info("RunDiffusion: could not prefetch kits. %s", error)
            self._kits = []

    def _on_tools_page(self, tools: list[tools_api.ToolSummary]) -> None:
        """A page of the catalogue landed. Show what is there so far.

        The draining takes a few round trips, and the browser can be open for
        all of them: someone who signs in and immediately opens it would
        otherwise be searching the first hundred tools forever, because the
        window took a copy of the list at the moment it opened.
        """
        self._tools = tools
        if self._selected_tool_id is None and tools:
            self._selected_tool_id = tools[0].id
        self._tool_browser.update_tools(tools)

    def _forget_uploads(self) -> None:
        """Drop the held pages of uploads, because they no longer list them all.

        Called wherever this panel creates an upload: sending a render, and
        choosing a file for an image field. The tab redraws from what it holds
        rather than refetching on every visit, which is what makes paging deep
        survive a tab switch and what makes this necessary.
        """
        self._uploads_generation += 1
        self._uploads_items = []
        self._uploads_cursor = None
        self._uploads_has_more = False

    def _forget_browsing(self) -> None:
        """Drop every page of library and uploads held for the old account.

        Cursors included: a cursor is issued against one query for one account
        and means nothing outside it, so keeping one would page a new account's
        library from a position in someone else's.
        """
        self._library_generation += 1
        self._library_items = []
        self._library_cursor = None
        self._library_has_more = False
        self._uploads_generation += 1
        self._uploads_items = []
        self._uploads_cursor = None
        self._uploads_has_more = False
        self._selected_asset.clear()

    async def _load_tool_tags(self) -> None:
        """The vocabulary behind the Model family and Media filters.

        No handler here: `list_tool_tags` answers an empty list rather than
        raising, because failure costs those two dropdowns and nothing else. An
        empty vocabulary draws the rows as unavailable, which is the same thing
        this would have done with a message in the log.
        """
        self._tool_tags = tool_tags_api.group_by_type(
            await tool_tags_api.list_tool_tags(self._session)
        )
        if self._current_tab == TAB_LIBRARY:
            self._render_filters()

    async def _load_tools(self) -> None:
        try:
            # Drained in full rather than a page at a time. The browser's search
            # box filters what is loaded, and `/tools` has no text search of its
            # own, so anything short of the whole catalogue makes that box lie.
            self._tools = await tools_api.list_all_tools(
                self._session, on_page=self._on_tools_page
            )
        except ApiError as error:
            logger.warning("RunDiffusion: could not list tools. %s", error.message)
            self._set_error(f"Could not load tools. {error.message}")
            return

        if not self._tools:
            return
        if self._current_tab == TAB_CREATE:
            self._render_tool_picker()
            self._spawn(self._load_tool_detail())

    async def _load_tool_detail(self) -> None:
        summary = self._selected_tool()
        if summary is None or self._form_frame is None:
            return

        # Read what is on screen BEFORE tearing it down, so a tool switch keeps
        # the values whose fieldKey exists in both schemas.
        carry_over = self._form.collect() if self._tool_detail is not None else {}

        self._tool_detail = None
        self._form_frame.clear()
        with self._form_frame:
            ui.Label(f"Loading {summary.name}...")

        try:
            tool = await tools_api.get_tool(self._session, summary.id)
        except ApiError as error:
            self._form_frame.clear()
            with self._form_frame:
                ui.Label(f"Could not load {summary.name}. {error.message}", word_wrap=True)
            return

        self._tool_detail = tool
        # An image chosen for the previous tool belongs to that tool's field, so
        # it goes with it rather than following the user to the next one.
        self._chosen_assets = {}
        self._image_state = {}
        # A camera chosen for the previous tool's field goes with it too. The
        # field it was chosen for does not exist on the next tool, and where a
        # field of the same name does, it is a different question.
        self._capture_state = {}
        self._form.build(
            self._form_frame, tool, carry_over, capture_container=self._capture_frame
        )
        self._form_built = True
        self._sync_viewport_watch()
        # A different tool is a different price, and the line above Generate is
        # the only place that says what this run costs.
        self._schedule_cost_refresh()

        unsupported = tool.unsupported_required_field
        if unsupported is not None:
            self._set_error(
                f"{tool.name} needs a {unsupported.type} input, which this "
                "plugin cannot supply yet."
            )
        else:
            self._set_status("")

    # -- cost --------------------------------------------------------------

    def _schedule_cost_refresh(self, *_args) -> None:
        """Re-price the run shortly, and only once for a burst of edits.

        The cost sits above Generate and has to be true when it is read, which
        means it cannot wait for a button. It also cannot fire per keystroke, so
        every change restarts one short timer.
        """
        if self._cost_task is not None:
            self._cost_task.cancel()
        self._cost_task = self._spawn(self._debounced_cost())

    async def _debounced_cost(self) -> None:
        try:
            await asyncio.sleep(COST_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        await self._preview_cost()

    async def _preview_cost(self) -> None:
        tool = self._tool_detail
        if tool is None or not self._session.is_signed_in:
            self._set_cost("")
            return

        self._clear_wall()
        try:
            result = await generate_api.preview_cost(
                self._session,
                tool,
                self._form.collect(),
                self._requested_results(),
                image_selections=self._form.image_selections(),
            )
        except ApiError as error:
            self._set_cost(f"Cost unavailable. {error.message}")
            return

        total = result.get("total_tokens")
        seconds = result.get("estimated_seconds")
        parts = []
        if total is not None:
            parts.append(f"{total} tokens")
        if seconds:
            # Per result, and said as an estimate: it comes from the tool's
            # average, not from this run.
            parts.append(f"about {seconds}s each")
        # No balance. v2 publishes the price of the run and never the account's
        # remaining tokens, so "N left after this run" would be a number this
        # panel invented.
        self._set_cost(" · ".join(parts) if parts else "")

        account = self._session.selected_account
        wall = wall_api.wall_for_preview(
            can_run=bool(result.get("can_run")),
            blocking_reason=result.get("blocking_reason"),
            is_personal=account.is_personal if account else True,
            email_verified=self._session.email_verified,
        )
        if wall is not None:
            self._render_wall(wall)

    def _on_generate_clicked(self) -> None:
        """Start a run. It stacks rather than replacing whatever is in flight.

        A generate takes tens of seconds, so wanting a second one before the
        first lands is normal. Superseding used to throw away a run the user had
        already been billed for.
        """
        if not self._session.is_signed_in:
            self._set_error("Sign in to generate.")
            return

        tool = self._tool_detail
        if tool is None:
            self._set_error("No tool selected.")
            return

        unsupported = tool.unsupported_required_field
        if unsupported is not None:
            self._set_error(
                f"{tool.name} needs a {unsupported.type} input, which this "
                "plugin cannot supply yet. Pick another tool."
            )
            return

        # The form is read HERE, on the click, not inside the job. The user can
        # edit the form or switch tools while a run is in flight, and a job must
        # submit what was on screen when it was started.
        values = dict(self._form.collect())
        # The images are read on the click too, and in order, because the order
        # decides which file part each capture points at.
        selections = self._form.image_selections()
        # Snapshotted alongside the values, so Reuse puts back the images this
        # run was started with rather than whatever is on screen later.
        image_state = self._form.export_image_state()
        count = self._requested_results()

        self._clear_wall()
        self._jobs.submit(
            tool.name,
            lambda job: self._run_job(job, tool, values, selections, count, image_state),
            prompt=prompt_text(tool, values),
        )

    async def _run_job(self, job, tool, values, selections, count, image_state) -> list:
        """One generation, start to finish. Returns its results.

        No capture happens here. Every image was taken or chosen before the
        click, which is what makes each captured view its own picture rather
        than one reading of the camera shared by all of them.
        """
        planned = tools_api.build_inputs(
            tool, values, image_selections=selections
        )
        missing = tools_api.missing_required_fields(tool, planned)
        if missing:
            names = ", ".join(f.label or f.key for f in missing)
            raise RuntimeError(f"still needs {names}")

        self._jobs.update(job, "submitting")
        try:
            request_id = await generate_api.submit(
                self._session, tool, values, count, image_selections=selections
            )
        except ApiError as error:
            if error.code == "TOOL_SCHEMA_STALE":
                # The tool changed while this form was open. Reload it and ask,
                # rather than resubmitting values built against a schema that no
                # longer exists.
                await self._load_tool_detail()
                raise RuntimeError(
                    "the tool changed while the form was open, so its inputs were reloaded"
                ) from None

            # A token block can appear here even after a clean preview: the
            # balance moves, and preview-cost is advisory rather than a
            # reservation. Same two-axis routing either way.
            account = self._session.selected_account
            wall = wall_api.wall_for_error(
                error.code,
                error.status,
                is_personal=account.is_personal if account else True,
                email_verified=self._session.email_verified,
            )
            if wall is not None:
                self._render_wall(wall)
                raise RuntimeError(wall.message) from None
            raise

        job.request_id = request_id
        self._jobs.update(job, "queued")
        result = await generate_api.poll_until_done(
            self._session,
            request_id,
            on_status=lambda state: self._jobs.update(job, state),
        )

        if not result.succeeded:
            # Name the request id even when the server says nothing useful, so a
            # run that fails with an empty error can still be looked up.
            detail = result.error_message or "the server reported no reason"
            raise RuntimeError(f"{detail} (run {result.request_id})")

        fetched = await generate_api.download_outputs(result, kinds=RECORDABLE_KINDS)
        if not fetched and result.outputs:
            raise RuntimeError(
                "This tool returned a result the panel cannot handle "
                f"(run {result.request_id})"
            )
        recorded = self._record_results(
            fetched,
            result.request_id,
            tool.name,
            tool_id=tool.id,
            values=values,
            image_state=image_state,
            prompt=job.prompt,
        )
        self._render_results()
        self._render_session_section()
        self._announce_results(job, tool.name, len(recorded))
        return recorded

    def _announce_results(self, job, tool_name: str, count: int) -> None:
        """Say where a render went when it did not land in front of the user.

        A run outlives the tab it was started from, and it can be hidden while
        it works, so finishing is often the first the user hears of it. The
        status line is under every tab, which makes it the one place that can
        say so wherever they happen to be.
        """
        if count <= 0:
            return
        if self._current_tab == TAB_CREATE and not job.hidden:
            # It is on screen in the results strip. Saying so would be noise.
            return
        self._set_status(
            f"{tool_name}: {count} render(s) ready, in This session at the top "
            "of the Library tab."
        )

    def _on_jobs_changed(self) -> None:
        """One listener for the queue, because runs show up in two places.

        The Create tab lists them and the Library tab counts them into This
        session. Whichever is on screen gets redrawn; the other one is a None
        frame and costs nothing.
        """
        self._render_jobs()
        # The strip holds a cell for each run still going, so a run starting,
        # changing state or landing is a change to the strip too.
        self._render_results()
        self._render_session_section()

    def _render_jobs(self) -> None:
        """Ask for this to be redrawn on the next frame.

        Reached from Hide, by way of the job queue's change callback.

        omni.ui refuses a container clear during event dispatch, so the rebuild
        waits a frame. See redraw.py, and `_render_filters` for the trace that
        proved it.

        The guard is repeated here rather than left to the render alone: with
        nothing to draw into there is no reason to book a frame at all.
        """
        if self._jobs_frame is None:
            return
        self._redraws.request("jobs", self._render_jobs_now)

    def _render_jobs_now(self) -> None:
        """What is running, one line each, and what has finished, folded to one.

        A muted bar moves under each run still going. It is INDETERMINATE: it
        says that something is happening and nothing about how much. The server
        publishes a state word and a poll interval, never a percentage, so a bar
        that filled would be drawing a number nobody sent.

        Finished runs fold into one counted line carrying Clear finished. A
        failed one keeps a line of its own, because its reason is the one thing
        on this list a person has to read.
        """
        if self._jobs_frame is None:
            return
        jobs = self._jobs.visible_jobs()
        hidden_running = self._jobs.hidden_running_count
        self._jobs_frame.clear()
        self._run_bars = []
        if not jobs and not hidden_running:
            self._sync_run_bar_animation()
            return

        running = [job for job in jobs if not job.is_finished]
        finished = [job for job in jobs if job.is_finished]
        failed = [job for job in finished if job.state == STATE_FAILED]
        with self._jobs_frame:
            with ui.VStack(spacing=4, height=0):
                with ui.HStack(height=16, spacing=6):
                    ui.Label("RUNS", style=_STYLE_CAPS)
                    ui.Label(
                        f"{len(running) + hidden_running} running · "
                        f"{len(self._jobs.jobs())} this session",
                        width=0,
                        alignment=ui.Alignment.RIGHT_CENTER,
                        style=_STYLE_META,
                    )
                for job in running:
                    with ui.VStack(spacing=2, height=0):
                        with ui.HStack(height=18, spacing=6):
                            ui.Label(
                                f"{job.tool_name}: {job.detail}",
                                elided_text=True,
                                style={"font_size": _SIZE_BODY, "color": _TEXT_BODY},
                            )
                            # "Hide", not "Stop". Stop implies cancel and refund
                            # and does neither: the run is submitted and billed.
                            ui.Button(
                                "Hide",
                                width=44,
                                height=18,
                                tooltip=HIDE_TOOLTIP,
                                style=_STYLE_QUIET_BUTTON,
                                clicked_fn=lambda j=job.id: self._jobs.hide(j),
                            )
                        self._build_run_bar()
                for job in failed:
                    ui.Label(
                        job.label,
                        word_wrap=True,
                        height=0,
                        style={"font_size": _SIZE_BODY, "color": _DANGER},
                    )
                if finished:
                    results = sum(len(job.results) for job in finished)
                    with ui.HStack(height=18, spacing=6):
                        ui.Label(
                            f"{len(finished)} finished · {results} "
                            f"result{'' if results == 1 else 's'}",
                            width=0,
                            style={"font_size": _SIZE_BODY, "color": _TEXT_BODY},
                        )
                        ui.Button(
                            "Clear finished",
                            width=0,
                            style=_STYLE_TEXT_BUTTON,
                            clicked_fn=self._on_clear_finished,
                        )
                        ui.Spacer()
        self._sync_run_bar_animation()

    def _build_run_bar(self) -> None:
        """One indeterminate bar: a track, and a segment that travels along it.

        The segment is placed by the width of a spacer in front of it, which is
        what the per-frame tick changes. Nothing is rebuilt to move it.
        """
        with ui.ZStack(height=RUN_BAR_HEIGHT):
            ui.Rectangle(style={"background_color": _RUN_TRACK, "border_radius": 1})
            with ui.HStack():
                lead = ui.Spacer(width=ui.Percent(0))
                ui.Rectangle(
                    width=ui.Percent(RUN_BAR_FRACTION * 100),
                    style={"background_color": _RUN_BAR, "border_radius": 1},
                )
                ui.Spacer()
        self._run_bars.append(lead)

    def _sync_run_bar_animation(self) -> None:
        """Tick while there is a bar on screen, and not a frame longer."""
        if not self._run_bars:
            self._run_bar_subscription = None
            return
        if self._run_bar_subscription is not None:
            return
        import omni.kit.app

        self._run_bar_subscription = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(self._on_run_bar_tick, name="rundiffusion.run-bars")
        )

    def _on_run_bar_tick(self, event) -> None:
        """Move every bar one frame along. It runs left to right and starts over."""
        if not self._run_bars:
            self._run_bar_subscription = None
            return
        try:
            delta = float(event.payload["dt"])
        except Exception:  # noqa: BLE001 - a missing dt is not worth a stall
            delta = 1.0 / 60.0
        self._run_bar_phase = (self._run_bar_phase + delta * RUN_BAR_SPEED) % 1.0
        offset = self._run_bar_phase * (1.0 - RUN_BAR_FRACTION) * 100
        for lead in list(self._run_bars):
            try:
                lead.width = ui.Percent(offset)
            except Exception:  # noqa: BLE001 - a destroyed bar costs one bar
                self._run_bars.remove(lead)

    def _on_clear_finished(self) -> None:
        """Clear the finished runs AND the results strip they produced.

        Clearing a run while its render stayed on screen made the button look
        broken, because the runs list is the smaller half of what Clear is
        obviously pointing at. So it takes both, and nothing is lost by it: the
        renders are still in This session on the Library tab, which is what the
        status line says. That is also why this does not delete anything.
        """
        for result in self._session_results:
            result.cleared = True
        if self._selected_result is not None and self._selected_result.cleared:
            self._selected_result = None
        self._jobs.clear_finished()
        self._render_results()
        self._render_session_section()
        if self._session_results:
            self._set_status(
                "Cleared. Your renders are in This session, at the top of the "
                "Library tab."
            )

    def _write_preview(self, stem: str, data: bytes | None) -> Path | None:
        """Put a first-frame still next to the result it belongs to.

        JPEG rather than the WebP the Runnit thumbnails use, and that is the
        server's choice rather than this panel's: WebP is what Kit's texture
        loader cannot read, which is the same reason library thumbnails are
        re-encoded on the way to disk here.

        None on any failure. A still that will not write costs the picture on a
        tile, and the tile has a fallback.
        """
        if not data:
            return None
        path = self._result_dir / f"result-{stem}-preview.jpg"
        try:
            path.write_bytes(data)
        except OSError:
            logger.info("RunDiffusion: could not write a preview still.")
            return None
        return path

    def _record_results(
        self,
        fetched: list[tuple[Any, bytes]],
        request_id: str,
        tool_name: str,
        tool_id: str | None = None,
        values: dict | None = None,
        image_state: dict | None = None,
        prompt: str = "",
    ) -> list[SessionResult]:
        """Write each output to disk under the name its own format asks for.

        Every result used to be written as `result-<uuid>.png` whatever it was.
        That is only harmless while every tool returns a picture, and the
        browser offers video and 3D tools too.
        """
        recorded = []
        for item in fetched:
            output, data = item.output, item.data
            stem = uuid.uuid4().hex
            path = self._result_dir / f"result-{stem}{output.extension}"
            path.write_bytes(data)
            result = SessionResult(
                request_id,
                path,
                tool_name,
                tool_id,
                values,
                image_state,
                prompt,
                kind=output.kind,
                duration_seconds=output.duration_seconds,
                width=output.width,
                height=output.height,
                size_bytes=output.size_bytes,
                mime_type=output.mime_type,
                preview_path=self._write_preview(stem, item.preview),
            )
            self._session_results.append(result)
            recorded.append(result)
        # The newest result becomes the selected one, because it is the one the
        # user just waited for.
        if recorded:
            self._selected_result = recorded[-1]
        self._render_session_section()
        return recorded
