"""What the Library tab narrows on, and how that becomes a request.

The web client offers a dozen facets over the same `/library` surface. This
panel offers the seven that are about finding a picture: tool, model family,
media, aspect ratio, resolution, date and favorites. The three that are about
teams and boards are answered elsewhere here (the account picker in the footer)
or are not modelled at all, and a control that cannot do what it says is worse
than a missing one.

The rules that matter are all about ABSENCE. A filter that is off must not
appear in the query: `favorited=false` and no `favorited` at all reach the same
rows, but only one of them reads as a filter that is off, and an empty
`tool_id` is a filter matching nothing rather than a filter matching everything.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rundiffusion_omniverse.api.assets import (
    ASPECT_CHOICES,
    DATE_CHOICES,
    FAVORITE_CHOICES,
    RESOLUTION_CHOICES,
    LibraryFilters,
)

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


class TestWhatIsSent:
    def test_nothing_set_sends_nothing(self):
        assert LibraryFilters().as_params(now=NOW) == {}

    def test_each_facet_uses_the_name_the_v2_surface_takes(self):
        """v1 called these `node_def_id`, `model_family_tag_id` and so on, and
        v2 renamed every one of them. A retired spelling is REJECTED by name
        rather than ignored, so getting this wrong is a 400, not a silent
        unfiltered page."""
        filters = LibraryFilters(
            tool_id="tool-1",
            aspect_ratio="16:9",
            resolution="4K+",
            model_family_tool_tag_id="family-1",
            media_tool_tag_id="media-1",
        )

        assert filters.as_params(now=NOW) == {
            "tool_id": "tool-1",
            "aspect_ratio": "16:9",
            "resolution": "4K+",
            "model_family_tool_tag_id": "family-1",
            "media_tool_tag_id": "media-1",
        }

    def test_favorites_off_is_absent_rather_than_false(self):
        assert "favorited" not in LibraryFilters(favorited=False).as_params(now=NOW)

    def test_favorites_on_is_sent(self):
        assert LibraryFilters(favorited=True).as_params(now=NOW) == {"favorited": "true"}


class TestTheDateWindow:
    def test_any_time_sends_no_window(self):
        assert "start_utc" not in LibraryFilters(within_days=None).as_params(now=NOW)

    def test_a_window_counts_back_from_now(self):
        params = LibraryFilters(within_days=7).as_params(now=NOW)

        assert params["start_utc"] == "2026-08-18T12:00:00Z"

    def test_the_window_has_no_end(self):
        """It ends now. Naming that instant would exclude anything generated
        between building the request and the server reading it, which on a
        library sorted newest-first is exactly what is being looked for."""
        assert "end_utc" not in LibraryFilters(within_days=30).as_params(now=NOW)

    def test_a_local_clock_is_converted_rather_than_sent_as_it_reads(self):
        """`start_utc` is UTC by name. A Kit session in another zone must not
        move someone's idea of the last 24 hours by the offset."""
        local = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc).astimezone(
            timezone.max
        )

        params = LibraryFilters(within_days=1).as_params(now=local)

        assert params["start_utc"] == "2026-08-24T12:00:00Z"


class TestTheCountOnTheHeader:
    def test_nothing_set_counts_none(self):
        assert LibraryFilters().active_count == 0
        assert LibraryFilters().is_active is False

    def test_every_narrowing_filter_counts(self):
        filters = LibraryFilters(tool_id="tool-1", favorited=True, within_days=7)

        assert filters.active_count == 3
        assert filters.is_active is True


class TestTheVocabularies:
    def test_every_choice_list_offers_an_off_position_first(self):
        """An "Any" row first, and it means the parameter is omitted entirely rather
        than sent empty."""
        for choices in (ASPECT_CHOICES, RESOLUTION_CHOICES, DATE_CHOICES):
            assert choices[0][1] is None

        assert FAVORITE_CHOICES[0][1] is False

    def test_the_aspect_buckets_are_the_ones_the_server_defines(self):
        """Ten buckets, spelled as `ASPECT_RATIO_LABELS` spells them. A bucket
        this panel invented would be a 400, since the server validates the
        value against a choice list."""
        assert [value for _label, value in ASPECT_CHOICES[1:]] == [
            "1:1", "16:9", "2:1", "3:2", "4:3", "21:9", "9:16", "2:3", "3:4", "OTHER",
        ]

    def test_the_resolution_buckets_are_the_ones_the_server_defines(self):
        assert [value for _label, value in RESOLUTION_CHOICES[1:]] == ["1K", "2K", "4K+"]
