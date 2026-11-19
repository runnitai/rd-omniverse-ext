"""One refresh of the sign-in token, however many requests find it expired.

Every authenticated request checks the id token first, and refreshes it when
it is about to run out. Whatever the panel has going at that moment (an
estimate, a page of the library, the poll of a run) finds it expired at the
same time, and each of them exchanging the refresh token for itself is several
identical exchanges where one does the job. So the first refreshes, and the
rest wait for it and use what it brought back.
"""

from __future__ import annotations

import asyncio
import time

from rundiffusion_omniverse.api import session as session_module
from rundiffusion_omniverse.api.session import RdSession
from rundiffusion_omniverse.api.transport import ApiError


def _session(expires_in: int) -> RdSession:
    session = RdSession()
    session.id_token = "old"
    session.refresh_token = "refresh-1"
    session.expires_at = int(time.time()) + expires_in
    session._persist = lambda: None
    return session


class TokenService:
    """POST /auth/token/refresh, answering when the test says so."""

    def __init__(self, failures: int = 0) -> None:
        self.calls: list = []
        self.failures = failures
        self.release: asyncio.Event | None = None

    async def __call__(self, method, url, *, body=None, headers=None):
        assert (method, url.rsplit("/", 3)[-3:]) == ("POST", ["auth", "token", "refresh"])
        self.calls.append(body)
        if self.release is not None:
            await self.release.wait()
        if len(self.calls) <= self.failures:
            raise ApiError(0, "UNREACHABLE", "Could not reach RunDiffusion.")
        return {
            "id_token": f"new-{len(self.calls)}",
            "refresh_token": "refresh-2",
            "expires_at": int(time.time()) + 3600,
        }


def _together(session: RdSession, service: TokenService, how_many: int) -> list:
    """`how_many` requests asking for the token at once, as the panel's do."""

    async def scenario():
        service.release = asyncio.Event()
        asking = [
            asyncio.ensure_future(session.ensure_valid_token()) for _ in range(how_many)
        ]
        for _ in range(10):
            await asyncio.sleep(0)
        service.release.set()
        return await asyncio.gather(*asking, return_exceptions=True)

    return asyncio.run(scenario())


class TestAnExpiredToken:
    def test_requests_that_find_it_expired_together_share_one_refresh(self, monkeypatch):
        service = TokenService()
        monkeypatch.setattr(session_module, "request_json_async", service)
        session = _session(expires_in=-60)

        tokens = _together(session, service, 5)

        assert len(service.calls) == 1
        assert tokens == ["new-1"] * 5
        assert session.refresh_token == "refresh-2"

    def test_one_about_to_expire_is_refreshed_once_too(self, monkeypatch):
        """Inside the margin kept before expiry, so a long run does not start
        with a token that dies on the way."""
        service = TokenService()
        monkeypatch.setattr(session_module, "request_json_async", service)
        session = _session(expires_in=session_module.TOKEN_REFRESH_SKEW_SECONDS - 10)

        tokens = _together(session, service, 3)

        assert len(service.calls) == 1
        assert tokens == ["new-1"] * 3

    def test_a_failed_refresh_is_reported_and_the_next_in_line_tries_again(
        self, monkeypatch
    ):
        """The request that waited asks for itself once it is its turn, rather
        than inheriting a failure it never saw."""
        service = TokenService(failures=1)
        monkeypatch.setattr(session_module, "request_json_async", service)
        session = _session(expires_in=-60)

        first, second = _together(session, service, 2)

        assert isinstance(first, ApiError)
        assert second == "new-2"
        assert len(service.calls) == 2


class TestATokenWithTimeLeft:
    def test_is_used_as_it_is(self, monkeypatch):
        service = TokenService()
        monkeypatch.setattr(session_module, "request_json_async", service)
        session = _session(expires_in=3600)

        tokens = _together(session, service, 3)

        assert service.calls == []
        assert tokens == ["old"] * 3
