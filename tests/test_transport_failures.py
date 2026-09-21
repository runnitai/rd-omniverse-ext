"""Every way a request can fail without an answer, as something the panel shows.

Caught in a real session. The Uploads tab sat on "Loading..." and stayed there,
and the only trace was in the Kit console:

    [Error] [asyncio] Task exception was never retrieved
      page = await assets_api.list_uploads(
      payload = await request_json_async(
      return await loop.run_in_executor(
    TimeoutError: The read operation timed out

A timeout during the READ raises the builtin `TimeoutError`. It is an `OSError`
but NOT a `urllib.error.URLError`, so the handler written for "could not reach
RunDiffusion" did not catch it, it escaped the `except ApiError` in every
caller, and it killed the task mid-load. Nothing in the panel knew, so nothing
in the panel said.

The rule these pin: a caller of this module sees `ApiError` or a result, never
a raw socket failure.
"""

from __future__ import annotations

import asyncio
import socket
import urllib.error

import pytest

from rundiffusion_omniverse.api import transport
from rundiffusion_omniverse.api.transport import ApiError, request_json


class FakeResponse:
    def __init__(self, raw: bytes = b"{}") -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


def _raising(error: BaseException):
    def _open(*_args, **_kwargs):
        raise error

    return _open


class TestReachingTheServer:
    def test_a_read_timeout_becomes_an_ApiError(self, monkeypatch):
        """THE one. `socket.timeout` is `TimeoutError` on this Python, and it
        arrives bare rather than wrapped in a URLError."""
        monkeypatch.setattr(
            transport.urllib.request,
            "urlopen",
            _raising(socket.timeout("The read operation timed out")),
        )

        with pytest.raises(ApiError) as caught:
            request_json("GET", "https://api/library")

        assert caught.value.code == "TIMEOUT"
        assert caught.value.status == 0

    def test_a_connect_timeout_is_the_same_failure_to_the_person_waiting(
        self, monkeypatch
    ):
        """This one DOES arrive wrapped, which is why the wrapper is unpacked
        rather than the outer type trusted."""
        monkeypatch.setattr(
            transport.urllib.request,
            "urlopen",
            _raising(urllib.error.URLError(TimeoutError("timed out"))),
        )

        with pytest.raises(ApiError) as caught:
            request_json("GET", "https://api/library")

        assert caught.value.code == "TIMEOUT"

    def test_the_timeout_message_says_what_to_do(self, monkeypatch):
        """It goes straight into the panel, under a grid the user is waiting
        on. "TimeoutError" is not an instruction."""
        monkeypatch.setattr(
            transport.urllib.request, "urlopen", _raising(socket.timeout())
        )

        with pytest.raises(ApiError) as caught:
            request_json("GET", "https://api/library")

        assert "Try again" in caught.value.message

    def test_no_network_still_reads_as_unreachable(self, monkeypatch):
        monkeypatch.setattr(
            transport.urllib.request,
            "urlopen",
            _raising(urllib.error.URLError("getaddrinfo failed")),
        )

        with pytest.raises(ApiError) as caught:
            request_json("GET", "https://api/library")

        assert caught.value.code == "UNREACHABLE"
        assert "Could not reach RunDiffusion" in caught.value.message

    def test_a_connection_reset_is_not_left_raw(self, monkeypatch):
        """A bare OSError that is neither a URLError nor a timeout. The point
        of catching OSError is that this cannot be forgotten again."""
        monkeypatch.setattr(
            transport.urllib.request,
            "urlopen",
            _raising(ConnectionResetError("reset by peer")),
        )

        with pytest.raises(ApiError) as caught:
            request_json("GET", "https://api/library")

        assert caught.value.code == "UNREACHABLE"

    def test_an_http_error_still_takes_the_server_at_its_word(self, monkeypatch):
        """A status the server DID send must keep its own code, not be
        flattened into a transport failure."""
        error = urllib.error.HTTPError(
            "https://api/library", 429, "Too Many Requests", {}, None
        )
        error.read = lambda: b'{"error": {"code": "RATE_LIMITED", "message": "Slow down."}}'
        monkeypatch.setattr(transport.urllib.request, "urlopen", _raising(error))

        with pytest.raises(ApiError) as caught:
            request_json("GET", "https://api/library")

        assert caught.value.status == 429
        assert caught.value.code == "RATE_LIMITED"

    def test_a_good_answer_is_still_returned(self, monkeypatch):
        monkeypatch.setattr(
            transport.urllib.request,
            "urlopen",
            lambda *_a, **_k: FakeResponse(b'{"data": []}'),
        )

        assert request_json("GET", "https://api/library") == {"data": []}


class TestTheCallersCope:
    """The listing calls catch `ApiError`. With the conversion above that is
    now the only thing they can be handed, which is what turns a dead task
    back into a message under the grid."""

    def test_a_timeout_reaches_the_panel_as_a_message(self, monkeypatch):
        from rundiffusion_omniverse.api import assets as assets_api
        from rundiffusion_omniverse.api.session import Account, RdSession

        monkeypatch.setattr(
            transport.urllib.request, "urlopen", _raising(socket.timeout())
        )
        session = RdSession(id_token="t", refresh_token="r", expires_at=1 << 40)
        session.accounts = [Account(id="personal", label="P", kind="PERSONAL")]

        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(ApiError) as caught:
                loop.run_until_complete(assets_api.list_uploads(session))
        finally:
            loop.close()

        assert caught.value.code == "TIMEOUT"
