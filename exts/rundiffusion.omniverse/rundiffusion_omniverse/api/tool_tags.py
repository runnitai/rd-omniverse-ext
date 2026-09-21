"""The tag vocabulary a tool carries, so a filter can offer labels and not ids.

`GET /api/v2/tool-tags` exists precisely because tools reference tags by id and
nothing else resolved them. It answers `{"data": [{id, type, label}], "types":
[...]}`, unpaged: the whole taxonomy is under a hundred rows and a client asking
at all wants all of it to build a filter with.

Grouping is by `type` (`MODEL_FAMILY`, `USE_CASE`, `MEDIA`, `INDUSTRY`, and
whatever is added next). An unfamiliar type is a new group rather than an error,
which is why nothing here enumerates them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .constants import api_base_url
from .session import RdSession
from .transport import request_json_async, with_query

logger = logging.getLogger(__name__)

#: The two taxonomies this panel filters on, spelled as the API spells them.
#: They are the two the web client offers, and offering the same two is the
#: point: a filter that behaves differently in two places is worse than a
#: filter that is missing from one.
TYPE_MODEL_FAMILY = "MODEL_FAMILY"
TYPE_MEDIA = "MEDIA"


@dataclass(frozen=True)
class ToolTag:
    """One tag, in the shape it also takes inside a tool's `tool_tags`."""

    id: str
    type: str | None
    label: str | None

    def __str__(self) -> str:  # what a dropdown row shows
        return self.label or self.id


def group_by_type(tags: list[ToolTag]) -> dict[str, list[ToolTag]]:
    """Tags bucketed by taxonomy, each bucket sorted by what is displayed.

    Sorted here rather than trusted from the response: the endpoint publishes no
    order, and a dropdown whose rows move between sessions is one nobody learns
    the shape of.
    """
    grouped: dict[str, list[ToolTag]] = {}
    for tag in tags:
        grouped.setdefault(tag.type or "", []).append(tag)
    for bucket in grouped.values():
        bucket.sort(key=lambda tag: (tag.label or tag.id).lower())
    return grouped


async def list_tool_tags(session: RdSession) -> list[ToolTag]:
    """Every tag this account may see. Empty on anything unexpected.

    THIS NEVER RAISES, and the breadth of that is deliberate. A failure costs
    the two tag dropdowns and nothing else: the library still lists, the other
    five filters still work, and the rows for these two say so. A tab that
    refused to open because its filter vocabulary did not arrive would be a far
    larger loss than the filter, and the caller spawns this as a background
    task, where an escaping exception is not a failure anybody sees.

    Broad rather than `ApiError` alone, because the shapes are as likely to
    disappoint as the request: a `data` that is not a list, or a row that is not
    an object, are both a `TypeError` on the way to a filter nobody needs.
    """
    try:
        payload = await request_json_async(
            "GET",
            with_query(f"{api_base_url()}/tool-tags", session.account_params()),
            headers=await session.auth_headers(),
        )
        rows = (payload or {}).get("data") or []
        return [
            ToolTag(
                id=item["id"],
                type=item.get("type"),
                label=item.get("label"),
            )
            for item in rows
            if item.get("id")
        ]
    except asyncio.CancelledError:
        # Sign-out and account switches cancel this, and a cancellation that is
        # swallowed here would be one the task never actually honours.
        raise
    except Exception as error:  # noqa: BLE001 - a lost vocabulary costs a filter
        logger.info(
            "RunDiffusion: could not load tool tags, so the Model family and "
            "Media filters are unavailable. %s",
            error,
        )
        return []
