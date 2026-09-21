"""Tests for the two doors on the landing screen.

Sign in and Create a free account run the SAME device flow. The only difference
is one query parameter on the URL the browser opens: without it a brand-new user
lands on a login wall and has to find the sign-up link themselves, which is the
worst possible first thirty seconds of a plugin.

It is one string concatenation, and it is worth pinning because it is invisible
from inside the plugin: both buttons look like they worked either way, and the
difference only shows up in a browser tab nobody is watching during a test.
"""

from __future__ import annotations

import pytest

from rundiffusion_omniverse.api.session import (
    INTENT_SIGN_IN,
    INTENT_SIGN_UP,
    verification_url_for_intent,
)

URL = "https://app.rundiffusion.com/auth/device?plugin=omniverse"


def test_signing_in_opens_the_url_the_server_gave():
    assert verification_url_for_intent(URL, INTENT_SIGN_IN) == URL


def test_signing_up_asks_the_web_for_the_sign_up_form():
    assert verification_url_for_intent(URL, INTENT_SIGN_UP) == f"{URL}&intent=signup"


def test_the_separator_follows_the_url_it_is_given():
    """The verification URL already carries ?plugin=, but that is the server's
    choice and not this plugin's to assume."""
    plain = "https://app.rundiffusion.com/auth/device"

    assert verification_url_for_intent(plain, INTENT_SIGN_UP) == f"{plain}?intent=signup"


@pytest.mark.parametrize("intent", ["", "signup", "SIGN_UP", "other", None])
def test_anything_but_the_exact_token_is_a_plain_sign_in(intent):
    """Defaulting to the sign-up form for an unrecognised value would send
    existing users somewhere they cannot use."""
    assert verification_url_for_intent(URL, intent) == URL
