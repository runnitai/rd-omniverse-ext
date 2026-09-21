"""The status line is off by default, and failures ignore that.

It is a running commentary: "Downloading...", "Copied abc-123.", "Reused the
inputs from 14:05." Useful while the panel was being built, noise to someone
using it, and most of it repeats something already on screen. So it is off
unless the Account tab turns it on.

The line that cannot be off is a failure. There is no dialog in this panel and
no other place to say that a save did not happen, so hiding one behind a
preference would mean telling the person nothing at all. That split is what
these cover, along with the holding: a message that arrives while the line is
off has to be there when it is turned on, or the switch appears to do nothing
until the next thing happens.
"""

from __future__ import annotations

import pytest

from rundiffusion_omniverse import prefs
from rundiffusion_omniverse.panel import RunDiffusionPanel


class FakeLabel:
    def __init__(self) -> None:
        self.text = ""
        self.visible = True


@pytest.fixture(autouse=True)
def no_carb(monkeypatch):
    """No host, so preferences fall back to memory. Cleared between tests."""
    monkeypatch.setattr(prefs, "_FALLBACK", {})


@pytest.fixture
def panel():
    made = object.__new__(RunDiffusionPanel)
    made._status = FakeLabel()
    made._status_message = ""
    made._status_is_failure = False
    made._show_status_log = False
    return made


class TestWhenItIsOff:
    def test_chatter_is_not_shown(self, panel):
        panel._set_status("Downloading...")

        assert panel._status.text == ""
        assert panel._status.visible is False

    def test_a_failure_is_shown_anyway(self, panel):
        panel._set_error("Could not save: disk full.")

        assert panel._status.text == "Could not save: disk full."
        assert panel._status.visible is True

    def test_chatter_after_a_failure_takes_the_line_away_again(self, panel):
        """Otherwise the failure stays on screen under the next six things the
        user does, long after it stopped being true."""
        panel._set_error("Could not save: disk full.")
        panel._set_status("Downloading...")

        assert panel._status.visible is False


class TestWhenItIsOn:
    def test_chatter_is_shown(self, panel):
        panel._set_status_log(True)
        panel._set_status("Downloading...")

        assert panel._status.text == "Downloading..."
        assert panel._status.visible is True

    def test_turning_it_on_shows_what_was_already_said(self, panel):
        """The message is held rather than written straight to the label. A
        switch that shows an empty row until the next thing happens looks
        broken."""
        panel._set_status("Reused the inputs from 14:05.")

        panel._set_status_log(True)

        assert panel._status.text == "Reused the inputs from 14:05."

    def test_turning_it_off_takes_it_away_immediately(self, panel):
        panel._set_status_log(True)
        panel._set_status("Downloading...")

        panel._set_status_log(False)

        assert panel._status.visible is False

    def test_an_empty_message_never_shows_the_line(self, panel):
        """Several paths clear the status by setting it to "". An empty row
        with nothing in it reads as something that failed to load."""
        panel._set_status_log(True)
        panel._set_status("")

        assert panel._status.visible is False


class TestRemembering:
    def test_the_choice_is_written_down(self, panel):
        panel._set_status_log(True)

        assert prefs.get_bool(prefs.SHOW_STATUS_LOG) is True

    def test_it_is_off_until_someone_asks_for_it(self):
        assert prefs.get_bool(prefs.SHOW_STATUS_LOG) is False

    def test_turning_it_back_off_is_written_down_too(self, panel):
        panel._set_status_log(True)
        panel._set_status_log(False)

        assert prefs.get_bool(prefs.SHOW_STATUS_LOG) is False


class TestBeforeThereIsALabel:
    def test_a_message_with_no_footer_yet_is_not_a_crash(self, panel):
        """The panel says things during startup, before the workbench is
        built."""
        panel._status = None

        panel._set_status("Loading your accounts...")
        panel._set_error("Could not load tools.")

        assert panel._status_message == "Could not load tools."
