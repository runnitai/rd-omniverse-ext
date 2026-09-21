"""Reading the catalogue a page at a time.

`/tools` is cursor paged and serves at most 100 per request. The plugin used to
take the first response and stop, which put a heading reading ALL TOOLS above a
prefix of them. These cover the request that asks for the next page and the
shape that comes back.

`last_cursor` is deliberately not consulted: v2 drops it, and in v1 it only
echoed what the caller sent, so reading it as the next page loops forever.
"""

from __future__ import annotations

import asyncio

import pytest

from rundiffusion_omniverse.api import tools as tools_api


class FakeSession:
    """Only the two things `list_tools` asks a session for."""

    def account_params(self) -> dict:
        return {}

    async def auth_headers(self) -> dict:
        return {"X-Device-Id": "device-1"}


class Recorder:
    """Stands in for the transport and records the URL it was handed."""

    def __init__(self, payload) -> None:
        self.payload = payload
        self.urls: list[str] = []

    async def __call__(self, _method, url, **_kwargs):
        self.urls.append(url)
        return self.payload


def _item(tool_id: str) -> dict:
    return {
        "id": tool_id,
        "name": tool_id.title(),
        "description": "does a thing",
        "avatar_url": f"https://api.example/{tool_id}.png",
    }


def _call(monkeypatch, payload, **kwargs):
    recorder = Recorder(payload)
    monkeypatch.setattr(tools_api, "request_json_async", recorder)
    page = asyncio.new_event_loop().run_until_complete(
        tools_api.list_tools(FakeSession(), **kwargs)
    )
    return page, recorder


class TestTheRequest:
    def test_the_first_page_sends_no_cursor(self, monkeypatch):
        _, recorder = _call(monkeypatch, {"data": []})
        assert "cursor=" not in recorder.urls[0]

    def test_the_next_page_sends_the_one_it_was_given(self, monkeypatch):
        _, recorder = _call(monkeypatch, {"data": []}, cursor="cursor-2")
        assert "cursor=cursor-2" in recorder.urls[0]

    def test_the_limit_is_sent(self, monkeypatch):
        _, recorder = _call(monkeypatch, {"data": []}, limit=100)
        assert "limit=100" in recorder.urls[0]


class TestTheResponse:
    def test_tools_read_as_the_server_sent_them(self, monkeypatch):
        page, _ = _call(monkeypatch, {"data": [_item("alpha")]})
        [tool] = page.items
        assert (tool.id, tool.name) == ("alpha", "Alpha")
        assert tool.avatar_url == "https://api.example/alpha.png"

    def test_a_next_cursor_means_there_is_more(self, monkeypatch):
        page, _ = _call(monkeypatch, {"data": [_item("a")], "next_cursor": "cursor-2"})
        assert page.next_cursor == "cursor-2"
        assert page.has_more is True

    def test_the_last_page_has_no_cursor(self, monkeypatch):
        page, _ = _call(monkeypatch, {"data": [_item("a")], "next_cursor": None})
        assert page.has_more is False

    def test_an_absent_next_cursor_is_the_last_page(self, monkeypatch):
        page, _ = _call(monkeypatch, {"data": [_item("a")]})
        assert page.has_more is False

    def test_last_cursor_is_never_followed(self, monkeypatch):
        """It echoes the request, so following it pages the same page forever."""
        page, _ = _call(monkeypatch, {"data": [], "last_cursor": "cursor-1"})
        assert page.next_cursor is None

    @pytest.mark.parametrize("payload", [None, {}, {"data": None}])
    def test_an_empty_response_is_an_empty_page(self, monkeypatch, payload):
        page, _ = _call(monkeypatch, payload)
        assert page.items == []
        assert page.has_more is False


class TestDrainingTheCatalogue:
    """`list_all_tools` follows the cursor to the end.

    There is no server-side text search on `/tools`, so the browser's search box
    filters what the client holds. That makes "all of it" a correctness
    requirement rather than a nicety: a half-loaded catalogue turns the search
    box into something that silently fails to find tools that exist.
    """

    def _pages(self, monkeypatch, pages):
        """Serve `pages` in order, recording the cursor each call was given."""
        seen: list = []

        async def _fake(session, limit=100, *, cursor=None):
            seen.append(cursor)
            return pages[len(seen) - 1]

        monkeypatch.setattr(tools_api, "list_tools", _fake)
        return seen

    def _drain(self, on_page=None):
        return asyncio.new_event_loop().run_until_complete(
            tools_api.list_all_tools(FakeSession(), on_page=on_page)
        )

    def test_one_page_is_one_request(self, monkeypatch):
        seen = self._pages(monkeypatch, [tools_api.ToolPage([_summary("a")])])
        assert [t.id for t in self._drain()] == ["a"]
        assert seen == [None]

    def test_every_page_is_followed(self, monkeypatch):
        seen = self._pages(monkeypatch, [
            tools_api.ToolPage([_summary("a")], next_cursor="c2"),
            tools_api.ToolPage([_summary("b")], next_cursor="c3"),
            tools_api.ToolPage([_summary("c")]),
        ])
        assert [t.id for t in self._drain()] == ["a", "b", "c"]
        assert seen == [None, "c2", "c3"]

    def test_a_tool_repeated_across_pages_appears_once(self, monkeypatch):
        """An overlapping cursor must not duplicate cards, and must not make
        the id-keyed selection lookup ambiguous."""
        self._pages(monkeypatch, [
            tools_api.ToolPage([_summary("a"), _summary("b")], next_cursor="c2"),
            tools_api.ToolPage([_summary("b"), _summary("c")]),
        ])
        assert [t.id for t in self._drain()] == ["a", "b", "c"]

    def test_progress_is_reported_after_each_page(self, monkeypatch):
        self._pages(monkeypatch, [
            tools_api.ToolPage([_summary("a")], next_cursor="c2"),
            tools_api.ToolPage([_summary("b")]),
        ])
        seen: list = []
        self._drain(on_page=lambda all_so_far: seen.append([t.id for t in all_so_far]))
        assert seen == [["a"], ["a", "b"]]

    def test_a_cursor_that_never_clears_is_not_followed_forever(self, monkeypatch):
        """The whole point of the ceiling: a server bug must not hang the panel."""
        async def _endless(session, limit=100, *, cursor=None):
            return tools_api.ToolPage([_summary(f"t{cursor}")], next_cursor="always")

        monkeypatch.setattr(tools_api, "list_tools", _endless)
        assert len(self._drain()) <= tools_api.MAX_PAGES


def _summary(tool_id: str) -> tools_api.ToolSummary:
    return tools_api.ToolSummary(id=tool_id, name=tool_id.title())
