"""The tag vocabulary the Model family and Media filters are built from.

Tools reference tags by id and nothing else resolved them, which is why
`/api/v2/tool-tags` exists at all. It answers the whole taxonomy unpaged, and
this module's only jobs are to parse it and to group it by type so a picker can
be built per taxonomy.

Grouping is deliberately not an enumeration. The set of types can grow, so an
unfamiliar one becomes a group of its own rather than an error or a silent drop.
"""

from __future__ import annotations

import asyncio

import pytest

from rundiffusion_omniverse.api import tool_tags
from rundiffusion_omniverse.api.session import RdSession
from rundiffusion_omniverse.api.transport import ApiError
from rundiffusion_omniverse.api.tool_tags import (
    TYPE_MEDIA,
    TYPE_MODEL_FAMILY,
    ToolTag,
    group_by_type,
)


class TestGrouping:
    def test_tags_land_under_their_own_taxonomy(self):
        grouped = group_by_type(
            [
                ToolTag("a", TYPE_MODEL_FAMILY, "Flux"),
                ToolTag("b", TYPE_MEDIA, "Image"),
                ToolTag("c", TYPE_MODEL_FAMILY, "Seedream"),
            ]
        )

        assert [tag.id for tag in grouped[TYPE_MODEL_FAMILY]] == ["a", "c"]
        assert [tag.id for tag in grouped[TYPE_MEDIA]] == ["b"]

    def test_a_taxonomy_nobody_here_knows_about_is_still_a_group(self):
        """The type list is the server's and can grow. Dropping what this panel
        does not recognise would make a filter silently incomplete the day a
        taxonomy is added."""
        grouped = group_by_type([ToolTag("a", "INDUSTRY", "Architecture")])

        assert [tag.id for tag in grouped["INDUSTRY"]] == ["a"]

    def test_a_bucket_is_sorted_by_what_is_displayed(self):
        """The endpoint publishes no order, and a dropdown whose rows move
        between sessions is one nobody learns the shape of."""
        grouped = group_by_type(
            [
                ToolTag("a", TYPE_MODEL_FAMILY, "Wan"),
                ToolTag("b", TYPE_MODEL_FAMILY, "flux"),
                ToolTag("c", TYPE_MODEL_FAMILY, "Nano Banana"),
            ]
        )

        assert [tag.label for tag in grouped[TYPE_MODEL_FAMILY]] == [
            "flux",
            "Nano Banana",
            "Wan",
        ]

    def test_a_tag_with_no_label_is_named_by_its_id(self):
        """Displaying an empty row would offer a filter with nothing written on
        it, which is worse than showing the id nobody wanted to see."""
        assert str(ToolTag("abc123", TYPE_MEDIA, None)) == "abc123"

    def test_nothing_at_all_groups_to_nothing(self):
        assert group_by_type([]) == {}


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


@pytest.fixture
def session():
    return RdSession(id_token="t", refresh_token="r", expires_at=1 << 40)


class TestFailing:
    """The vocabulary is optional, and the caller spawns this as a background
    task where an escaping exception is not a failure anybody sees. So it does
    not raise: an empty list draws the two rows as unavailable, which is the
    whole cost of losing it."""

    def test_a_refused_request_is_an_empty_vocabulary(self, session, monkeypatch):
        async def _refuse(*_args, **_kwargs):
            raise ApiError(503, message="Service unavailable.")

        monkeypatch.setattr(tool_tags, "request_json_async", _refuse)

        assert _run(tool_tags.list_tool_tags(session)) == []

    def test_a_payload_of_the_wrong_shape_is_an_empty_vocabulary(
        self, session, monkeypatch
    ):
        """`data` as a bare string rather than a list of objects. Not an
        `ApiError`, which is exactly why the handler is broader than one."""

        async def _nonsense(*_args, **_kwargs):
            return {"data": "not-a-list"}

        monkeypatch.setattr(tool_tags, "request_json_async", _nonsense)

        assert _run(tool_tags.list_tool_tags(session)) == []

    def test_a_row_with_no_id_is_skipped_rather_than_fatal(self, session, monkeypatch):
        async def _partial(*_args, **_kwargs):
            return {"data": [{"label": "Nameless"}, {"id": "a", "label": "Flux"}]}

        monkeypatch.setattr(tool_tags, "request_json_async", _partial)

        assert [tag.id for tag in _run(tool_tags.list_tool_tags(session))] == ["a"]

    def test_a_cancellation_is_honoured_rather_than_swallowed(
        self, session, monkeypatch
    ):
        """Sign-out and account switches cancel this. A cancellation caught by
        the broad handler would be one the task never actually honours."""

        async def _cancelled(*_args, **_kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(tool_tags, "request_json_async", _cancelled)

        with pytest.raises(asyncio.CancelledError):
            _run(tool_tags.list_tool_tags(session))
