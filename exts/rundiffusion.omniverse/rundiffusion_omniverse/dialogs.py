"""The two things that need more room than a docked panel has.

The line is drawn explicitly: the panel's usable width floor is small, so
anything that genuinely needs more space opens as a window instead of squeezing
into the column. Exactly two things qualify.

**The tool browser.** A dropdown is fine for picking a tool you already know and
useless for finding one. Tools have avatars, and a grid of them is how a person
recognises the one they want.

**The asset picker.** An image field can be fed from the viewport, from the
library, from uploads, or from a file on disk. That is four sources and a grid,
which is not a thing to nest inside a form.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from pathlib import Path
from typing import Any

import omni.ui as ui

from . import prefs
from . import saving
from .api import assets as assets_api
from .api import kits as kits_api
from .api import tools as tools_api
from .api.assets import Asset
from .api.session import RdSession
from .api.transport import ApiError
from .asset_grid import AssetGrid
from .text import shorten
from .theme import (
    AMETHYST,
    CELL,
    CELL_EMPTY,
    CELL_HOVERED,
    QUIET_BORDER,
    QUIET_FILL,
    SIZE_BODY,
    SIZE_META,
    STYLE_CAPS,
    STYLE_QUIET_BUTTON,
    STYLE_SECONDARY,
    TEXT_BODY,
    TEXT_EMPHASIS,
    TEXT_SECONDARY,
    WEIGHTED_BORDER,
    WEIGHTED_FILL,
)

logger = logging.getLogger(__name__)


def _task_finished(tasks: set):
    """Forget a finished task, and say so if it died.

    Nothing awaits these, so an exception inside one is reported by asyncio as
    "Task exception was never retrieved" and never mentions RunDiffusion. Same
    reasoning as `RunDiffusionPanel._task_finished`, which is where it bit.
    """

    def _done(task) -> None:
        tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "RunDiffusion: a background task failed and nothing was told.",
                exc_info=error,
            )

    return _done

#: Tiles need a ground to read as tiles. Same value as the panel's empty result
#: cell, so the two grids in this plugin sit on the same colour.
#:
#: The two states are written out in full rather than as a base plus an
#: override, because `set_style` REPLACES a widget's style rather than merging
#: into it: a hovered dict holding only the colour would drop the radius and
#: the margin, and the tile would change shape under the pointer.
_STYLE_CARD = {
    "background_color": CELL,
    "border_radius": 4,
    # VGrid has no gutter of its own, so the gap between tiles is the card's.
    "margin": 3,
    # Reserved at rest, in the ground colour, so that lighting it on hover
    # cannot resize the tile. A border that appears from nothing reflows the
    # card by a pixel and reads as a twitch.
    "border_width": 1,
    "border_color": CELL,
}
_STYLE_CARD_HOVERED = {
    "background_color": CELL_HOVERED,
    "border_radius": 4,
    "margin": 3,
    "border_width": 1,
    "border_color": AMETHYST,
}
_STYLE_CARD_TITLE = {"font_size": SIZE_BODY, "color": TEXT_EMPHASIS}
_STYLE_CARD_META = {"font_size": SIZE_META, "color": TEXT_SECONDARY}

#: The tool grid: two columns of tiles, each a logo well, a name and two lines
#: of description.
_TOOL_COLUMNS = 2
_TOOL_CELL = 64
#: The logo well. Always drawn, whether or not the tool has a logo, so a
#: missing one reads as a held slot rather than as a broken image.
_TOOL_AVATAR = 34
#: What fits in two lines under a name on a half-width tile.
_TOOL_DESCRIPTION_CHARS = 70

#: Tiles drawn per press of Load more. Enough to fill the window twice over,
#: few enough that opening the browser does not lay out the whole catalogue.
_TOOLS_PAGE = 24

#: Pills: the kit filter row and a kit's own tab row. Width is estimated from
#: the label, because omni.ui will not measure a string and the row has to wrap
#: by itself.
_PILL_HEIGHT = 22
_PILL_CHAR_WIDTH = 7
_PILL_PADDING = 22
_PILL_GAP = 6
_STYLE_PILL = {
    "Button": {
        "background_color": QUIET_FILL,
        "border_color": QUIET_BORDER,
        "border_width": 1,
        "border_radius": 11,
    },
    "Button:hovered": {"background_color": CELL_HOVERED},
    "Button.Label": {"font_size": SIZE_BODY, "color": TEXT_BODY},
}
_STYLE_PILL_ACTIVE = {
    "Button": {
        "background_color": WEIGHTED_FILL,
        "border_color": WEIGHTED_BORDER,
        "border_width": 1,
        "border_radius": 11,
    },
    "Button:hovered": {"background_color": WEIGHTED_BORDER},
    "Button.Label": {"font_size": SIZE_BODY, "color": TEXT_EMPHASIS},
}

#: How far outside the visible area an avatar is still worth fetching, as a
#: multiple of the row height. Two screens' worth in each direction, so a
#: normal scroll lands on pictures that are already there rather than on a
#: column of blanks that fill in behind the pointer.
_AVATAR_PREFETCH_ROWS = 2

#: The placeholder under an avatar that has been asked for but has not arrived.
#: Vertical bands, each running the same cycle a little behind the one to its
#: left, so the brightness travels across the square instead of the whole thing
#: blinking together. Three is enough to read as movement across 48px and few
#: enough to restyle every frame without thinking about it.
_PULSE_BANDS = 3
#: Seconds for one full cycle. Slow enough to read as breathing; a faster one
#: reads as a fault.
_PULSE_PERIOD_SECONDS = 1.6
#: The greys it travels between, dark enough that a row of them is calm rather
#: than a wall of flashing squares.
_PULSE_DARK = 0x2A
_PULSE_LIGHT = 0x4A

#: How still the list has to be before an avatar is worth asking for. A scroll
#: from the top of the catalogue to the bottom passes every card in it,
#: and fetching each one as it flies by spends the whole request budget on
#: pictures nobody saw. Waiting for the scroll to settle means a long drag
#: costs what is on screen when it stops, which is about a dozen.
_SCROLL_SETTLE_SECONDS = 0.35

#: How often to look again while anything is still waiting for a picture.
#: Scroll events are the main trigger, but they are not a guarantee: a card can
#: end up on screen without one (a layout that settles a frame late, a cooldown
#: that lifts while nothing moves), and a placeholder that waits for the user
#: to nudge the list is a placeholder that looks broken.
_SWEEP_SECONDS = 2.0

#: What a 429 costs EVERYTHING, not just the avatar that earned it. The
#: endpoint is rate limited per client and the catalogue is larger than the
#: limit, so a refusal means the budget is gone and every other request in the
#: queue would be refused too. Retrying them individually is what turns one
#: throttled scroll into a storm that cannot converge: each sweep re-trips the
#: limit it is waiting on.
_COOLDOWN_SECONDS = 30.0
_COOLDOWN_CEILING = 120.0
#: Give up on one avatar after this many refusals.
_AVATAR_MAX_ATTEMPTS = 5


def _grey(value: int) -> int:
    """An opaque grey as omni.ui wants it: 0xAABBGGRR."""
    value = max(0, min(255, value))
    return 0xFF000000 | (value << 16) | (value << 8) | value

def _cover(card: ui.Rectangle, on_click) -> None:
    """Make the whole card clickable, and light it while the pointer is on it.

    The click target is a button covering the card rather than one sitting
    inside it: a button occupying part of a tile leaves dead space that looks
    clickable and is not.

    That is also why the hover is driven from the button instead of from a
    `:hovered` style on the rectangle. The button covers the card, so it is the
    widget the pointer is actually over, and the rectangle underneath would
    never see the state change.
    """
    hit = ui.InvisibleButton(clicked_fn=on_click)
    hit.set_mouse_hovered_fn(
        lambda hovered, rect=card: rect.set_style(
            _STYLE_CARD_HOVERED if hovered else _STYLE_CARD
        )
    )


def _rest(bands: list) -> None:
    """Settle the bands to a flat, quiet square."""
    for rect, _phase in bands:
        try:
            rect.set_style({"background_color": _grey(_PULSE_DARK)})
        except Exception:  # noqa: BLE001 - a destroyed band needs no resting
            pass


def _build_pulse_placeholder() -> list:
    """The bands of one placeholder square, left to right.

    Returned rather than registered, because building a card is not the same
    as deciding to fetch its picture: most of these are never asked for and
    must never animate.
    """
    bands = []
    with ui.HStack():
        for index in range(_PULSE_BANDS):
            # Each band a third of a cycle behind its neighbour, which is what
            # turns three squares blinking into one brightness travelling.
            bands.append(
                (ui.Rectangle(style={"background_color": _grey(_PULSE_DARK)}),
                 index / _PULSE_BANDS)
            )
    return bands


class ToolBrowser:
    """Find a tool you cannot name, by kit, by tab, or by typing.

    A dropdown picks a tool you already know. This window is for the other case,
    and a flat alphabetical list of the whole catalogue was only half an answer
    to it: it works when you can spell the name and not at all when what you
    know is that you want to edit an image.

    Kits are that missing half, and they come from the server already resolved:
    `GET /api/v2/kits` returns the same curation the web app renders, filtered
    to the tools this account can run and scoped to this plugin's surface, so
    nothing here decides which tools belong together. They are a FILTER over
    the grid, a row of pills with their counts on them, and a kit's own tabs
    narrow it further from the right of the heading.

    Search cuts across all of it. Someone typing knows what they are looking
    for, and making them pick a kit first would be the dropdown's problem again.

    The flat list is still the fallback, on purpose. A slow or unavailable kits
    call has to leave a working tool browser rather than an empty window, so the
    catalogue the panel already fetched is what shows while the kits load and
    what stays if they never arrive.
    """

    #: What the kits call is doing, which the window has to be able to say.
    KITS_LOADING = "loading"
    KITS_READY = "ready"
    KITS_UNAVAILABLE = "unavailable"

    def __init__(self, session: RdSession, cache_dir: Path) -> None:
        self._session = session
        self._cache_dir = cache_dir
        self._window: ui.Window | None = None
        self._tasks: set[asyncio.Task] = set()
        self._grid = AssetGrid(cache_dir)

        # Rebuilt on every navigation. A plain Frame, because re-entering a
        # CollapsableFrame to replace its child does not draw.
        self._body: ui.Frame | None = None
        self._status: ui.Label | None = None

        self._tools: list[tools_api.ToolSummary] = []
        #: The scroller, kept so the visible range can be worked out.
        self._scroll: ui.ScrollingFrame | None = None
        #: Avatars belonging to cards that have been built but not yet fetched.
        #: A catalogue of tools means a catalogue of pictures, and fetching them
        #: all to show twelve is the whole reason this list exists. Entries are
        #: dropped as they load, so the list is what is still outstanding.
        self._deferred_avatars: list[tuple[ui.Frame, str, list]] = []
        #: Placeholder bands currently breathing, as (rectangle, phase offset).
        #: Only avatars actually being fetched are in here: animating the other
        #: two hundred would burn a frame's work on squares nobody can see.
        self._pulsing: list[tuple[ui.Rectangle, float]] = []
        self._pulse_phase = 0.0
        self._pulse_subscription = None
        #: How many times each avatar has been refused, keyed by url, and
        #: whether a retry sweep is already booked.
        self._avatar_attempts: dict[str, int] = {}
        #: Nothing is fetched before this. One shared clock, because the
        #: throttle is shared: it is keyed on the IP, not on the picture.
        self._cooldown_until = 0.0
        self._cooldown_streak = 0
        self._retry_task = None
        #: Debounce for the scroll. Restarted by every scroll event, so it
        #: only ever fires once the list has stopped moving.
        self._settle_task = None
        #: The periodic look-again while anything is still outstanding.
        self._sweep_task = None
        self._kits: list[kits_api.Kit] = []
        self._kits_state = self.KITS_LOADING
        #: Which kit the grid is filtered to, and which of its tabs. None means
        #: every tool.
        self._open_kit: kits_api.Kit | None = None
        self._active_tab: str | None = None
        self._search = ""
        #: How many tiles the grid under the current filter draws. Reset by
        #: every change of filter, raised by Load more.
        self._shown_limit = _TOOLS_PAGE
        #: The search box and the placeholder drawn inside it.
        self._search_field: ui.StringField | None = None
        self._search_placeholder: ui.Label | None = None
        self._on_pick = None
        #: A rebuild is already booked for the next frame. See `_render`.
        self._render_pending = False

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._render_pending = False
        self._deferred_avatars = []
        self._pulsing = []
        self._pulse_subscription = None
        self._retry_task = None
        self._settle_task = None
        self._sweep_task = None
        self._avatar_attempts = {}
        self._cooldown_until = 0.0
        self._cooldown_streak = 0
        self._scroll = None
        self._grid.destroy()
        if self._window is not None:
            self._window.destroy()
            self._window = None
        # Nothing to rebuild once the window is gone, and a booked rebuild
        # would reach for a frame that no longer exists.
        self._body = None
        self._status = None
        self._search_field = None
        self._search_placeholder = None

    # -- lifecycle ---------------------------------------------------------

    def show(
        self,
        tools: list[tools_api.ToolSummary],
        on_pick,
        *,
        kits: list[kits_api.Kit] | None = None,
    ) -> None:
        """`on_pick` receives the chosen ToolSummary.

        `kits` is the panel's prefetch, taken the same way `tools` is. When it
        has them the pills are there on the first frame; when it does not this
        window falls back to fetching its own, which is what happens if the
        prefetch failed or has not landed yet.
        """
        self._tools = list(tools)
        self._on_pick = on_pick
        # Opened fresh each time rather than where it was left. The window is a
        # way of answering "which tool", and resuming inside yesterday's filter
        # is not a useful place to start.
        self._open_kit = None
        self._active_tab = None
        self._search = ""
        self._shown_limit = _TOOLS_PAGE

        if self._window is None:
            self._window = ui.Window("Choose a tool", width=520, height=620)

        with self._window.frame:
            with ui.VStack(spacing=8):
                with ui.VStack(height=0, spacing=4):
                    # A placeholder under the field rather than a label above
                    # it: the words say what the box is for, in the box, and
                    # cost no row. Hidden as soon as there is text.
                    with ui.ZStack(height=26):
                        self._search_field = ui.StringField(height=26)
                        with ui.HStack():
                            ui.Spacer(width=8)
                            self._search_placeholder = ui.Label(
                                "Search all tools", style=_STYLE_CARD_META
                            )
                    self._search_field.model.add_value_changed_fn(self._on_search_changed)
                    self._status = ui.Label(
                        "", word_wrap=True, height=0, style=_STYLE_CARD_META
                    )

                # Never sideways. Everything in here wraps to the width it is
                # given, so a horizontal bar would only ever mean something
                # failed to, and it is the harder of the two scrolls to use.
                self._scroll = ui.ScrollingFrame(
                    horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF
                )
                with self._scroll:
                    self._body = ui.Frame(height=0)
                # Scrolling is the event that reveals new cards, so it is what
                # asks for their pictures. Guarded because losing the callback
                # should cost lazy loading, not the window: without it every
                # avatar simply waits for the next render instead.
                if hasattr(self._scroll, "set_scroll_y_changed_fn"):
                    self._scroll.set_scroll_y_changed_fn(self._on_scrolled)
                else:
                    logger.info(
                        "RunDiffusion: this omni.ui has no scroll callback, so "
                        "tool avatars load per render rather than on scroll."
                    )

        # Refetched only when the panel has nothing to give. The curation is
        # per account, and the panel drops what it holds on an account change,
        # so an empty list here means "not loaded", never "loaded and empty".
        self._kits = list(kits or [])
        self._kits_state = self.KITS_READY if self._kits else self.KITS_LOADING
        # A booking made against the previous body must not suppress this one.
        self._render_pending = False
        self._render()
        if not self._kits:
            self._spawn(self._load_kits())

        self._window.visible = True
        # Focused on open. Search already spans every tool, so anyone who knows
        # the name types it and never touches the browsing below.
        self._spawn(self._focus_search_next_frame())

    async def _focus_search_next_frame(self) -> None:
        """Put the caret in the search box once the window has been drawn.

        A frame late, because a field that has not been laid out yet has
        nowhere to put keyboard focus.
        """
        import omni.kit.app

        await omni.kit.app.get_app().next_update_async()
        if self._search_field is None:
            return
        try:
            self._search_field.focus_keyboard()
        except Exception:  # noqa: BLE001 - an unfocused box still works
            logger.debug("RunDiffusion: could not focus the tool search.")

    def _spawn(self, coroutine) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(_task_finished(self._tasks))

    async def _load_kits(self) -> None:
        try:
            self._kits = await kits_api.list_kits(self._session)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the flat list still works
            # Includes the 403 an account without this plugin enabled gets. The
            # window degrades to the catalogue rather than explaining a gate
            # that the panel's own wall explains better.
            logger.info("RunDiffusion: could not load kits. %s", error)
            self._kits_state = self.KITS_UNAVAILABLE
            self._render()
            return

        self._kits_state = self.KITS_READY
        self._render()

    # -- navigation --------------------------------------------------------

    def _on_search_changed(self, model) -> None:
        self._search = model.get_value_as_string().strip()
        if self._search_placeholder is not None:
            self._search_placeholder.visible = not model.get_value_as_string()
        self._shown_limit = _TOOLS_PAGE
        self._render()

    def _filter_kit(self, kit: kits_api.Kit | None) -> None:
        """Narrow the grid to one kit, or put every tool back with None.

        A filter rather than a place to go. A kit used to be a card you entered
        and left again by a "< All kits" breadcrumb; as a row of pills the counts
        stay on screen and moving between kits costs one click with nothing to
        climb back out of.
        """
        self._open_kit = kit
        self._active_tab = kits_api.ALL_TAB if kit is not None else None
        self._shown_limit = _TOOLS_PAGE
        self._render()

    def _select_tab(self, tab: str) -> None:
        self._active_tab = tab
        self._shown_limit = _TOOLS_PAGE
        self._render()

    def _load_more_tools(self) -> None:
        self._shown_limit += _TOOLS_PAGE
        self._render()

    def _choose(self, tool: tools_api.ToolSummary) -> None:
        # Remembered on this machine, so the next opening can offer it first.
        prefs.remember_tool(tool.id)
        if self._window is not None:
            self._window.visible = False
        if self._on_pick is not None:
            self._on_pick(tool)

    # -- rendering ---------------------------------------------------------

    def _render(self) -> None:
        """Ask for a rebuild. It happens on the next frame, never on this one.

        Every caller of this is a click handler, and rebuilding a container
        from inside the event that is still being dispatched is something
        omni.ui refuses: it logs `Container::clear was called during an event
        or draw` and `addChild attempting to add a child during a draw
        callback`, then carries on in a state it does not define. Waiting one
        frame costs nothing a person can perceive and puts the work where the
        toolkit expects it.

        Coalescing matters as much as deferring. A tab click can land while a
        rebuild is already pending, and running both would clear a container
        the second pass is midway through filling.
        """
        if self._body is None or self._render_pending:
            return
        self._render_pending = True
        self._spawn(self._render_next_frame())

    async def _render_next_frame(self) -> None:
        import omni.kit.app

        try:
            await omni.kit.app.get_app().next_update_async()
        except asyncio.CancelledError:
            self._render_pending = False
            raise
        self._render_pending = False
        # The window can close between the click and the frame that serves it.
        if self._body is None:
            return
        self._render_now()

    def _render_now(self) -> None:
        if self._body is None:
            return
        self._set_status(self._status_text())
        # Before the clear, not after: every avatar fetch still running was
        # started for a row that is about to stop existing, and one that lands
        # afterwards paints its picture onto whichever row inherits the slot.
        self._grid.cancel_pending()
        # The frames these point at are about to be destroyed, and a card that
        # is rebuilt registers itself again. The bands go with them, and the
        # subscription ends with the last of them.
        self._deferred_avatars = []
        self._pulsing = []
        self._pulse_subscription = None
        self._body.clear()
        with self._body:
            if self._search:
                self._render_search()
            else:
                self._render_browse()
        # A frame later, when the cards have positions to read.
        self._spawn(self._load_visible_avatars_next_frame())
        self._ensure_sweeping()

    async def _load_visible_avatars_next_frame(self) -> None:
        import omni.kit.app

        await omni.kit.app.get_app().next_update_async()
        self._load_visible_avatars()

    def _status_text(self) -> str:
        if self._search:
            return "Searching every tool you can run."
        if self._kits_state == self.KITS_LOADING:
            return "Loading kits..."
        if self._kits_state == self.KITS_UNAVAILABLE:
            return "Kits are unavailable right now. Every tool is listed below."
        if not self._kits:
            return "No kits are set up for this account. Every tool is listed below."
        return ""

    def _set_status(self, message: str) -> None:
        if self._status is not None:
            self._status.text = message

    def _render_search(self) -> None:
        needle = self._search.lower()
        shown = [
            tool
            for tool in self._tools
            if needle in tool.name.lower() or needle in (tool.description or "").lower()
        ]
        with ui.VStack(spacing=8, height=0):
            ui.Label(f"RESULTS · {len(shown)} TOOLS", height=0, style=STYLE_CAPS)
            if not shown:
                ui.Label("No tool matches that.", word_wrap=True, style=STYLE_SECONDARY)
                return
            self._build_tool_grid(shown)

    def _render_browse(self) -> None:
        """The kit pills, the tools used recently, then the tools under the filter.

        Every tool stays one click away from the first frame: "All tools" is
        the pill that starts selected, so the answer to "where is the one I used
        last week" is on screen without working out which kit leads to it.
        """
        kit = self._open_kit
        with ui.VStack(spacing=10, height=0):
            self._build_kit_pills()
            if kit is None:
                self._build_recent()

            if kit is not None:
                tools = [entry.tool for entry in kit.tools_for_tab(self._active_tab)]
                heading = kit.label.upper()
            else:
                tools = list(self._tools)
                heading = "ALL"
            ui.Label(f"{heading} · {len(tools)} TOOLS", height=0, style=STYLE_CAPS)
            # The curator's tabs, only where they authored some. A row holding
            # nothing but "All" is a control that cannot do anything. On rows
            # of their own under the heading, wrapping like the kit pills: in
            # one line beside the heading a kit with several tabs ran past the
            # edge of the window into a horizontal scroll. Reported from Base
            # Editor.
            if kit is not None and len(kit.tabs) > 1:
                self._build_wrapped_pills(
                    [
                        (tab, tab == self._active_tab, lambda t=tab: self._select_tab(t))
                        for tab in kit.tabs
                    ]
                )

            if not tools:
                ui.Label("Nothing under this tab.", word_wrap=True, style=STYLE_SECONDARY)
                return
            self._build_tool_grid(tools)

    def _build_kit_pills(self) -> None:
        """All tools, then each kit with its count, wrapping onto more rows.

        Wrapped by hand, because omni.ui has no flow layout: each pill's width
        is estimated from its label and a row closes when the next one would
        not fit.
        """
        entries: list[tuple[str, Any]] = [("All tools", None)]
        entries.extend((f"{kit.label} · {len(kit.tools)}", kit) for kit in self._kits)
        self._build_wrapped_pills(
            [
                (label, kit is self._open_kit, lambda k=kit: self._filter_kit(k))
                for label, kit in entries
            ]
        )

    def _build_wrapped_pills(self, pills: list[tuple[str, bool, Any]]) -> None:
        """(label, active, on_click) pills, laid out in rows that wrap.

        By hand, because omni.ui has no flow layout: each pill's width is
        estimated from its label, and a row closes when the next one would not
        fit in the width the scroller reports.
        """
        available = 0.0
        if self._scroll is not None:
            try:
                available = float(getattr(self._scroll, "computed_width", 0) or 0)
            except Exception:  # noqa: BLE001 - a width that cannot be read is the fallback
                available = 0.0
        available = (available or 480.0) - 24

        rows = wrap_pills([label for label, _active, _click in pills], available)
        with ui.VStack(spacing=_PILL_GAP, height=0):
            for row in rows:
                with ui.HStack(height=_PILL_HEIGHT, spacing=_PILL_GAP):
                    for index in row:
                        label, active, on_click = pills[index]
                        self._build_pill(label, active, on_click)
                    ui.Spacer()

    @staticmethod
    def _build_pill(label: str, active: bool, on_click) -> None:
        ui.Button(
            label,
            width=_pill_width(label),
            height=_PILL_HEIGHT,
            style=_STYLE_PILL_ACTIVE if active else _STYLE_PILL,
            clicked_fn=on_click,
        )

    def _build_recent(self) -> None:
        """Up to two tools this machine picked most recently.

        Local and needing no API. Most sessions use two or three tools, so this
        is the cheapest way there is to put the next one a single click away.
        Only tools the catalogue in hand lists are shown, so a tool the current
        account cannot run never appears here.
        """
        by_id = {tool.id: tool for tool in self._tools}
        recent = [by_id[tool_id] for tool_id in prefs.recent_tool_ids() if tool_id in by_id]
        if not recent:
            return
        with ui.VStack(spacing=4, height=0):
            with ui.HStack(height=16):
                ui.Label("RECENT", style=STYLE_CAPS)
                ui.Label(
                    "this machine",
                    width=0,
                    alignment=ui.Alignment.RIGHT_CENTER,
                    style=_STYLE_CARD_META,
                )
            with ui.VGrid(column_count=_TOOL_COLUMNS, row_height=_TOOL_CELL, height=0):
                for tool in recent:
                    self._build_tool_card(tool)

    def _build_tool_grid(self, tools: list) -> None:
        """Two columns of tiles, a page at a time, with Load more under them."""
        visible = tools[: self._shown_limit]
        with ui.VGrid(column_count=_TOOL_COLUMNS, row_height=_TOOL_CELL, height=0):
            for tool in visible:
                self._build_tool_card(tool)
        if len(tools) > len(visible):
            ui.Button(
                "Load more",
                height=24,
                style=STYLE_QUIET_BUTTON,
                clicked_fn=self._load_more_tools,
            )

    def _ensure_sweeping(self) -> None:
        """Keep looking while anything is outstanding, then stop.

        Bounded by its own queue: it exits as soon as every avatar has either
        arrived or been given up on, so an idle browser costs nothing.
        """
        if self._sweep_task is not None or not self._deferred_avatars:
            return
        self._sweep_task = asyncio.ensure_future(self._sweep())
        self._tasks.add(self._sweep_task)
        self._sweep_task.add_done_callback(self._tasks.discard)

    async def _sweep(self) -> None:
        try:
            while self._deferred_avatars:
                await asyncio.sleep(_SWEEP_SECONDS)
                self._load_visible_avatars()
        except asyncio.CancelledError:
            raise
        finally:
            self._sweep_task = None

    def _avatar_finished(
        self,
        frame: ui.Frame,
        url: str,
        bands: list,
        loaded: bool,
        retryable: bool,
        retry_after: float | None,
    ) -> None:
        """One avatar stopped being in flight. Decide whether it comes back.

        This is where a throttled avatar used to die, and then where it used to
        storm. Asking for one removed it from `_deferred_avatars`, so the first
        version never asked again: bands frozen, no picture, and only an
        unrelated rebuild put the card back. The second version re-queued each
        refusal on its own timer, which meant ninety-odd requests going out
        together every few seconds against a limit that allows 120 a minute.
        That never converged; it just kept the window open.

        So a refusal is treated as news about the connection rather than about
        the picture: everything stops for a while, then what is on screen is
        asked for again.
        """
        self._stop_pulsing(bands)
        _rest(bands)
        if loaded:
            self._avatar_attempts.pop(url, None)
            self._cooldown_streak = 0
            return

        attempts = self._avatar_attempts.get(url, 0) + 1
        self._avatar_attempts[url] = attempts
        if not retryable or attempts >= _AVATAR_MAX_ATTEMPTS:
            # Settled. The card keeps its resting placeholder rather than an
            # animation promising something no longer coming.
            return

        self._deferred_avatars.append((frame, url, bands))
        self._enter_cooldown(retry_after)
        self._ensure_sweeping()

    def _enter_cooldown(self, retry_after: float | None) -> None:
        """Stop asking for anything for a while, and book one sweep after.

        One cooldown for all of them. The refusals arrive in a clump, so this
        takes the first one's word for it and ignores the rest of the clump
        rather than compounding a delay ninety times over.
        """
        now = time.monotonic()
        if now < self._cooldown_until:
            return
        self._cooldown_streak += 1
        delay = retry_after or min(
            _COOLDOWN_SECONDS * (2 ** (self._cooldown_streak - 1)), _COOLDOWN_CEILING
        )
        self._cooldown_until = now + delay
        logger.info(
            "RunDiffusion: avatar requests throttled. Pausing %.0fs.", delay
        )
        self._book_resume(delay)

    def _book_resume(self, delay: float) -> None:
        """Kept apart from the decision above so the decision can be tested."""
        if self._retry_task is not None:
            return
        self._retry_task = asyncio.ensure_future(self._resume_after(delay))
        self._tasks.add(self._retry_task)
        self._retry_task.add_done_callback(self._tasks.discard)

    async def _resume_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            self._retry_task = None
            # Through the visibility check, so the sweep asks for what is on
            # screen now rather than for everything that was ever refused.
            self._load_visible_avatars()
        except asyncio.CancelledError:
            self._retry_task = None
            raise

    # -- the breathing placeholder ----------------------------------------

    def _start_pulsing(self, bands: list) -> None:
        if not bands:
            return
        self._pulsing.extend(bands)
        if self._pulse_subscription is not None:
            return
        import omni.kit.app

        # Subscribed only while something is waiting. A permanent per-frame
        # callback that spends most of its life iterating an empty list is a
        # cost this window would carry for as long as it is open.
        self._pulse_subscription = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(
                self._on_pulse, name="rundiffusion.tool-avatar-pulse"
            )
        )

    def _stop_pulsing(self, bands: list) -> None:
        if bands:
            done = {id(rect) for rect, _phase in bands}
            self._pulsing = [
                entry for entry in self._pulsing if id(entry[0]) not in done
            ]
        if not self._pulsing:
            self._pulse_subscription = None

    def _on_pulse(self, event) -> None:
        """Move every waiting band one frame along its cycle."""
        if not self._pulsing:
            self._pulse_subscription = None
            return
        try:
            delta = float(event.payload["dt"])
        except Exception:  # noqa: BLE001 - a missing dt is not worth a stall
            delta = 1.0 / 60.0
        self._pulse_phase = (self._pulse_phase + delta) % _PULSE_PERIOD_SECONDS

        base = self._pulse_phase / _PULSE_PERIOD_SECONDS
        span = _PULSE_LIGHT - _PULSE_DARK
        for rect, offset in list(self._pulsing):
            # sin over the cycle, lifted into 0..1, so it eases at both ends
            # instead of snapping back at the seam.
            wave = (math.sin(2 * math.pi * (base + offset)) + 1.0) / 2.0
            try:
                rect.set_style(
                    {"background_color": _grey(_PULSE_DARK + int(span * wave))}
                )
            except Exception:  # noqa: BLE001 - a destroyed band costs one band
                self._pulsing = [
                    entry for entry in self._pulsing if entry[0] is not rect
                ]

    def _on_scrolled(self, _y: float) -> None:
        """Scrolling does not fetch. It restarts the clock that does.

        Every scroll event cancels the pending settle and books another, so a
        continuous drag fires nothing at all until the user stops.
        """
        if self._settle_task is not None:
            self._settle_task.cancel()
        self._settle_task = asyncio.ensure_future(self._settle())
        self._tasks.add(self._settle_task)
        self._settle_task.add_done_callback(self._tasks.discard)

    async def _settle(self) -> None:
        try:
            await asyncio.sleep(_SCROLL_SETTLE_SECONDS)
        except asyncio.CancelledError:
            raise
        self._settle_task = None
        self._load_visible_avatars()

    def _load_visible_avatars(self) -> None:
        """Fetch the pictures for cards at or near the visible area.

        Position is read off the widgets rather than calculated from indices
        and row heights. The grid does not start at the top of the scroller
        (the kits and two headings sit above it, and their height depends on
        how many kits there are), so any arithmetic would need an offset that
        is itself only knowable after layout. `screen_position_y` already
        accounts for all of it.

        Positions are only meaningful once omni.ui has laid the widgets out,
        which is why the first call is made a frame after the render rather
        than inside it.
        """
        if self._scroll is None or not self._deferred_avatars:
            return
        if time.monotonic() < self._cooldown_until:
            # The budget is spent. Asking now earns another 429 and pushes the
            # window further out, which is exactly how this failed before.
            return

        top = getattr(self._scroll, "screen_position_y", None)
        height = getattr(self._scroll, "computed_height", None)
        if not top or not height:
            # Not laid out yet. Nothing is fetched rather than everything:
            # a wrong guess here is the exact cost this method exists to
            # avoid, and the next scroll or render will ask again.
            return

        margin = _TOOL_CELL * _AVATAR_PREFETCH_ROWS
        # Clamped so the band cannot reach 0. A card omni.ui has not laid out
        # yet reports `screen_position_y` of 0, and with the prefetch margin
        # subtracted from a window near the top of the screen the band starts
        # negative, which made every undrawn card in the catalogue look like
        # it was on screen. That is not a near miss: on the first pass none of
        # them are drawn, so the whole catalogue was requested at once, and
        # everything past the endpoint's rate limit came back 429.
        visible_top = max(top - margin, 1.0)
        visible_bottom = top + height + margin

        still_waiting: list[tuple[ui.Frame, str, list]] = []
        for frame, url, bands in self._deferred_avatars:
            y = getattr(frame, "screen_position_y", 0)
            # A zero is "not laid out", never "at the top of the screen": a
            # real card inside a real scroller always sits below the window
            # chrome. Treating the two as the same is what made this fetch the
            # whole catalogue.
            if y > 0 and visible_top <= y <= visible_bottom:
                self._start_pulsing(bands)
                self._grid.load_image_into(
                    frame,
                    url,
                    size=_TOOL_AVATAR,
                    on_done=(
                        lambda loaded, retryable, wait, f=frame, u=url, b=bands: (
                            self._avatar_finished(f, u, b, loaded, retryable, wait)
                        )
                    ),
                )
            else:
                still_waiting.append((frame, url, bands))
        self._deferred_avatars = still_waiting

    def update_tools(self, tools: list[tools_api.ToolSummary]) -> None:
        """Take a longer catalogue than the one this window opened with.

        The panel drains `/tools` over several round trips and calls this after
        each. Ignored while closed, because `show` takes the list again anyway
        and rebuilding an invisible window is work nobody sees.
        """
        if self._window is None or not self._window.visible:
            return
        if len(tools) == len(self._tools):
            return
        self._tools = list(tools)
        self._render()

    def _build_tool_card(self, tool: tools_api.ToolSummary) -> None:
        """One tool as a tile: logo well, name, and what it does.

        The well is drawn for every tool, logo or not. Where there is no logo it
        holds an RD monogram, so an absent picture reads as a slot being held
        rather than as an image that failed to load, which is what two tiles
        in the earlier browser looked like.
        """
        with ui.ZStack(height=_TOOL_CELL):
            card = ui.Rectangle(style=_STYLE_CARD)
            with ui.HStack():
                ui.Spacer(width=10)
                with ui.VStack(width=_TOOL_AVATAR):
                    ui.Spacer()
                    with ui.ZStack(width=_TOOL_AVATAR, height=_TOOL_AVATAR):
                        ui.Rectangle(
                            style={"background_color": CELL_EMPTY, "border_radius": 3}
                        )
                        avatar = ui.Frame(width=_TOOL_AVATAR, height=_TOOL_AVATAR)
                        bands: list = []
                        with avatar:
                            if tool.avatar_url:
                                bands = _build_pulse_placeholder()
                            else:
                                ui.Label(
                                    "RD",
                                    alignment=ui.Alignment.CENTER,
                                    style=_STYLE_CARD_META,
                                )
                    ui.Spacer()
                if tool.avatar_url:
                    # Registered, not fetched. `_load_visible_avatars` decides
                    # which of these are worth a request, and starts these
                    # bands breathing when it asks.
                    self._deferred_avatars.append((avatar, tool.avatar_url, bands))
                ui.Spacer(width=10)
                with ui.VStack(spacing=2):
                    ui.Spacer(height=10)
                    ui.Label(
                        tool.name,
                        elided_text=True,
                        height=0,
                        style=_STYLE_CARD_TITLE,
                    )
                    if tool.description:
                        ui.Label(
                            shorten(tool.description, _TOOL_DESCRIPTION_CHARS),
                            word_wrap=True,
                            height=0,
                            style=_STYLE_CARD_META,
                        )
                    ui.Spacer()
                ui.Spacer(width=10)
            _cover(card, lambda t=tool: self._choose(t))


def wrap_pills(labels: list[str], available: float) -> list[list[int]]:
    """Which pills go on which row, by index, in a width of `available`.

    A pill wider than the whole row still gets a row to itself rather than
    being dropped.
    """
    rows: list[list[int]] = [[]]
    used = 0.0
    for index, label in enumerate(labels):
        width = _pill_width(label)
        if rows[-1] and used + _PILL_GAP + width > available:
            rows.append([])
            used = 0.0
        used += (_PILL_GAP if rows[-1] else 0) + width
        rows[-1].append(index)
    return [row for row in rows if row]


def _pill_width(label: str) -> int:
    """A pill's width, estimated from its label. See `_build_kit_pills`."""
    return len(label) * _PILL_CHAR_WIDTH + _PILL_PADDING


class AssetPicker:
    """Choose an image for a field: from the library, from uploads, or from disk.

    A file from disk is uploaded first, because an image field takes a reference
    to something the server already holds. Doing that here rather than at submit
    time means the user finds out immediately if the file is the wrong type,
    instead of losing a generate to it.
    """

    def __init__(self, session: RdSession, cache_dir: Path, *, on_uploaded=None) -> None:
        self._session = session
        self._cache_dir = cache_dir
        #: Called when a file chosen here becomes an upload. The Uploads tab
        #: holds its pages rather than refetching them on every visit, so
        #: without this an image added from a field would be missing from the
        #: tab that exists to list uploads, for the rest of the session.
        self._on_uploaded = on_uploaded
        self._window: ui.Window | None = None
        self._grid = AssetGrid(cache_dir)
        self._tasks: set[asyncio.Task] = set()
        self._status: ui.Label | None = None
        #: Where the current source has been paged to. Reset by switching
        #: source, because a cursor is issued against one listing and means
        #: nothing against the other.
        self._source = "library"
        self._cursor: str | None = None
        self._has_more = False
        self._loading = False
        #: Bumped whenever the listing starts again, so a page still in the air
        #: for the old source, or for a previous opening of this window, knows
        #: not to paint into a body that has been rebuilt under it.
        self._generation = 0
        #: How many usable images are on screen. Counted rather than taken from
        #: the page, because the pages are filtered.
        self._shown = 0
        self._more_button: Any = None

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._grid.destroy()
        if self._window is not None:
            self._window.destroy()
            self._window = None

    def _spawn(self, coroutine) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(_task_finished(self._tasks))

    def show(self, on_pick) -> None:
        """`on_pick` receives the chosen Asset."""
        if self._window is None:
            self._window = ui.Window("Choose an image", width=520, height=620)

        def _picked(asset: Asset) -> None:
            if self._window is not None:
                self._window.visible = False
            on_pick(asset)

        # Every child except the grid is sized to its content, so the grid gets
        # the rest of the window. Without the explicit heights an omni.ui stack
        # DISTRIBUTES its space evenly, which put a band of empty room above the
        # images roughly as tall as the images themselves.
        with self._window.frame:
            with ui.VStack(spacing=8):
                with ui.HStack(height=26, spacing=6):
                    ui.Button(
                        "From library",
                        clicked_fn=lambda: self._spawn(self._load(body, "library", _picked)),
                    )
                    ui.Button(
                        "From uploads",
                        clicked_fn=lambda: self._spawn(self._load(body, "uploads", _picked)),
                    )
                    ui.Button(
                        "From file",
                        clicked_fn=lambda: self._pick_file(_picked),
                    )
                self._status = ui.Label("", word_wrap=True, height=0)
                with ui.ScrollingFrame():
                    body = ui.Frame(height=0)
                    with body:
                        ui.Label("Loading your library...")
                # Below the scroll rather than under the last row, so the way
                # to reach older images does not itself have to be scrolled to.
                self._more_button = ui.Button(
                    "Load more",
                    height=24,
                    clicked_fn=lambda: self._spawn(
                        self._load(body, self._source, _picked, more=True)
                    ),
                )
                self._more_button.enabled = False

        self._window.visible = True
        # Opened straight onto the library rather than onto a chooser. Asking
        # which source first is a click spent before anything is on screen, and
        # the library is where the images almost always are.
        self._spawn(self._load(body, "library", _picked))

    async def _load(
        self, body: ui.Frame, source: str, on_pick, *, more: bool = False
    ) -> None:
        """A page of `source`. `more` adds the next one under what is shown.

        Without `more` this picker showed the newest two dozen images and
        offered no way past them, so choosing anything older meant leaving the
        panel for the browser.
        """
        restarting = not more or source != self._source
        # Only Load more is refused while one is in flight, and only because two
        # clicks would spend the same cursor twice. Refusing a RESTART is what
        # left this window stuck on "Loading your library..." when it was closed
        # and reopened mid-request, and made From uploads do nothing when it was
        # pressed before the library had landed.
        if not restarting and self._loading:
            return
        if restarting:
            # Same rule as the tool browser's `_render`: switching source
            # between library and uploads replaces every cell, so the
            # thumbnails still arriving for the old source have nowhere
            # legitimate to land.
            self._grid.cancel_pending()
            self._generation += 1
            self._source = source
            self._cursor = None
            self._has_more = False
            self._shown = 0
            body.clear()
            with body:
                ui.Label("Loading...")

        generation = self._generation
        cursor = self._cursor
        self._loading = True
        self._set_more_enabled(False)
        try:
            page = (
                await assets_api.list_library(self._session, cursor=cursor)
                if source == "library"
                else await assets_api.list_uploads(self._session, cursor=cursor)
            )
        except ApiError as error:
            if generation != self._generation:
                return
            if cursor is None:
                body.clear()
                with body:
                    ui.Label(f"Could not load. {error.message}", word_wrap=True)
            else:
                # What is on screen still stands, so the failure is said in the
                # status line rather than by replacing images that are fine.
                self._set_status(f"Could not load more. {error.message}")
            return
        finally:
            # Only the newest listing owns the flag. An older one finishing
            # later must not declare the window idle while its replacement runs.
            if generation == self._generation:
                self._loading = False

        if generation != self._generation:
            # Answered for a source, or an opening of this window, that nobody
            # is looking at any more.
            return

        self._cursor = page.next_cursor
        self._has_more = bool(page.has_more and page.next_cursor)
        # Images only. This window exists to fill an image FIELD, which takes an
        # IMG reference, so a library video here is a cell whose only outcome is
        # a rejected run. Filtered client-side because `/library` has no result
        # type parameter to ask with, which also means a page can arrive with
        # nothing usable in it: that is what `has_more` is for, and Load more
        # stays offered while the server says there is more to come.
        usable = [asset for asset in page.items if asset.is_image]
        self._shown += len(usable)
        self._grid.build(
            body,
            usable,
            on_pick=on_pick,
            empty_message="No images here yet.",
            append=cursor is not None,
        )
        self._set_more_enabled(self._has_more)
        self._set_status(self._listing_status(len(usable)))

    def _listing_status(self, arrived: int) -> str:
        """What the line under the buttons says after a page lands.

        A page that filtered down to nothing is the case this exists for. It
        happens on a library with a run of videos in it, and without a word for
        it Load more reads as a button that does nothing: the grid does not
        change, the count does not change, and the only evidence anything
        happened is that the button briefly greyed out.
        """
        if arrived == 0 and self._has_more:
            return "No images on that page. Load more to keep looking."
        if not self._shown:
            return ""
        more = " so far" if self._has_more else ""
        return f"Showing {self._shown} image{'' if self._shown == 1 else 's'}{more}."

    def _set_more_enabled(self, enabled: bool) -> None:
        if self._more_button is None:
            return
        try:
            self._more_button.enabled = enabled
        except Exception:  # noqa: BLE001 - a destroyed button is not a failure
            logger.debug("RunDiffusion: the picker's Load more button is gone.")

    def _pick_file(self, on_pick) -> None:
        """Open a file, upload it, and hand back the reference."""

        def _chosen(path: Path) -> None:
            self._spawn(self._upload(path, on_pick))

        if not saving.open_open_dialog(on_chosen=_chosen):
            self._set_status("A file browser is not available in this app.")

    async def _upload(self, path: Path, on_pick) -> None:
        mime = assets_api.mime_for(path.name)
        if mime is None:
            # Said here rather than at submit: the server allow-lists the type
            # and a wrong one costs a whole generate to discover.
            self._set_status(
                f"{path.suffix or 'That file'} is not an image type RunDiffusion accepts."
            )
            return

        self._set_status(f"Uploading {path.name}...")
        try:
            asset = await assets_api.upload_image(
                self._session,
                filename=path.name,
                content=path.read_bytes(),
                mime_type=mime,
            )
        except (ApiError, OSError) as error:
            message = getattr(error, "message", str(error))
            self._set_status(f"Could not upload: {message}")
            return

        self._set_status("")
        if self._on_uploaded is not None:
            self._on_uploaded()
        if self._window is not None:
            self._window.visible = False
        on_pick(asset)

    def _set_status(self, message: str) -> None:
        if self._status is not None:
            self._status.text = message


#: The viewer's picture area. Same reasoning as the compare window: a fixed
#: size draws predictably, and nobody resizes this.
VIEW_WIDTH = 720
VIEW_HEIGHT = 560


class ImageViewer:
    """One image, big, with the things you would want to do to it.

    Open used to hand the file to the operating system's picture viewer, which
    shows the render and then strands it: everything a person wants next (keep
    this one, send it to RunDiffusion, wipe it against what it was made from)
    is back in a panel they have just been taken out of. So Open now opens a
    window this plugin owns, and the actions come with it.

    The system viewer is still one click away, because it is the right tool for
    zooming into detail and this window deliberately is not.
    """

    def __init__(self) -> None:
        self._window: ui.Window | None = None
        #: Where the arrow keys and the step buttons go, while the picture on
        #: screen is one of a list. None at either end, and for a picture that
        #: is not part of a list at all.
        self._on_previous = None
        self._on_next = None

    def destroy(self) -> None:
        self._on_previous = None
        self._on_next = None
        if self._window is not None:
            self._window.set_key_pressed_fn(None)
            self._window.destroy()
            self._window = None

    def show(
        self,
        path: Path,
        *,
        title: str = "",
        caption: str = "",
        actions: list[tuple[str, object]] | None = None,
        on_previous=None,
        on_next=None,
        position: str = "",
    ) -> None:
        """`actions` are (label, callback) pairs, drawn in the order given.

        `on_previous` and `on_next` step through the list the picture came from,
        with `position` ("3 of 48") between them. Leave all three out for a
        picture that stands alone, and no step row is drawn.
        """
        if self._window is None:
            self._window = ui.Window(
                "RunDiffusion image", width=VIEW_WIDTH + 40, height=VIEW_HEIGHT + 160
            )
            self._window.set_key_pressed_fn(self._on_key_pressed)
        self._window.title = title or "RunDiffusion image"
        self._on_previous = on_previous
        self._on_next = on_next
        stepping = on_previous is not None or on_next is not None or bool(position)

        with self._window.frame:
            with ui.VStack(spacing=8):
                with ui.HStack():
                    ui.Spacer()
                    ui.Image(
                        str(path),
                        width=VIEW_WIDTH,
                        height=VIEW_HEIGHT,
                        fill_policy=ui.FillPolicy.PRESERVE_ASPECT_FIT,
                    )
                    ui.Spacer()

                with ui.VStack(height=0, spacing=6):
                    if stepping:
                        # Step through the library without closing the window
                        # and finding the next picture in the grid behind it.
                        # Disabled at the ends rather than wrapping round, so
                        # the buttons say where in the list you are.
                        with ui.HStack(height=26, spacing=6):
                            previous = ui.Button(
                                "< Previous",
                                width=100,
                                style=STYLE_QUIET_BUTTON,
                                clicked_fn=self.previous,
                            )
                            previous.enabled = on_previous is not None
                            ui.Label(
                                position,
                                alignment=ui.Alignment.CENTER,
                                style=STYLE_SECONDARY,
                            )
                            following = ui.Button(
                                "Next >",
                                width=100,
                                style=STYLE_QUIET_BUTTON,
                                clicked_fn=self.next,
                            )
                            following.enabled = on_next is not None
                    if caption:
                        ui.Label(caption, word_wrap=True, height=0)
                    with ui.HStack(height=26, spacing=4):
                        for label, callback in actions or []:
                            ui.Button(
                                label, clicked_fn=lambda c=callback: c()
                            )
                    ui.Button("Close", height=24, clicked_fn=self.hide)

        self._window.visible = True
        if stepping:
            # Keys go to the focused window only, and showing one does not
            # focus it, so the arrow keys would reach whatever had focus before.
            try:
                self._window.focus()
            except Exception:  # noqa: BLE001 - the buttons still step
                logger.debug("RunDiffusion: could not focus the image viewer.")

    def previous(self) -> None:
        if self._on_previous is not None:
            self._on_previous()

    def next(self) -> None:
        if self._on_next is not None:
            self._on_next()

    def _on_key_pressed(self, key, _modifiers, pressed) -> None:
        """Left and right step through the list, like any picture viewer."""
        if not pressed:
            return
        try:
            import carb.input

            # Integers, as Kit's own windows compare them: the key arrives as
            # an int, which is never equal to the enum member itself.
            left = int(carb.input.KeyboardInput.LEFT)
            right = int(carb.input.KeyboardInput.RIGHT)
        except Exception:  # noqa: BLE001 - no carb means no keys to read
            return
        if key == left:
            self.previous()
        elif key == right:
            self.next()

    def hide(self) -> None:
        if self._window is not None:
            self._window.visible = False

    @property
    def is_showing(self) -> bool:
        return self._window is not None and self._window.visible
