"""Kits: the tool catalogue re-cut by intent, so a picker can offer a shortlist.

`GET /api/v2/kits` returns the curation web has rendered for some time, and the
server does the work that would otherwise be repeated in every plugin: the tools
inside a kit come back RESOLVED, in the same shape `/tools` returns, already
filtered to what this account can run, with a kit whose every tool is invisible
dropped entirely. So this module parses and does not decide.

Two things about the request are worth knowing here rather than being rediscovered:

**The surface is derived, not asked for.** There is no `?surface=` parameter.
The server reads the client kind off the device record written at device-flow
completion, which is why `X-Device-Id` on the request is what makes this panel
receive Omniverse's curation rather than everyone's. The endpoint fails CLOSED:
without that header a caller is placed on no surface and sees only kits that opt
into every surface. `session.auth_headers()` sends it whenever there is one, so
this is a reason not to remove it rather than something to add.

**Unpaged.** There are a handful of kits and a picker wants all of them, so
there is no cursor to follow and none is invented here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .constants import api_base_url
from .session import RdSession
from .tools import ToolSummary
from .transport import request_json_async, with_query

logger = logging.getLogger(__name__)

#: The tab shown first inside a kit, which is this panel's own and never comes
#: from the server: `kit_tags` are the curator's tabs, and a person opening a
#: kit wants to see what is in it before narrowing.
ALL_TAB = "All"


@dataclass(frozen=True)
class KitTool:
    """One tool as a kit presents it: the catalogue entry plus its curation."""

    tool: ToolSummary
    #: Which tabs inside THIS kit the tool appears under. Editorial keys chosen
    #: by a curator, unrelated to the catalogue-wide `tool_tags`.
    kit_tags: tuple[str, ...] = ()
    #: Small capability badges ("Fast", "New"). They never filter, which is the
    #: whole difference between a label and a kit tag.
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class Kit:
    """One curated collection, ready to render."""

    id: str
    label: str
    description: str = ""
    #: The tab row, in display order, derived by the server from the tools it
    #: actually returned, so no tab can render empty.
    kit_tags: tuple[str, ...] = ()
    default_tool_id: str | None = None
    tools: tuple[KitTool, ...] = field(default_factory=tuple)

    @property
    def tabs(self) -> tuple[str, ...]:
        """The tab row as this panel draws it: everything, then the curator's."""
        return (ALL_TAB, *self.kit_tags)

    def tools_for_tab(self, tab: str | None) -> list[KitTool]:
        if not tab or tab == ALL_TAB:
            return list(self.tools)
        return [entry for entry in self.tools if tab in entry.kit_tags]

    def __str__(self) -> str:  # what a card shows
        return self.label or self.id


def _strings(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values if isinstance(value, str) and value)


def _labels(values: Any) -> tuple[str, ...]:
    """A label is `{id, icon, label}`, and only its text is drawable here.

    The icon is a Material Design Icons name, which omni.ui has no way to draw,
    so it is dropped rather than rendered as the literal string `mdi-flash`.
    """
    if not isinstance(values, list):
        return ()
    out = []
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("label"), str):
            text = value["label"].strip()
            if text:
                out.append(text)
    return tuple(out)


def _kit_tool(entry: Any) -> KitTool | None:
    if not isinstance(entry, dict):
        return None
    tool = entry.get("tool")
    if not isinstance(tool, dict) or not tool.get("id"):
        return None
    return KitTool(
        tool=ToolSummary(
            id=tool["id"],
            name=tool.get("name") or tool["id"],
            avatar_url=tool.get("avatar_url"),
            description=tool.get("description"),
        ),
        kit_tags=_strings(entry.get("kit_tags")),
        labels=_labels(entry.get("labels")),
    )


def parse_kits(payload: Any) -> list[Kit]:
    """Read the listing. Order is the server's and is the order to render.

    A kit with nothing in it is skipped. The server already drops those, so this
    is the same rule surviving a partial response rather than a second one: a
    card that opens onto an empty list is the failure both are avoiding.
    """
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []

    kits = []
    for raw in data:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        tools = tuple(
            entry
            for entry in (_kit_tool(item) for item in raw.get("tools") or [])
            if entry is not None
        )
        if not tools:
            continue
        kits.append(
            Kit(
                id=str(raw["id"]),
                label=str(raw.get("label") or raw["id"]),
                description=str(raw.get("description") or ""),
                kit_tags=_strings(raw.get("kit_tags")),
                default_tool_id=raw.get("default_tool_id") or None,
                tools=tools,
            )
        )
    return kits


async def list_kits(session: RdSession) -> list[Kit]:
    """Every kit this account can see on this surface, in display order."""
    payload = await request_json_async(
        "GET",
        with_query(f"{api_base_url()}/kits", session.account_params()),
        headers=await session.auth_headers(),
    )
    return parse_kits(payload)
