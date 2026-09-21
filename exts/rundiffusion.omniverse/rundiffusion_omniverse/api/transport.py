"""Minimal JSON-over-HTTP helper.

Uses `urllib.request` from the standard library rather than `requests`. Kit
prebundles a lot, but every third-party import is one more thing that has to be
present in a customer's app, and this client needs nothing `urllib` cannot do.

Every call is blocking, so the async wrappers below hand the work to a thread
rather than stalling Kit's update loop. A frozen viewport is the most visible
possible bug in a DCC plugin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30


class ApiError(RuntimeError):
    """A structured failure from the API.

    The server answers errors in a stable envelope, `{"error": {"code",
    "message", "retryable", "details"}}`, so the code is carried separately from
    the message: callers branch on `code`, and only `message` is ever shown to a
    person.
    """

    def __init__(
        self,
        status: int,
        code: str | None = None,
        message: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message or f"HTTP {status}")
        self.status = status
        self.code = code
        self.message = message or f"HTTP {status}"
        self.details = details or {}

    @property
    def is_authorization_pending(self) -> bool:
        """428 while the user has not finished authorizing in the browser."""
        return self.status == 428 or self.code == "AUTHORIZATION_PENDING"

    @property
    def is_slow_down(self) -> bool:
        """The poll interval was too aggressive. Double it, per the server."""
        return self.code == "SLOW_DOWN"

    @property
    def is_device_code_expired(self) -> bool:
        return self.status == 410 or self.code == "DEVICE_CODE_EXPIRED"


def _parse_error(status: int, body: bytes) -> ApiError:
    try:
        payload = json.loads(body.decode("utf-8"))
        error = payload.get("error") or {}
        return ApiError(
            status=status,
            code=error.get("code"),
            message=error.get("message"),
            details=error.get("details"),
        )
    except Exception:  # noqa: BLE001 - a non-JSON body is still a failure worth reporting
        return ApiError(status=status, message=f"HTTP {status}")


def _unreachable(error: OSError) -> ApiError:
    """An `OSError` from urllib, as a failure the panel can show.

    Status 0, because the request never got an answer and there is no code to
    branch on.

    The TIMEOUT case is split out because it is the one that escaped. A timeout
    during the READ raises the builtin `TimeoutError`, which is an `OSError`
    but NOT a `urllib.error.URLError`, so an `except URLError` handler lets it
    straight through. It then escaped `request_json`, escaped the `except
    ApiError` in every caller, killed the task loading the page, and left the
    grid saying "Loading..." with nothing on the way and nothing to say why.
    The only trace was asyncio's own "Task exception was never retrieved" in
    the Kit console, which nobody browsing a library is looking at.

    Caught as `OSError` rather than as the pair, because `URLError` is an
    `OSError` too and one handler cannot then be got wrong again.
    """
    # A timeout while CONNECTING arrives wrapped in a URLError; one while
    # reading arrives bare. Both are the same thing to the person waiting.
    reason = getattr(error, "reason", None)
    if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError):
        return ApiError(
            status=0,
            code="TIMEOUT",
            message=(
                f"RunDiffusion took more than {REQUEST_TIMEOUT_SECONDS} seconds "
                "to answer. Try again."
            ),
        )
    return ApiError(
        status=0,
        code="UNREACHABLE",
        message=f"Could not reach RunDiffusion: {reason or error}",
    )


def request_json(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """Blocking JSON request. Returns the decoded body, or None for a 204."""
    data = None
    request_headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})

    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise _parse_error(error.code, error.read()) from None
    except OSError as error:
        # No network, DNS failure, TLS refusal, or a read that timed out.
        # Distinct from an API error: the request never got an answer, so there
        # is no code to branch on. See `_unreachable` for why this is OSError
        # and not URLError.
        raise _unreachable(error) from None


async def request_json_async(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """`request_json` on a worker thread, so Kit keeps rendering."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: request_json(method, url, body=body, headers=headers)
    )


def _multipart_body(
    fields: dict[str, str], files: list[tuple[str, str, bytes, str]], boundary: str
) -> bytes:
    """Encode a multipart/form-data body.

    Hand-rolled because `urllib` has no multipart encoder and pulling in
    `requests` for one POST is not worth the dependency. The shape is fixed by
    the server: one `payload` part carrying the JSON body, then one `files` part
    per image, in the order the body's `file_index` values refer to.

    Each file part carries its REAL content type. The server validates it
    against an allow-list and answers 415 for anything else, so the generic
    `application/octet-stream` an earlier draft sent was rejected outright.
    """
    line = f"--{boundary}".encode()
    parts: list[bytes] = []

    for name, value in fields.items():
        parts += [
            line,
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            value.encode("utf-8"),
        ]

    for name, filename, content, content_type in files:
        parts += [
            line,
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode(),
            f"Content-Type: {content_type}".encode(),
            b"",
            content,
        ]

    parts += [f"--{boundary}--".encode(), b""]
    return b"\r\n".join(parts)


async def post_multipart_async(
    url: str,
    *,
    fields: dict[str, str],
    files: list[tuple[str, str, bytes, str]],
    headers: dict[str, str] | None = None,
) -> Any:
    """Multipart POST on a worker thread.

    Each file is `(part_name, filename, content, content_type)`.
    """
    boundary = f"----RunDiffusion{uuid.uuid4().hex}"
    body = _multipart_body(fields, files, boundary)
    request_headers = {
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    request_headers.update(headers or {})

    def send() -> Any:
        request = urllib.request.Request(
            url, data=body, headers=request_headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as error:
            raise _parse_error(error.code, error.read()) from None
        except OSError as error:
            raise _unreachable(error) from None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, send)


async def download_async(url: str) -> bytes:
    """Fetch a result image. The URL is pre-signed, so it carries no auth."""

    def fetch() -> bytes:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch)


def with_query(url: str, params: dict[str, Any]) -> str:
    """Append non-empty query parameters to a URL."""
    filtered = {key: value for key, value in params.items() if value not in (None, "")}
    if not filtered:
        return url
    return f"{url}?{urllib.parse.urlencode(filtered)}"
