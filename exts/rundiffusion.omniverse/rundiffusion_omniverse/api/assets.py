"""Uploads and library: the two places an image can come from besides the viewport.

Both publish the same idea in the same shape, an item with an `id`, a signed
`url` and a `thumb_url`, so this module gives them one type and lets the panel
show them the same way.

The reference each becomes is the point of the whole thing, and v2's rule is
that a reference uses the id the endpoint handed out, under the name it handed
it out as:

    {"kind": "UPLOAD_REF",  "id": "<the id /uploads returned>"}
    {"kind": "LIBRARY_REF", "id": "<the WHOLE id /library returned>"}

The library id is compound, `{run_id}:{result_id}`, and v2 takes it back whole.
v1 made callers split it on the colon and re-case both halves, which every
integration had to write and which produced a reference matching nothing when
done wrong.

Signed URLs last 7 days. They are re-listed rather than stored, which is why
nothing here caches a url.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import media
from .constants import api_base_url
from .session import RdSession
from .transport import post_multipart_async, request_json_async, with_query

logger = logging.getLogger(__name__)

#: What the server accepts for a direct upload.
UPLOAD_ALLOWED_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


@dataclass
class AssetPage:
    """One page of assets, plus how to ask for the next.

    Cursor pagination rather than offsets, because the library is large and a
    docked panel shows a screenful at a time. `next_cursor` is opaque and is
    passed back untouched.
    """

    items: list
    next_cursor: str | None = None
    has_more: bool = False


@dataclass
class Asset:
    """One item from either source, and what it actually is.

    `kind` is the REFERENCE kind, UPLOAD_REF or LIBRARY_REF, which says which
    endpoint it came from and how to name it in a request. It says nothing
    about the file, and reading it as though it did is how the library came to
    treat a generated video as a picture: downloaded as `.png`, opened in a
    viewer that draws images as an empty frame, and offered to an IMG field
    that would refuse it. `media_kind` is the other question, asked of the
    `type` and `mime_type` the listing publishes.
    """

    id: str
    kind: str  # UPLOAD_REF | LIBRARY_REF
    url: str | None
    thumb_url: str | None
    name: str | None = None
    created: str | None = None
    #: The server's result type (IMG, VID, ASSET_3D...). Absent on an upload,
    #: which is always an image because the endpoint accepts nothing else.
    type: str | None = None
    mime_type: str | None = None

    @property
    def preview_url(self) -> str | None:
        """What to show in a grid.

        `thumb_url` falls back to the full image server-side when the thumbnail
        has not been generated yet, so a client always has something usable.
        """
        return self.thumb_url or self.url

    @property
    def drawable_preview_url(self) -> str | None:
        """The URL worth DOWNLOADING to draw a thumbnail from, or None.

        Not the same question as `preview_url`. `thumb_url` falls back to the
        full file server-side when no thumbnail has been generated, which is
        exactly right for an image and useless for anything else: it means a
        grid holding a two-minute video downloads the whole thing to fail to
        decode it, and does so again on every redraw, because a failure is not
        cached. Six pages of a video-heavy library made that a lot of bytes for
        a cell that ends up reading "Video" either way.

        A real thumbnail is a different object at a different URL, so having
        one is what distinguishes the two cases.
        """
        if self.is_image:
            return self.preview_url
        if self.thumb_url and self.thumb_url != self.url:
            return self.thumb_url
        return None

    @property
    def media_kind(self) -> str:
        """What this plugin would have to draw to show it."""
        return media.kind_for(self.type, self.mime_type)

    @property
    def is_image(self) -> bool:
        """Whether it can be drawn, compared, or sent to an image field."""
        return self.media_kind == media.KIND_IMAGE

    @property
    def extension(self) -> str:
        """The suffix to write it under, including the dot."""
        return media.extension_for(self.mime_type, self.url)

    def as_input_ref(self) -> dict[str, str]:
        """The descriptor that puts this asset in an image field."""
        return {"kind": self.kind, "id": self.id}


#: Aspect buckets `/library` accepts, with the wording the web client shows for
#: each. The two surfaces filter the same column, so they name the buckets the
#: same way: someone who learned "Landscape" in the browser should not have to
#: relearn it here. "Any" is first and means the parameter is omitted entirely
#: rather than sent empty.
ASPECT_CHOICES: tuple[tuple[str, str | None], ...] = (
    ("Any aspect", None),
    ("1:1 (Square)", "1:1"),
    ("16:9 (Landscape)", "16:9"),
    ("2:1", "2:1"),
    ("3:2", "3:2"),
    ("4:3", "4:3"),
    ("21:9 (Ultrawide)", "21:9"),
    ("9:16 (Portrait)", "9:16"),
    ("2:3", "2:3"),
    ("3:4", "3:4"),
    ("Other", "OTHER"),
)

#: Resolution buckets, by the long edge. Same three the server defines and the
#: web client offers.
RESOLUTION_CHOICES: tuple[tuple[str, str | None], ...] = (
    ("Any resolution", None),
    ("1K (~1024px)", "1K"),
    ("2K (~2048px)", "2K"),
    ("4K+ (4096px and up)", "4K+"),
)

#: How far back to look, as a window rather than as two dates.
#:
#: The web client offers a From and a To through a date picker, which is a
#: control a docked Kit panel does not have: the honest alternatives are a
#: text box wanting a typed YYYY-MM-DD, or the windows below. A window is what
#: people are actually reaching for when they narrow a history by date, and it
#: cannot be typed wrong.
DATE_CHOICES: tuple[tuple[str, int | None], ...] = (
    ("Any time", None),
    ("Past 24 hours", 1),
    ("Past 7 days", 7),
    ("Past 30 days", 30),
    ("Past 12 months", 365),
)

#: Favourites, as a two-way choice rather than a checkbox, so the default state
#: says what it is showing rather than leaving it to be inferred.
FAVORITE_CHOICES: tuple[tuple[str, bool], ...] = (
    ("All generations", False),
    ("Favorited only", True),
)


@dataclass
class LibraryFilters:
    """What the Library tab is currently narrowed to.

    One object rather than seven parameters because every one of them has to
    survive a tab rebuild, be compared against what is on screen to know whether
    a refetch is needed, and be counted for the "3 filters" chip. Seven loose
    attributes did the first of those and none of the rest.

    Every field is the id or bucket the API takes, never a label: a display
    string that has to be mapped back at request time is a mapping that gets
    written twice and disagrees once.
    """

    tool_id: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    model_family_tool_tag_id: str | None = None
    media_tool_tag_id: str | None = None
    favorited: bool = False
    #: The date window in days, matching a `DATE_CHOICES` value. None is any time.
    within_days: int | None = None

    @property
    def active_count(self) -> int:
        """How many filters are narrowing the result, for the chip on the header.

        A count is what makes a collapsed filter panel safe to collapse: without
        it, a library narrowed to one tool and one aspect looks like a library
        with four items in it.
        """
        return len([value for value in self.as_params() if value])

    @property
    def is_active(self) -> bool:
        return self.active_count > 0

    def as_params(self, *, now: datetime | None = None) -> dict[str, str]:
        """The query parameters, with everything unset left out entirely.

        Omitted rather than sent empty, because `favorited=false` and no
        `favorited` at all are the same request and only one of them reads as a
        filter that is off.

        `now` is injectable so the date window is testable without freezing the
        clock.
        """
        params: dict[str, str] = {}
        if self.tool_id:
            params["tool_id"] = self.tool_id
        if self.aspect_ratio:
            params["aspect_ratio"] = self.aspect_ratio
        if self.resolution:
            params["resolution"] = self.resolution
        if self.model_family_tool_tag_id:
            params["model_family_tool_tag_id"] = self.model_family_tool_tag_id
        if self.media_tool_tag_id:
            params["media_tool_tag_id"] = self.media_tool_tag_id
        if self.favorited:
            params["favorited"] = "true"
        if self.within_days:
            moment = now or datetime.now(timezone.utc)
            start = moment.astimezone(timezone.utc) - timedelta(days=self.within_days)
            # No `end_utc`. The window ends now, and naming that instant would
            # exclude anything generated between building the request and the
            # server reading it, which on a library sorted newest-first is
            # exactly what the user is looking for.
            params["start_utc"] = start.isoformat().replace("+00:00", "Z")
        return params


def mime_for(filename: str) -> str | None:
    """The MIME to declare for a file the user picked, or None if unsupported.

    Declared from the extension rather than sniffed: the server validates the
    part's content type against an allow-list and answers 415 for anything else,
    so sending a generic type fails the upload outright.
    """
    lowered = filename.lower()
    for suffix, mime in UPLOAD_ALLOWED_MIMES.items():
        if lowered.endswith(suffix):
            return mime
    return None


async def list_uploads(
    session: RdSession, limit: int = 24, *, cursor: str | None = None
) -> AssetPage:
    payload = await request_json_async(
        "GET",
        with_query(
            f"{api_base_url()}/uploads",
            {"limit": limit, "cursor": cursor, **session.account_params()},
        ),
        headers=await session.auth_headers(),
    )
    items = [
        Asset(
            id=item["id"],
            kind="UPLOAD_REF",
            url=item.get("url"),
            thumb_url=item.get("thumb_url"),
            name=item.get("name"),
            created=item.get("created"),
            # `/uploads` publishes no `type`, and this states the one it
            # would have. The endpoint accepts jpeg, png, webp, heic and heif
            # and nothing else, so an upload is an image by construction, and
            # leaving it unstated made every upload whose `mime_type` was
            # absent read as OTHER: filtered out of the image picker, and
            # written to disk as `.bin`.
            type="IMG",
            mime_type=item.get("mime_type"),
        )
        for item in (payload or {}).get("data") or []
    ]
    return AssetPage(
        items=items,
        next_cursor=(payload or {}).get("next_cursor"),
        has_more=bool((payload or {}).get("has_more")),
    )


async def list_library(
    session: RdSession,
    limit: int = 24,
    *,
    cursor: str | None = None,
    filters: "LibraryFilters | None" = None,
) -> AssetPage:
    """A page of the caller's library, narrowed by `filters`.

    The filters are the ones this panel can offer honestly. The library surface
    is explicit that its parameters are a FILTER and never a search: there is no
    free-text prompt query, so offering a search box would promise something the
    API does not do.
    """
    payload = await request_json_async(
        "GET",
        with_query(
            f"{api_base_url()}/library",
            {
                "limit": limit,
                "cursor": cursor,
                **(filters or LibraryFilters()).as_params(),
                **session.account_params(),
            },
        ),
        headers=await session.auth_headers(),
    )
    items = []
    for item in (payload or {}).get("data") or []:
        # v2 withholds TEXT results from this surface, but a defensive check
        # costs nothing and a text row in an image grid would be a puzzle.
        if (item.get("type") or "").upper() == "TEXT":
            continue
        items.append(
            Asset(
                id=item["id"],
                kind="LIBRARY_REF",
                url=item.get("url"),
                thumb_url=item.get("thumb_url"),
                name=item.get("tool_id"),
                created=item.get("created"),
                # Carried, not discarded. This surface returns IMG, VID,
                # ASSET_3D and LAYERS, and everything downstream that writes a
                # file or draws a picture needs to know which.
                type=item.get("type"),
                mime_type=item.get("mime_type"),
            )
        )
    return AssetPage(
        items=items,
        next_cursor=(payload or {}).get("next_cursor"),
        has_more=bool((payload or {}).get("has_more")),
    )


async def upload_image(
    session: RdSession, *, filename: str, content: bytes, mime_type: str
) -> Asset:
    """Send bytes to /uploads so they persist beyond this run.

    This is what "Send to RunDiffusion" does with a render. A generated result
    lives in the Generate bucket for about 14 days and is not in the library;
    uploading it makes a durable copy that can also be fed back into another
    generation as an UPLOAD_REF.
    """
    payload = await post_multipart_async(
        with_query(f"{api_base_url()}/uploads", session.account_params()),
        fields={},
        files=[("file", filename, content, mime_type)],
        headers=await session.auth_headers(),
    )
    item: dict[str, Any] = payload or {}
    return Asset(
        id=item["id"],
        kind="UPLOAD_REF",
        url=item.get("url"),
        thumb_url=item.get("thumb_url"),
        name=item.get("name") or filename,
        created=item.get("created"),
        type="IMG",
        # The declared type is the fallback: it is what the server just
        # validated the bytes against, so it cannot disagree with them.
        mime_type=item.get("mime_type") or mime_type,
    )
