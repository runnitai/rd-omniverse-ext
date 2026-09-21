"""Submit a run, poll it, and hand back the finished images.

The multipart shape is fixed by the server: one `payload` part carrying the JSON
body as a string, then one `files` part per image. A file part on its own does
nothing; the body has to point at it by index from `inputs`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import media
from .constants import DEFAULT_OUTPUT_FILE_NAME, GENERATE_ATTRIBUTION, api_base_url
from .transport import (
    ApiError,
    download_async,
    post_multipart_async,
    request_json_async,
    with_query,
)
from .session import RdSession
from .tools import ImageSelection, ToolDetail, build_inputs, ordered_captures

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})

#: The viewport capture is encoded to PNG, and the server's allow-list for
#: multipart parts is image/jpeg, png, webp, heic, heif (plus video types).
CAPTURE_MIME_TYPE = "image/png"

#: Give up rather than poll forever. Generous, because a queued run behind a
#: busy GPU is slow rather than broken.
POLL_TIMEOUT_SECONDS = 600


#: What the panel can do something with. Re-exported from `media` rather than
#: declared here: a run's output and a library item are the same question asked
#: of two payloads, and answering it twice is how the library came to treat a
#: video as a picture while a run did not.
KIND_IMAGE = media.KIND_IMAGE
KIND_VIDEO = media.KIND_VIDEO
KIND_ASSET_3D = media.KIND_ASSET_3D
KIND_OTHER = media.KIND_OTHER


@dataclass(frozen=True)
class FetchedOutput:
    """One output, downloaded, with its still if it had one.

    A tuple was enough while an output was only ever its own bytes. The still
    is a second, optional download that belongs to the same result, and
    threading it through as a third tuple element is the kind of thing that is
    read wrong once and then silently written to the wrong file.
    """

    output: "Output"
    data: bytes
    #: The first-frame JPEG, when the server had one and it downloaded.
    preview: bytes | None = None


@dataclass(frozen=True)
class Output:
    """One file a finished run produced.

    The type is the server's, not a guess from the URL: an output carries
    `type` and `mime_type` and the plugin ignored both, treating everything as
    an image and writing whatever arrived to a `.png`. A video tool is
    selectable today, so that was reachable.
    """

    url: str | None = None
    #: A signed still for an output this plugin cannot draw itself. Video only
    #: today, and None whenever the worker could not make one, which the server
    #: is explicit about: a caller branches on null and uses its own fallback
    #: rather than being handed a URL that 404s.
    #:
    #: Not the same thing as a library item's `thumb_url`, which falls back to
    #: the file itself. This one is either a still or nothing.
    preview_url: str | None = None
    type: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    size_bytes: int | None = None
    expired: bool = False

    @property
    def kind(self) -> str:
        """What this plugin would have to draw to show it."""
        return media.kind_for(self.type, self.mime_type)

    @property
    def is_fetchable(self) -> bool:
        return bool(self.url) and not self.expired

    @property
    def has_preview(self) -> bool:
        """Whether there is a still to draw instead of a tile of words.

        Expiry counts. The preview is signed on the same TTL as the output it
        belongs to and shares the bucket's lifecycle, so an expired output's
        preview is expired too.
        """
        return bool(self.preview_url) and not self.expired

    @property
    def extension(self) -> str:
        """The suffix this file should be written with, including the dot."""
        return media.extension_for(self.mime_type, self.url)


def parse_outputs(raw: Any) -> list[Output]:
    """Read the `outputs` array. Anything unreadable is dropped, not guessed."""
    if not isinstance(raw, list):
        return []
    parsed = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        parsed.append(
            Output(
                url=item.get("url"),
                preview_url=item.get("preview_url"),
                type=item.get("type"),
                mime_type=item.get("mime_type"),
                width=item.get("width"),
                height=item.get("height"),
                duration_seconds=item.get("duration_seconds"),
                size_bytes=item.get("size_bytes"),
                expired=bool(item.get("expired")),
            )
        )
    return parsed


@dataclass
class GenerateResult:
    request_id: str
    status: str
    outputs: list[Output] = field(default_factory=list)
    error_message: str | None = None
    tokens_charged: int | None = None
    #: The whole terminal status body. Kept because a failed run does not always
    #: carry an `error`: the first Omniverse generate came back `failed` with
    #: nothing else, and there was no record of what the server had actually
    #: been sent or had answered.
    raw: dict = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


async def preview_cost(
    session: RdSession,
    tool: ToolDetail,
    values: dict,
    num_results: int = 1,
    image_selections: dict[str, list[ImageSelection]] | None = None,
) -> dict:
    """The authoritative token cost, before the user spends anything.

    Branch on `can_run`, never on `blocking_reason`: the reason is a display
    string with no stable vocabulary.
    """
    body = {
        "tool_id": tool.id,
        "tool_fields_hash": tool.tool_fields_hash,
        "inputs": build_inputs(
            tool, values, has_capture=False, image_selections=image_selections
        ),
        "num_results": num_results,
    }
    return await request_json_async(
        "POST",
        with_query(f"{api_base_url()}/generate/preview-cost", session.account_params()),
        body=body,
        headers=await session.auth_headers(),
    )


def capture_file_parts(pngs: list[bytes]) -> list[tuple[str, str, bytes, str]]:
    """One file part per captured image, in the order they were numbered.

    This is the other half of the contract `build_inputs` writes: it numbers the
    captures 0, 1, 2 walking the schema, and `ordered_captures` collects their
    bytes on the same walk, so the nth part here IS the image the nth descriptor
    means. Neither side re-derives an order the other could disagree with.

    image/png rather than application/octet-stream: the server allow-lists the
    part's content type and answers 415 otherwise. A capture is always PNG, so
    this is a constant rather than a guess.
    """
    return [
        ("files", f"omniverse-capture-{index}.png", png, CAPTURE_MIME_TYPE)
        for index, png in enumerate(pngs)
    ]


async def submit(
    session: RdSession,
    tool: ToolDetail,
    values: dict,
    num_results: int = 1,
    image_selections: dict[str, list[ImageSelection]] | None = None,
) -> str:
    """POST the run. Returns the request id from the 202.

    `image_selections` is what the user put in each image field, in order, and
    each capture carries its own bytes. Nothing is captured here: the images were
    taken when the user asked for them, so what goes up is exactly what the panel
    was showing.
    """
    captures = ordered_captures(tool, image_selections)
    inputs = build_inputs(
        tool,
        values,
        has_capture=True,
        image_selections=image_selections,
    )
    body = {
        "tool_id": tool.id,
        "tool_fields_hash": tool.tool_fields_hash,
        "inputs": inputs,
        "num_results": num_results,
        # Attribution. Without it the run is indistinguishable from generic API
        # traffic. `omniverse` had to be a registered plugin kind for this to be
        # accepted at all.
        "plugin": GENERATE_ATTRIBUTION,
    }

    headers = await session.auth_headers()
    # Replays the original 202 rather than starting a second run if the response
    # is lost in transit. Cheap insurance against double-billing a user for one
    # click.
    headers["Idempotency-Key"] = str(uuid.uuid4())

    # Logged before sending, so a run that dies server-side with an empty error
    # still leaves a record of exactly what was submitted. Without this the only
    # evidence of the failure is the word "failed".
    logger.info(
        "RunDiffusion: submitting to %s with inputs %s",
        tool.id,
        json.dumps(body.get("inputs"), default=str)[:2000],
    )

    payload = await post_multipart_async(
        with_query(f"{api_base_url()}/generate", session.account_params()),
        fields={"payload": json.dumps(body)},
        files=capture_file_parts(captures),
        headers=headers,
    )
    return payload["request_id"]


async def poll_until_done(
    session: RdSession,
    request_id: str,
    on_status: Callable[[str], None] | None = None,
) -> GenerateResult:
    """Poll until the run reaches a terminal state.

    The server tells us how long to wait between polls via `retry_after_seconds`,
    so honour it rather than picking an interval: it is what keeps a slow queue
    from being hammered by every connected plugin at once.
    """
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT_SECONDS
    delay = 2

    while True:
        payload: dict[str, Any] = await request_json_async(
            "GET",
            with_query(f"{api_base_url()}/generate/{request_id}", session.account_params()),
            headers=await session.auth_headers(),
        )
        status = str(payload.get("status") or "")

        if status in TERMINAL_STATES:
            error = payload.get("error") or {}
            if status != "succeeded":
                logger.error(
                    "RunDiffusion: run %s ended %s. Full status body: %s",
                    request_id,
                    status,
                    json.dumps(payload, default=str)[:4000],
                )
            return GenerateResult(
                request_id=request_id,
                status=status,
                outputs=parse_outputs(payload.get("outputs")),
                error_message=error.get("message"),
                tokens_charged=payload.get("tokens_charged"),
                raw=payload,
            )

        if on_status is not None:
            on_status(status or "pending")

        if asyncio.get_event_loop().time() > deadline:
            raise ApiError(
                status=0,
                message=f"The run did not finish within {POLL_TIMEOUT_SECONDS // 60} minutes.",
            )

        delay = int(payload.get("retry_after_seconds") or delay)
        await asyncio.sleep(max(1, delay))


async def download_outputs(
    result: GenerateResult, kinds: tuple[str, ...] = (KIND_IMAGE,)
) -> list[tuple[Output, bytes]]:
    """Fetch the finished files, paired with what each one is.

    `kinds` is what the caller can actually do something with. Defaulting to
    images keeps the panel's behaviour while video and 3D are unbuilt:
    before this, every output was downloaded
    and written to a `.png` whatever it was, and a video tool is selectable, so
    a run could produce a file named for a format it was not.

    Expired results are skipped rather than fatal.
    """
    fetched: list[FetchedOutput] = []
    for output in result.outputs:
        if output.kind not in kinds:
            logger.info(
                "RunDiffusion: skipping a %s output this panel cannot show yet.",
                output.kind,
            )
            continue
        if not output.is_fetchable:
            continue
        try:
            data = await download_async(output.url)
        except Exception:  # noqa: BLE001 - one bad URL should not lose the rest
            logger.exception("RunDiffusion: could not download a result.")
            continue
        fetched.append(FetchedOutput(output, data, await _preview_bytes(output)))
    return fetched


async def _preview_bytes(output: Output) -> bytes | None:
    """The still for one output, or None. Never raises.

    A preview that will not download costs the picture on a tile and nothing
    else: the render itself is already in hand by the time this runs, and the
    tile has a fallback that predates previews existing at all. Losing the run
    over the thumbnail would be the wrong trade by a wide margin.
    """
    if not output.has_preview:
        return None
    try:
        return await download_async(output.preview_url)
    except Exception:  # noqa: BLE001 - a missing still is not a missing render
        logger.info("RunDiffusion: could not download a preview still.")
        return None
