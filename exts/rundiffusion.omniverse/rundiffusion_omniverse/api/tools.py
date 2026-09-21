"""Tool discovery and the mapping from a tool's schema to a request body.

Two jobs. Listing tools for the picker, and working out which of a tool's fields
the prompt goes in and which one the viewport capture goes in.

The second job is the one with teeth. v2 keys `inputs` by **fieldKey**, taken
from the tool detail's `fields[].fieldKey`, and NOT by slug, label, or the
`uuid` that sits next to it looking equally usable. Keying by `uuid` fails
silently: it is deliberately excluded from `tool_fields_hash` because it is not
stable, so a request built from it is accepted and then matches nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .constants import api_base_url
from .transport import request_json_async, with_query
from .session import RdSession

logger = logging.getLogger(__name__)

# Field types, spelled exactly as the API spells them. An earlier draft guessed
# this vocabulary and got three of them wrong: it had TEXTAREA for TEXT_AREA,
# invented STRING, and knew nothing about NEG_PROMPT or BOOLEAN. A type that is
# not recognised falls through to a text box, so every one of those mistakes
# rendered SOMETHING and looked fine.

#: Prompt-style fields. NEG_PROMPT is deliberately NOT here: it is a prompt in
#: shape only, and writing the user's words into it inverts their intent.
PROMPT_TYPES = frozenset({"PROMPT"})

#: Everything that takes free text.
TEXT_TYPES = frozenset({"TEXT", "TEXT_AREA", "PROMPT", "NEG_PROMPT"})

#: Numeric fields rendered as a slider, and the ones rendered as a plain number.
SLIDER_TYPES = frozenset({"SLIDER", "UPSCALE_FACTOR_SLIDER", "CFG", "STEPS"})
NUMBER_TYPES = frozenset({"NUM"})

#: Choice fields.
SINGLE_SELECT_TYPES = frozenset({"SINGLE_SELECT", "MODEL_SINGLE_SELECT"})
MULTI_SELECT_TYPES = frozenset({"MULTI_SELECT"})

#: Field types that take an image. IMGS is the plural form and takes a LIST of
#: descriptors rather than one, which is a distinction that has shipped as a
#: bug before.
SINGLE_IMAGE_TYPES = frozenset({"IMG"})
MULTI_IMAGE_TYPES = frozenset({"IMGS"})
IMAGE_TYPES = SINGLE_IMAGE_TYPES | MULTI_IMAGE_TYPES

#: Presentation only. They carry no value and are never sent.
DISPLAY_ONLY_TYPES = frozenset({"HEADER", "HTML"})

#: Fields whose presence means this tool cannot be run from here yet.
#: MODEL_SINGLE_SELECT is runnable (it is a select); the MODEL *type* is what v2
#: refuses, and VIDEO/ASSET_3D are inputs this plugin cannot produce.
UNSUPPORTED_REQUIRED_TYPES = frozenset({"MODEL", "VIDEO", "ASSET_3D"})


@dataclass
class ToolSummary:
    id: str
    name: str
    #: Public URL for the tool's avatar. Not signed and not account-scoped, so
    #: it can be fetched without auth headers and cached like any other image.
    avatar_url: str | None = None
    description: str | None = None

    def __str__(self) -> str:  # what the picker shows
        return self.name or self.id


@dataclass
class ToolField:
    key: str
    type: str
    label: str | None
    required: bool
    #: The tool's own default. Sending it is how a required field this panel has
    #: no control for still gets a usable value.
    default_value: Any = None
    #: The field's `display` block: options, min, max, step. Needed to render a
    #: control that matches what the tool actually declares.
    display: dict = field(default_factory=dict)


@dataclass
class ToolDetail:
    id: str
    name: str
    tool_fields_hash: str
    fields: list[ToolField] = field(default_factory=list)
    #: The field tree as published, groups and all. The flat `fields` list is
    #: what requests are built from; this is what the form renders, because
    #: grouping and order are part of how a tool asks to be filled in.
    tree: list = field(default_factory=list)

    @property
    def prompt_field(self) -> ToolField | None:
        """The field the prompt text goes in.

        Required fields win over optional ones: if a tool has both a prompt and
        an optional negative prompt, writing the user's words into the negative
        one would invert their intent, which is worse than not running.
        """
        candidates = [f for f in self.fields if f.type.upper() in PROMPT_TYPES]
        if not candidates:
            return None
        return next((f for f in candidates if f.required), candidates[0])

    @property
    def image_field(self) -> ToolField | None:
        """The field the viewport capture goes in."""
        candidates = [f for f in self.fields if f.type.upper() in IMAGE_TYPES]
        if not candidates:
            return None
        return next((f for f in candidates if f.required), candidates[0])

    @property
    def unsupported_required_field(self) -> ToolField | None:
        """A required field this plugin cannot supply, so the run would fail."""
        return next(
            (
                f
                for f in self.fields
                if f.required and f.type.upper() in UNSUPPORTED_REQUIRED_TYPES
            ),
            None,
        )


def _walk_fields(node: Any, out: list[ToolField]) -> None:
    """Collect the leaves of the field tree that are caller-supplied inputs.

    The tree is nested groups, and only nodes marked `isField` are inputs. The
    projection renames `__rfield` to `isField` and `fieldDefUuid` to `fieldKey`,
    so those are the names to read.
    """
    if isinstance(node, list):
        for item in node:
            _walk_fields(item, out)
        return

    if not isinstance(node, dict):
        return

    if node.get("isField") and node.get("fieldKey"):
        out.append(
            ToolField(
                key=node["fieldKey"],
                type=str(node.get("type") or ""),
                label=node.get("label"),
                required=bool(node.get("required")),
                default_value=node.get("defaultValue"),
                display=node.get("display") or {},
            )
        )

    for value in node.values():
        if isinstance(value, (list, dict)):
            _walk_fields(value, out)


@dataclass
class ToolPage:
    """One page of the catalogue, plus how to ask for the next.

    The same shape `AssetPage` uses, for the same reason: `/tools` is cursor
    paged and serves up to 100 per request, so a caller that takes the first
    response and stops is showing a prefix of the catalogue while calling it
    all of them. `next_cursor` is opaque and goes back untouched.
    """

    items: list[ToolSummary]
    next_cursor: str | None = None

    @property
    def has_more(self) -> bool:
        return bool(self.next_cursor)


#: What one request asks for. The server's own ceiling, because the catalogue
#: is drained in full and fewer round trips is the only thing to optimise for.
PAGE_LIMIT = 100

#: A misbehaving cursor that never clears would otherwise loop forever. Fifty
#: full pages is far more than the catalogue will plausibly ever need.
MAX_PAGES = 50


async def list_all_tools(session: RdSession, on_page=None) -> list[ToolSummary]:
    """Every tool this account can run, by draining the cursor.

    The picker searches the full set rather than a page of it: there is no
    server-side text search on `/tools`, so a search box can only be honest if
    the client is holding everything.

    `on_page` is handed the accumulated list after each page so a caller can
    render progressively instead of waiting for the last one.
    """
    tools: list[ToolSummary] = []
    seen: set[str] = set()
    cursor: str | None = None
    pages = 0
    for _ in range(MAX_PAGES):
        page = await list_tools(session, PAGE_LIMIT, cursor=cursor)
        pages += 1
        # Deduped on the way in: a cursor that overlaps its predecessor would
        # otherwise put the same tool in the list twice, and the selection
        # lookup keys on id.
        for tool in page.items:
            if tool.id not in seen:
                seen.add(tool.id)
                tools.append(tool)
        if on_page is not None:
            on_page(list(tools))
        if not page.next_cursor:
            logger.info(
                "RunDiffusion: loaded %s tools in %s page(s).", len(tools), pages
            )
            break
        cursor = page.next_cursor
    else:
        logger.warning(
            "RunDiffusion: stopped draining tools at %s pages. The cursor never "
            "cleared, so the catalogue may be incomplete.",
            MAX_PAGES,
        )
    return tools


async def list_tools(
    session: RdSession, limit: int = 50, *, cursor: str | None = None
) -> ToolPage:
    payload = await request_json_async(
        "GET",
        with_query(
            f"{api_base_url()}/tools",
            {"limit": limit, "cursor": cursor, **session.account_params()},
        ),
        headers=await session.auth_headers(),
    )
    payload = payload or {}
    return ToolPage(
        items=[
            ToolSummary(
                id=item["id"],
                name=item.get("name") or item["id"],
                avatar_url=item.get("avatar_url"),
                description=item.get("description"),
            )
            for item in payload.get("data") or []
        ],
        # v2 drops `last_cursor` (it only echoed what the caller sent, and
        # reading it as the next page loops forever). `next_cursor` is the one
        # to follow, and it is absent on the final page.
        next_cursor=payload.get("next_cursor"),
    )


async def get_tool(session: RdSession, tool_id: str) -> ToolDetail:
    payload = await request_json_async(
        "GET",
        with_query(f"{api_base_url()}/tools/{tool_id}", session.account_params()),
        headers=await session.auth_headers(),
    )
    fields: list[ToolField] = []
    _walk_fields(payload.get("fields") or [], fields)
    return ToolDetail(
        id=payload["id"],
        name=payload.get("name") or payload["id"],
        # Echoed back unchanged on generate. A stale one answers 409
        # TOOL_SCHEMA_STALE with the current hash in details.
        tool_fields_hash=payload["tool_fields_hash"],
        fields=fields,
        tree=payload.get("fields") or [],
    )


@dataclass(frozen=True)
class ImageSelection:
    """One image chosen for one image field.

    Exactly one of two things. Either `reference` names something the server
    already holds (an UPLOAD_REF or a LIBRARY_REF), or `png` carries the bytes of
    a viewport capture, which have to travel with the request because the server
    has never seen them.

    The bytes live HERE rather than being captured at submit time, and that is
    the difference that lets a field hold several different views: a capture is
    taken when the user asks for it and pinned, so moving the camera and
    capturing again gives a second image rather than redefining the first.
    """

    reference: dict | None = None
    png: bytes | None = None
    #: What to say about it in the panel. Never sent.
    description: str = ""

    @property
    def is_capture(self) -> bool:
        return self.reference is None


def image_field_keys(tool: ToolDetail) -> list[str]:
    """The tool's image fields, in the order the schema declares them.

    This order IS the file_index order: `build_inputs` walks it to number the
    capture descriptors and the caller stages its parts to match.
    """
    return [f.key for f in tool.fields if f.type.upper() in IMAGE_TYPES]


def ordered_captures(
    tool: ToolDetail, image_selections: dict[str, list[ImageSelection]] | None
) -> list[bytes]:
    """The captured images, in the order `build_inputs` numbers them.

    The nth item here is what `file_index` n refers to, so this is the list the
    caller stages as file parts. It walks `image_field_keys` rather than
    re-deriving an order, which is the point: there is ONE ordering rule and
    both halves of the contract read it.

    A capture with no bytes is skipped rather than staged, which keeps the
    numbering honest on the preview-cost path where nothing has been captured.
    """
    return [
        entry.png
        for key in image_field_keys(tool)
        for entry in (image_selections or {}).get(key) or []
        if entry.is_capture and entry.png is not None
    ]


def build_inputs(
    tool: ToolDetail,
    values: dict[str, Any],
    has_capture: bool = True,
    image_selections: dict[str, list[ImageSelection]] | None = None,
) -> dict[str, Any]:
    """Assemble the `inputs` map for a run.

    `values` is what the form collected, already keyed by fieldKey. Three things
    are added on top of it, in this order:

    1. the images, because a capture's bytes do not exist until submit time and
       so cannot come from a control;
    2. the tool's own defaultValue for any REQUIRED field still unfilled;
    3. nothing else. Optional fields left blank stay blank, so the tool applies
       its own behaviour rather than being handed this plugin's idea of it.

    `image_selections` is what the user put in each image field, in order: a
    plural field can hold several images, mixing captured views with library and
    upload references. Each capture carries its own bytes, so two of them are two
    different views rather than one view sent twice.

    `has_capture` is False on the preview-cost path, which prices the run before
    anything is uploaded: captures are dropped there and references still count.

    Before there was a form, this function guessed which field the prompt went
    in. It no longer guesses: the user fills the schema the tool published.
    """
    # A blank is the absence of a value, so the key is dropped rather than sent
    # empty. Sending it is not the same thing to a provider: one video tool's
    # provider answered a run with `style: ""` with
    #
    #   Input should be 'anime', '3d_animation', 'clay', 'comic' or 'cyberpunk'
    #
    # because an optional literal accepts one of its values or nothing, and ""
    # is neither. The run failed after it was submitted rather than being
    # refused up front, which is the worst place for a mistake this avoidable
    # to surface. Omitting the key is what "leave it blank" has to mean.
    #
    # Required fields are pruned too: one left empty then falls to the default
    # pass below, and failing that is named by `missing_required_fields`, which
    # refuses locally instead of buying a downstream failure.
    inputs: dict[str, Any] = {
        key: value
        for key, value in values.items()
        if value is not None and value != ""
    }

    if image_selections is None:
        image_selections = {}

    plural_keys = {f.key for f in tool.fields if f.type.upper() in MULTI_IMAGE_TYPES}

    # POSITION IS THE CONTRACT. Walking the schema order, every capture takes the
    # next file_index, and the caller stages that many parts in the same walk. A
    # single part shared between two slots would be smaller on the wire and is
    # not done: every bug in this plugin so far has been a shape the server
    # accepted and then mishandled downstream, and one part per image is the
    # shape with no second reading.
    file_index = 0
    for key in image_field_keys(tool):
        descriptors: list[dict] = []
        for entry in image_selections.get(key) or []:
            if entry.is_capture:
                # A capture with no bytes has no part to point at, so it is
                # skipped rather than being numbered into a gap.
                if not has_capture or entry.png is None:
                    continue
                descriptors.append({"kind": "MULTIPART", "file_index": file_index})
                file_index += 1
            else:
                descriptors.append(entry.reference)

        if not descriptors:
            continue
        # A plural field takes a LIST. A bare descriptor or reference where a
        # list belongs is accepted and then mishandled downstream rather than
        # rejected, which is exactly how it goes unnoticed.
        inputs[key] = descriptors if key in plural_keys else descriptors[0]

    # Every REQUIRED field still without a value gets the tool's own default.
    #
    # The form covers what it can render, but a tool can require a type this
    # plugin has no control for. A request sent without such a field fails only
    # after it is submitted, and that has happened on more than one tool.
    #
    # defaultValue rather than a guessed literal: a composite takes an object
    # whose keys are the tool's private sub-mappings, so inventing one would be
    # guessing a shape again.
    for tool_field in tool.fields:
        if not tool_field.required or tool_field.key in inputs:
            continue
        if tool_field.default_value is not None:
            inputs[tool_field.key] = tool_field.default_value

    return inputs


def missing_required_fields(tool: ToolDetail, inputs: dict[str, Any]) -> list[ToolField]:
    """Required fields left unfilled, which would make the run fail downstream.

    Better to refuse and name them than to submit a request the server accepts
    and then bills for.
    """
    return [f for f in tool.fields if f.required and f.key not in inputs]
