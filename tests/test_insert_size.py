"""The size a 3D asset is added to the stage at, as it is remembered.

The prompt offers the last answer again, which is only a convenience while the
saved answer is usable. The settings file is the user's to edit, so a value
this version cannot use has to fall back to the default rather than prefill a
dialog that then refuses its own starting value.
"""

from __future__ import annotations

import math

import pytest

from rundiffusion_omniverse import insert_size, prefs
from rundiffusion_omniverse.insert_size import (
    DEFAULT_SIZE,
    remember_size,
    remembered_size,
    usable_length,
)
from rundiffusion_omniverse.stage import UNITS, InsertSize, meters_per


@pytest.fixture(autouse=True)
def clean_prefs(monkeypatch):
    monkeypatch.setattr(prefs, "_settings", lambda: None)
    monkeypatch.setattr(prefs, "_FALLBACK", {})


class TestRememberingTheLastSize:
    def test_the_first_prompt_offers_the_default(self):
        assert remembered_size() == DEFAULT_SIZE

    def test_what_was_chosen_is_offered_next_time(self):
        remember_size(InsertSize(45.0, "cm"))
        assert remembered_size() == InsertSize(45.0, "cm")

    @pytest.mark.parametrize("length", [0.0, -3.0, math.inf, math.nan])
    def test_an_unusable_saved_length_falls_back(self, length):
        prefs.set_float(prefs.INSERT_LENGTH, length)
        prefs.set_string(prefs.INSERT_UNIT, "ft")
        assert remembered_size() == InsertSize(DEFAULT_SIZE.length, "ft")

    def test_an_unknown_saved_unit_falls_back(self):
        # A unit a later version added, read by this one after a downgrade.
        prefs.set_float(prefs.INSERT_LENGTH, 7.0)
        prefs.set_string(prefs.INSERT_UNIT, "yd")
        assert remembered_size() == InsertSize(7.0, DEFAULT_SIZE.unit)

    def test_a_saved_length_that_is_not_a_number_falls_back(self):
        prefs._FALLBACK[prefs.INSERT_LENGTH] = "big"
        assert remembered_size().length == DEFAULT_SIZE.length


class TestWhatCountsAsALength:
    @pytest.mark.parametrize("value", [0.001, 1.0, 1e6])
    def test_positive_lengths_are_usable(self, value):
        assert usable_length(value) is True

    @pytest.mark.parametrize("value", [0.0, -1.0, math.nan, math.inf])
    def test_nothing_else_is(self, value):
        assert usable_length(value) is False


class TestTheUnitsOffered:
    def test_every_offered_unit_converts(self):
        for unit, meters in UNITS:
            assert meters_per(unit) == meters

    def test_the_default_is_one_of_them(self):
        assert DEFAULT_SIZE.unit in dict(UNITS)

    def test_the_dialog_uses_the_stage_modules_list(self):
        # One list, so the dropdown and the arithmetic cannot disagree.
        assert insert_size.UNITS is UNITS
