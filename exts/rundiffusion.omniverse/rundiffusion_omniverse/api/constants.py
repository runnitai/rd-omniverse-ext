"""Host identity and API constants for the Omniverse plugin.

Kept in one small module because there is no shared client library to lift
them into: this file is the single place a value that identifies this host is
defined.

Every value here is deliberately distinct from any other RunDiffusion client's,
so the token store of one host can never be read, cleared, or decrypted by
another.
"""

from __future__ import annotations

import os

# v2, not v1. v1 is the older API and is deprecated: its responses carry an
# RFC-8594 Deprecation header, so a plugin that starts on it is born needing a
# migration.
PROD_API_BASE_URL = "https://api2.rundiffusion.com/api/v2"

API_BASE_URL_ENV_VAR = "RD_OMNIVERSE_API_BASE_URL"

# How this plugin names itself to the API, sent on device/start and checked
# there.
#
# Not free-form, and not cosmetic: the API accepts only kinds it already knows
# about, so this string cannot be invented or changed here alone. An account
# also has to be allowed to act as this kind before sign-in completes, which is
# what `plugin_kinds` on the account describes.
PLUGIN_KIND = "omniverse"

#: Sentinel in an account's plugin_kinds meaning "every kind, including ones not
#: yet defined". An account publishes either this wildcard or an explicit list,
#: depending on how it is set up, so both shapes have to be handled.
PLUGIN_KIND_WILDCARD = "*"

# The `plugin` field on POST /generate, for analytics attribution. Without it a
# run is indistinguishable from generic API traffic.
GENERATE_ATTRIBUTION = "OMNIVERSE"

# %LOCALAPPDATA%\RunDiffusionOmniverse. Never share this folder or the entropy
# below with another host: a shared folder lets one plugin's sign-out clear the
# other's session, and shared entropy lets either decrypt the other's refresh
# token.
TOKEN_STORE_FOLDER_NAME = "RunDiffusionOmniverse"
TOKEN_STORE_ENTROPY = "RunDiffusionOmniverse.auth.v1"

SURFACE_TAG = "omniverse"
DEFAULT_OUTPUT_FILE_NAME = "omniverse-output.png"

# ---- Pages this plugin links out to ---------------------------------------
#
# Here for a reason beyond tidiness: a plugin that handles an account and its
# data has to make the policies reachable FROM INSIDE ITSELF, not only from a
# web page the user would have to know to go and find. Some distribution
# channels require exactly that of a submission, and a customer's legal review
# asks the same question whether or not anyone is obliged to.

WEB_BASE_URL = "https://app.rundiffusion.com"
SITE_BASE_URL = "https://www.rundiffusion.com"

#: Where users manage and revoke the plugin devices they have signed in from.
#: Worth surfacing here because THIS host is one of those devices.
MANAGE_DEVICES_URL = f"{WEB_BASE_URL}/auth/devices"

#: Per-plugin onboarding walkthrough. Public, and handles its own sign-in
#: display, so it opens without a session.
ONBOARDING_URL = f"{WEB_BASE_URL}/plugins/{PLUGIN_KIND}/onboarding"

PRIVACY_POLICY_URL = f"{SITE_BASE_URL}/privacy-policy"
TERMS_OF_SERVICE_URL = f"{SITE_BASE_URL}/terms-of-service"
COOKIE_POLICY_URL = f"{SITE_BASE_URL}/cookie-policy"


def api_base_url() -> str:
    """Production unless the env var overrides it.

    Kit reads the environment at launch, so an override has to be set before the
    app starts, not from inside a running session.
    """
    return os.environ.get(API_BASE_URL_ENV_VAR) or PROD_API_BASE_URL
