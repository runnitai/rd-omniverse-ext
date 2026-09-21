"""A grid of image thumbnails, used by the Library and Uploads tabs and the picker.

omni.ui.Image loads from a path, not from bytes and not from an http URL, so
every thumbnail has to be fetched and written to disk before it can be shown.
That is the whole reason this class exists rather than being three lines inside
a tab.

Two properties worth keeping:

**Nothing blocks the UI.** Thumbnails are fetched off the update loop and the
grid fills in as they land, so a slow page never freezes the viewport.

**A failed thumbnail loses one cell, not the grid.** A signed URL that has
expired, or a single 404, leaves that item as a labelled placeholder while the
rest render.
"""

from __future__ import annotations

import asyncio
import time
import urllib.error
import io
import logging
from pathlib import Path

import omni.ui as ui

from .api import media
from .api.assets import Asset
from .api.transport import download_async

logger = logging.getLogger(__name__)

#: How many images may be in flight at once. The avatar endpoint is rate
#: limited per client, and a browser holding the whole tool catalogue can ask
#: for a hundred of them in one scroll. This does not make that fit the budget
#: on its own (see the retry), but it stops the burst that makes tripping it
#: certain.
_MAX_CONCURRENT_FETCHES = 6

#: Statuses worth asking about again. 429 is the throttle and clears on its own
#: within the minute; 5xx is the server having a moment. A 404 is an avatar
#: that does not exist, and retrying that forever would be a busy loop against
#: a settled answer.
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Requests per second the fetcher allows itself, and how many it may spend at
#: once before that rate starts to bite.
#:
#: The avatar endpoint is rate limited per client, and the catalogue holds more
#: tools than that limit allows in one window, so browsing the whole list cannot
#: fit inside the budget however carefully it is gated: sooner or later a
#: thorough look asks for more pictures than the window allows. The choice is
#: between sprinting until refused and pacing to stay under, and the second is
#: the only one that does not end in a blackout. This rate sits under the limit
#: with room for the panel's own avatar traffic from the same client.
_FETCH_RATE_PER_SECOND = 1.6
#: A screenful arrives at once and should feel instant, so the bucket starts
#: full and the rate only matters to someone travelling through the catalogue.
_FETCH_BURST = 20


def _is_retryable(error: Exception) -> bool:
    status = getattr(error, "code", None)
    if isinstance(status, int):
        return status in _RETRYABLE_STATUSES
    # No status at all is a transport failure (DNS, reset, timeout), which is
    # the most retryable thing there is.
    return isinstance(error, (OSError, urllib.error.URLError))


def _retry_after_seconds(error: Exception) -> float | None:
    """Whatever the server said to wait, if it said anything."""
    headers = getattr(error, "headers", None)
    if headers is None:
        return None
    try:
        return max(0.0, float(headers.get("Retry-After")))
    except (TypeError, ValueError):
        return None



def _write_as_png(data: bytes, path: Path) -> bool:
    """Decode whatever came back and write a real PNG. True on success.

    The bytes are NOT necessarily a PNG. Library and upload thumbnails are
    512x512 **WebP**, and Kit's texture loader cannot read WebP: writing those
    bytes under a .png name produced a wall of

        STB Failed to load image info: Image not of any known type, or corrupt

    and an empty grid. Naming the file .webp would not help either, because the
    loader is the limitation rather than the extension. So the bytes are decoded
    and re-encoded through the prebundled Pillow, which turns any format the
    server sends into one the host can definitely display.
    """
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            # Flattened to RGB(A) because a palette or CMYK image saves to a PNG
            # the loader handles less predictably.
            image.convert("RGBA").save(path, format="PNG")
        return True
    except Exception:  # noqa: BLE001 - a bad image costs one cell
        logger.debug("RunDiffusion: could not decode an image for %s.", path.name)
        return False

#: Cells per row at the panel's usual docked width.
_COLUMNS = 3
_CELL = 96

#: The action bar revealed over the bottom of a cell while the pointer is on
#: it. Tall enough to press without covering enough of a 96px thumbnail to stop
#: it being recognisable, which is the whole reason it hides again.
_ACTIONS_HEIGHT = 20


def _reveal_on_hover(cell: ui.Widget, bar: ui.Widget) -> None:
    """Show `bar` while the pointer is anywhere on `cell`.

    On the cell rather than the bar because the bar starts hidden and a hidden
    widget is never hovered. On the whole cell rather than on the picture alone
    because the bar is part of the cell, and a reveal that ended the moment the
    pointer reached what it revealed would be a button nobody can press.
    """

    def on_hovered(hovered: bool) -> None:
        # Guarded: a rebuild can destroy the cell while the pointer is still on
        # it, and a destroyed widget still answers.
        try:
            bar.visible = hovered
        except Exception:  # noqa: BLE001 - a stale cell costs nothing
            logger.debug("RunDiffusion: a cell's actions outlived their cell.")

    cell.set_mouse_hovered_fn(on_hovered)


class AssetGrid:
    """Renders assets into a container and reports which one was clicked."""

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._tasks: set[asyncio.Task] = set()
        #: Bumped every time the widgets this grid was filling are thrown away.
        #: A fetch carries the generation it started in and refuses to paint if
        #: that is no longer the current one. See `cancel_pending`.
        self._generation = 0
        #: Lazily made, because a semaphore binds to the loop that creates it
        #: and this object outlives none but is built before one is running.
        self._gate: asyncio.Semaphore | None = None
        #: Token bucket, so a long browse paces itself instead of being paced
        #: by the server refusing it. Starts full: the first screenful is free.
        self._tokens = float(_FETCH_BURST)
        self._tokens_checked = time.monotonic()
        #: The VGrid the last full build made. Kept because `append` adds cells
        #: to THAT widget rather than to the container: an omni.ui Frame holds
        #: one child, so re-entering the container to add a second grid would
        #: replace the first rather than sit under it.
        self._cells: ui.VGrid | None = None

    def destroy(self) -> None:
        self.cancel_pending()

    def cancel_pending(self) -> None:
        """Abandon every fetch in flight, because its target is being replaced.

        A caller that rebuilds its container MUST call this first. An image
        fetch outlives the widget it was started for: the await returns after
        the rebuild, and the frame it captured is one the container has already
        destroyed. Painting into that frame puts a picture belonging to one row
        onto whatever now occupies its place, which reads as the wrong avatar
        against the wrong tool and moves around between runs, since it depends
        only on which fetches happened to be in the air at the click.

        Cancelling is not sufficient on its own. A task already past its await
        is not interrupted by `cancel`, so the generation check is what stops
        that last one, and both are needed.
        """
        self._generation += 1
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        # The grid this was filling is going away with everything else, so an
        # `append` after this must start a fresh one rather than add cells to a
        # widget that has been destroyed.
        self._cells = None

    def build(
        self,
        container: ui.Widget,
        assets: list[Asset],
        *,
        on_pick=None,
        actions_for=None,
        empty_message: str = "Nothing here yet.",
        append: bool = False,
    ) -> None:
        """Fill `container` with a grid. `on_pick` receives the Asset clicked.

        `actions_for(asset)` returns (label, callback) pairs put in a bar over
        the bottom of that cell, shown only while the pointer is on it. It is
        what lets an item be acted on where it is rather than selected first
        and acted on somewhere else, and it is optional: the picker wants a
        click to mean pick and nothing else.

        `append` adds these assets UNDER what is already on screen instead of
        replacing it, which is what makes Load more feel like more rather than
        like a jump: the page you were looking at stays where it was, and the
        scroll position with it.

        Appending goes into the grid widget the last full build kept, not into
        `container`. A rebuild of the container would throw away every cell
        whose thumbnail is still in flight, which is the whole cost Load more
        exists to avoid.
        """
        if append and self._cells is not None:
            with self._cells:
                for asset in assets:
                    self._build_cell(asset, on_pick, actions_for)
            return

        container.clear()
        self._cells = None

        if not assets:
            with container:
                ui.Label(empty_message, word_wrap=True)
            return

        with container:
            with ui.VStack(spacing=6, height=0):
                self._cells = ui.VGrid(column_count=_COLUMNS, row_height=_CELL, height=0)
                with self._cells:
                    for asset in assets:
                        self._build_cell(asset, on_pick, actions_for)

    def _build_cell(self, asset: Asset, on_pick, actions_for=None) -> None:
        # A frame per cell so the thumbnail can replace the placeholder in place
        # once it arrives, without rebuilding the whole grid.
        cell = ui.Frame(height=_CELL)
        with cell:
            ui.Label("...", alignment=ui.Alignment.CENTER)

        url = asset.drawable_preview_url
        if not url:
            with cell:
                ui.Label(
                    _placeholder_for(asset) if not asset.is_image else "No preview",
                    alignment=ui.Alignment.CENTER,
                    word_wrap=True,
                )
            return

        self._spawn(self._fill_cell(cell, asset, url, on_pick, actions_for))

    def _spawn(self, coroutine) -> None:
        # Keep the reference: an unreferenced task can be collected before it
        # runs, which here would look like thumbnails that never load.
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fill_cell(
        self, cell: ui.Frame, asset: Asset, url: str, on_pick, actions_for=None
    ) -> None:
        """One cell's thumbnail, through the same cache and pacing as the rest.

        Through `_fetch` rather than straight to the network, and that is what
        makes paging affordable: a grid that has loaded ten pages is redrawn
        whenever its tab is shown again, and re-downloading two hundred and
        forty thumbnails to redraw pictures already on disk is the difference
        between paging deep being free and being unusable.

        Unpaced, though. The rate limiter is the avatar endpoint's, and a
        screenful of storage thumbnails must not queue behind a budget that
        exists for a different host.
        """
        generation = self._generation
        path, _retryable, _wait = await self._fetch(
            url, cache_key=f"thumb-{asset.id}", paced=False
        )

        if generation != self._generation:
            return

        if path is None:
            logger.debug("RunDiffusion: could not fetch a thumbnail for %s.", asset.id)
            cell.clear()
            with cell:
                ui.Label(
                    _placeholder_for(asset),
                    alignment=ui.Alignment.CENTER,
                    word_wrap=True,
                )
            return

        cell.clear()
        with cell:
            if on_pick is None:
                ui.Image(str(path), height=_CELL, fill_policy=ui.FillPolicy.PRESERVE_ASPECT_FIT)
            else:
                # Assigned before it is entered, the way the grid itself is: a
                # handle on the stack is needed after the block to hang the
                # hover on.
                stack = ui.ZStack()
                with stack:
                    ui.Image(
                        str(path),
                        height=_CELL,
                        fill_policy=ui.FillPolicy.PRESERVE_ASPECT_FIT,
                    )
                    bar = self._build_click_layer(asset, on_pick, actions_for)
                if bar is not None:
                    _reveal_on_hover(stack, bar)

    def _build_click_layer(self, asset: Asset, on_pick, actions_for):
        """The part of a cell that answers a click. Returns its hover bar.

        With no actions this is what it always was: one invisible button over
        the whole picture, so the click target is the cell rather than a
        caption under it. The image picker wants exactly that, where a click
        means pick and nothing else.

        With actions it is a COLUMN, and the column is the point. The first
        draft stacked the bar over the cell-wide invisible button inside the
        ZStack, on the assumption that a later child of a back-to-front stack
        takes the click. It does not: omni.ui hit-tests in the order widgets
        are issued, so the invisible button underneath claimed every press and
        the buttons drawn on top of it were dead while looking live. The cell
        opened its selection row instead of running the action. Reported from
        Base Editor on 2026-08-27.

        Two widgets that never overlap cannot disagree about who was clicked,
        so the invisible button now takes the space ABOVE the bar rather than
        the space behind it. A hidden widget takes no room in a stack, so while
        the bar is hidden the button is the whole cell exactly as before, and
        it gives up its bottom edge only for as long as there is something
        there to give it up to.
        """
        actions = actions_for(asset) if actions_for is not None else None
        if not actions:
            ui.InvisibleButton(clicked_fn=lambda a=asset: on_pick(a))
            return None

        with ui.VStack():
            ui.InvisibleButton(clicked_fn=lambda a=asset: on_pick(a))
            bar = ui.HStack(height=_ACTIONS_HEIGHT, spacing=2)
            with bar:
                for label, callback in actions:
                    ui.Button(
                        label,
                        height=_ACTIONS_HEIGHT,
                        clicked_fn=lambda c=callback, a=asset: c(a),
                    )
        # Built hidden. A bar that flashes on every redraw of a grid that is
        # rebuilt on every tab switch would be worse than no bar.
        bar.visible = False
        return bar

    def load_image_into(
        self, frame: ui.Frame, url: str, *, size: int = _CELL, on_done=None
    ) -> None:
        """Fetch one image and put it in `frame`.

        Used for tool avatars and for the preview on a chosen image field.

        Kept here rather than duplicated in the browser: same fetch, same cache,
        and the same "one dead URL costs one cell" rule.

        `on_done(loaded, retryable, retry_after)` is called when the fetch stops
        mattering, whether it painted, failed, or was abandoned. A caller
        animating a placeholder needs to know when to stop as much as it needs
        to know whether it worked, and a caller showing a grid of two hundred
        of these needs to know that a throttled one is worth asking for again.
        """
        self._spawn(self._fill_image(frame, url, size, on_done))

    async def fetch_to_path(self, url: str) -> Path | None:
        """Put one image on disk as a PNG and return where. None if it cannot be.

        The same fetch, cache and WebP-to-PNG decode the grids use, without a
        widget to put it in: the compare window needs a file path for an image
        the user chose from their library, and it needs it before it can draw
        anything at all.
        """
        # A file already here is this exact image: the name is derived from the
        # URL, signature and all, so a hit cannot be a different picture. Worth
        # checking because a field's previews are redrawn whenever the viewport
        # thumbnail moves, and refetching an avatar per camera move is waste.
        path, _retryable, _wait = await self._fetch(url)
        return path

    async def _take_token(self) -> None:
        """Wait until this fetch fits inside the rate.

        Refilled by the clock rather than by a timer, so a browser that sits
        idle for a minute comes back with a full bucket and the next screenful
        is instant again.
        """
        while True:
            now = time.monotonic()
            self._tokens = min(
                float(_FETCH_BURST),
                self._tokens + (now - self._tokens_checked) * _FETCH_RATE_PER_SECOND,
            )
            self._tokens_checked = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await asyncio.sleep((1.0 - self._tokens) / _FETCH_RATE_PER_SECOND)

    async def _fetch(
        self, url: str, *, cache_key: str | None = None, paced: bool = True
    ) -> tuple[Path | None, bool, float | None]:
        """`(path, retryable, retry_after)`.

        The two extra values are what turns a failed avatar from a permanent
        blank into something worth asking about again. Throwing them away is
        how a 429 came to look identical to a deleted image.

        `cache_key` names the file when the caller has something steadier than
        the URL to name it by. A library thumbnail's URL carries a signature
        that is re-minted on every listing, so keying on it would miss on every
        reload; keying on the asset id hits, because an id names one picture
        forever.

        `paced` spends a token from the rate limiter, and only avatars should.
        The budget exists for the avatar endpoint, which is rate limited per
        client against a large catalogue. A library thumbnail is a signed
        object on storage, throttled by nothing, and putting a screenful of them
        through a 1.6/s bucket makes a page take a quarter of a minute to fill.
        The concurrency gate below still applies to both, because the reason for
        that one is the client rather than the server.
        """
        name = _safe(cache_key) if cache_key else _safe(url)[-60:]
        cached = self._cache_dir / f"img-{name}.png"
        if cached.exists():
            return cached, False, None

        if self._gate is None:
            self._gate = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)

        try:
            if paced:
                await self._take_token()
            async with self._gate:
                data = await download_async(url)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - one dead URL costs one cell
            retryable = _is_retryable(error)
            # At info, not debug. This being invisible is how a hundred
            # throttled avatars read as "the images just do not load".
            logger.info(
                "RunDiffusion: could not fetch an image (%s, retryable=%s).",
                getattr(error, "code", type(error).__name__),
                retryable,
            )
            return None, retryable, _retry_after_seconds(error)

        return (cached, False, None) if _write_as_png(data, cached) else (None, False, None)

    async def _fill_image(
        self, frame: ui.Frame, url: str, size: int, on_done=None
    ) -> None:
        generation = self._generation
        cached = None
        retryable = False
        retry_after = None
        try:
            cached, retryable, retry_after = await self._fetch(url)
            if cached is None:
                return
            if generation != self._generation:
                # The container was rebuilt while this was fetching, so `frame`
                # is a widget nobody is looking at any more.
                return

            frame.clear()
            with frame:
                ui.Image(
                    str(cached),
                    height=size,
                    fill_policy=ui.FillPolicy.PRESERVE_ASPECT_FIT,
                )
        finally:
            # In a finally so cancellation counts too: `cancel_pending` is the
            # normal way one of these ends. A cancelled fetch reports as not
            # retryable because the rebuild that cancelled it registers the
            # card again anyway.
            if on_done is not None:
                on_done(cached is not None, retryable, retry_after)


def _placeholder_for(asset: Asset) -> str:
    """What to write in a cell with no picture in it.

    A generated video's thumbnail is a still the server has not made yet, so
    `thumb_url` falls back to the video itself and the decode fails. Labelling
    that "Unavailable" is wrong twice over: the file is available, and the cell
    stops saying the one useful thing about it.
    """
    if asset.media_kind == media.KIND_VIDEO:
        return "Video"
    if asset.media_kind == media.KIND_ASSET_3D:
        return "3D asset"
    return "Unavailable"


def _safe(value: str) -> str:
    """A library id is `run:result`, and a colon is not a filename."""
    return "".join(character if character.isalnum() else "-" for character in value)
