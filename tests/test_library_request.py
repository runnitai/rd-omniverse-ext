"""The URL a page of the library is actually asked for at.

The names on this request are the trap. v1 called the tool filter
`node_def_id`, the tag filters `model_family_tag_id` and `media_tag_id`, and
took a whole-day `date_from`; v2 renamed every one of them and REJECTS the old
spelling by name. So a filter built against the wrong version is not a filter
that quietly does nothing, it is a 400 on the whole page.

The other half is the cursor. Paging works by handing back the opaque
`next_cursor` untouched, alongside the same filters as the page before it: a
cursor is issued against a query, and following one with different filters
would page through a result set that no longer exists.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from rundiffusion_omniverse.api import assets as assets_api
from rundiffusion_omniverse.api.assets import LibraryFilters
from rundiffusion_omniverse.api.session import Account, RdSession


class Recorder:
    """Stands in for the transport and keeps the URL it was handed."""

    def __init__(self, payload=None) -> None:
        self.urls: list[str] = []
        self._payload = payload or {"data": [], "has_more": False, "next_cursor": None}

    async def __call__(self, _method, url, headers=None):
        self.urls.append(url)
        return self._payload

    @property
    def query(self) -> dict:
        parsed = parse_qs(urlparse(self.urls[-1]).query)
        return {key: value[0] for key, value in parsed.items()}

    @property
    def path(self) -> str:
        return urlparse(self.urls[-1]).path


@pytest.fixture
def session():
    made = RdSession(id_token="t", refresh_token="r", expires_at=1 << 40)
    made.accounts = [Account(id="personal", label="Personal", kind="PERSONAL")]
    return made


@pytest.fixture
def transport(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(assets_api, "request_json_async", recorder)
    return recorder


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


class TestTheLibraryRequest:
    def test_an_unfiltered_page_asks_for_a_limit_and_nothing_else(self, session, transport):
        _run(assets_api.list_library(session, 24))

        assert transport.path.endswith("/library")
        assert transport.query == {"limit": "24"}

    def test_the_filters_go_on_by_their_v2_names(self, session, transport):
        _run(
            assets_api.list_library(
                session,
                24,
                filters=LibraryFilters(
                    tool_id="tool-1",
                    model_family_tool_tag_id="family-1",
                    media_tool_tag_id="media-1",
                    aspect_ratio="16:9",
                ),
            )
        )

        query = transport.query
        assert query["tool_id"] == "tool-1"
        assert query["model_family_tool_tag_id"] == "family-1"
        assert query["media_tool_tag_id"] == "media-1"
        assert query["aspect_ratio"] == "16:9"
        # The retired v1 spellings, which v2 answers with a 400 rather than by
        # ignoring them.
        assert "node_def_id" not in query
        assert "model_family_tag_id" not in query
        assert "media_tag_id" not in query

    def test_a_later_page_carries_the_cursor_and_the_same_filters(self, session, transport):
        """A cursor is issued against a query. Following it with different
        filters would page through a result set that no longer exists."""
        filters = LibraryFilters(tool_id="tool-1")

        _run(assets_api.list_library(session, 24, cursor="opaque-2", filters=filters))

        assert transport.query["cursor"] == "opaque-2"
        assert transport.query["tool_id"] == "tool-1"

    def test_a_team_account_narrows_the_pool(self, session, transport):
        session.accounts.append(Account(id="team-1", label="Team", kind="TEAM"))
        session.selected_account_id = "team-1"

        _run(assets_api.list_library(session, 24))

        assert transport.query["team_id"] == "team-1"


class TestTheUploadsRequest:
    def test_uploads_take_a_limit_and_a_cursor_and_nothing_else(self, session, transport):
        """`/uploads` publishes no filters at all, which is why the Uploads tab
        offers none: a dropdown there would be a control that cannot act."""
        _run(assets_api.list_uploads(session, 24, cursor="opaque-2"))

        assert transport.path.endswith("/uploads")
        assert transport.query == {"limit": "24", "cursor": "opaque-2"}


class TestWhatComesBack:
    """The listing publishes `type` and `mime_type`, and the plugin threw both
    away. Everything downstream that writes a file or draws a picture needs
    them, so an item that is not an image has to arrive knowing that."""

    def test_a_library_video_arrives_as_a_video(self, session, monkeypatch):
        recorder = Recorder(
            {
                "data": [
                    {
                        "id": "run:res",
                        "type": "VID",
                        "mime_type": "video/mp4",
                        "url": "https://storage/clip.mp4?sig=1",
                    }
                ],
                "has_more": False,
                "next_cursor": None,
            }
        )
        monkeypatch.setattr(assets_api, "request_json_async", recorder)

        page = _run(assets_api.list_library(session, 24))

        assert page.items[0].is_image is False
        assert page.items[0].extension == ".mp4"

    def test_a_text_result_is_still_withheld(self, session, monkeypatch):
        """v2 withholds these from this surface, but a text row in an image
        grid would be a puzzle, so the check stays."""
        recorder = Recorder(
            {
                "data": [
                    {"id": "a", "type": "TEXT"},
                    {"id": "b", "type": "IMG", "mime_type": "image/png"},
                ],
                "has_more": False,
                "next_cursor": None,
            }
        )
        monkeypatch.setattr(assets_api, "request_json_async", recorder)

        assert [item.id for item in _run(assets_api.list_library(session, 24)).items] == ["b"]

    def test_an_upload_is_an_image_even_with_no_mime(self, session, monkeypatch):
        """`/uploads` publishes no `type` and its `mime_type` is optional. The
        endpoint accepts only images, so an upload is one by construction, and
        leaving that unstated filtered every such upload out of the picker and
        wrote it to disk as `.bin`."""
        recorder = Recorder(
            {
                "data": [{"id": "u1", "url": "https://storage/u1"}],
                "has_more": False,
                "next_cursor": None,
            }
        )
        monkeypatch.setattr(assets_api, "request_json_async", recorder)

        assert _run(assets_api.list_uploads(session, 24)).items[0].is_image is True
