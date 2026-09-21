"""The pages this plugin links out to, and the version it shows for itself.

Small, but each one is a claim made to a user that nothing else checks. A dead
policy link in a plugin handed to an enterprise customer is found by their legal
review rather than by us, and a version label that has drifted from the manifest
is worse than no version at all, because it is quoted back in bug reports.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rundiffusion_omniverse.api import constants

PNG_MAGIC = bytes([137, 80, 78, 71, 13, 10, 26, 10])

EXTENSION_TOML = (
    Path(__file__).resolve().parents[1]
    / "exts"
    / "rundiffusion.omniverse"
    / "config"
    / "extension.toml"
)

#: Spelled out rather than derived, so a typo in the constant cannot also be
#: the expectation.
POLICY_URLS = {
    "PRIVACY_POLICY_URL": "https://www.rundiffusion.com/privacy-policy",
    "TERMS_OF_SERVICE_URL": "https://www.rundiffusion.com/terms-of-service",
    "COOKIE_POLICY_URL": "https://www.rundiffusion.com/cookie-policy",
}


@pytest.mark.parametrize("name,expected", sorted(POLICY_URLS.items()))
def test_the_policy_links_match_the_published_pages(name: str, expected: str):
    assert getattr(constants, name) == expected


def test_the_onboarding_link_names_this_plugin_kind():
    """The walkthrough is per plugin, and the kind is what routes it. A wrong
    one lands on a page for somebody else's host."""
    assert constants.ONBOARDING_URL.endswith(f"/plugins/{constants.PLUGIN_KIND}/onboarding")
    assert constants.PLUGIN_KIND == "omniverse"


def test_manage_devices_points_at_the_device_list():
    assert constants.MANAGE_DEVICES_URL == "https://app.rundiffusion.com/auth/devices"


@pytest.mark.parametrize("name", sorted(POLICY_URLS) + ["MANAGE_DEVICES_URL", "ONBOARDING_URL"])
def test_every_outbound_link_is_https(name: str):
    assert getattr(constants, name).startswith("https://")


def test_the_version_on_the_account_tab_matches_the_manifest():
    """The panel shows a version; the manifest declares one. One of them being
    edited alone is the drift this catches."""
    from rundiffusion_omniverse import panel

    declared = re.search(
        r'^version\s*=\s*"([^"]+)"', EXTENSION_TOML.read_text(encoding="utf-8"), re.M
    )

    assert declared is not None, "extension.toml declares no version"
    assert panel.EXTENSION_VERSION == declared.group(1)


def test_the_landing_screen_ships_its_mark():
    """The logo is a file the extension carries, not something it fetches.

    omni.ui.Image loads a path, so a missing or moved file is a landing screen
    with a hole in it. The panel already degrades to text if the file is gone;
    this is what notices that it went.
    """
    from rundiffusion_omniverse import panel

    assert panel.MARK_PATH.exists(), f"missing {panel.MARK_PATH}"
    assert panel.MARK_PATH.read_bytes().startswith(PNG_MAGIC)
