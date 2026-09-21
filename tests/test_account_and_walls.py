"""Tests for account selection and token-wall routing.

Both are pure decisions with a UI bolted on, and both are wrong in ways that
look fine on screen: a `team_id` sent for a personal account still returns
tools, and a wall that routes a team caller to a personal checkout still shows a
button. Neither would fail loudly.
"""

from __future__ import annotations

from rundiffusion_omniverse.api.session import Account, RdSession
from rundiffusion_omniverse.api.token_wall import (
    SUBSCRIPTIONS_URL,
    VARIANT_GET_TOKENS,
    VARIANT_TEAM_DEPLETED,
    VARIANT_VERIFY,
    VERIFY_EMAIL_URL,
    wall_for_error,
    wall_for_preview,
)

PERSONAL = Account(id="p1", label="Personal", kind="PERSONAL")
TEAM = Account(id="t1", label="Acme", kind="TEAM")


def _session(*accounts: Account, selected: str | None = None, verified: bool = True) -> RdSession:
    session = RdSession()
    session.accounts = list(accounts)
    session.selected_account_id = selected
    session.user = {"email": "a@b.com", "email_verified": verified}
    return session


class TestAccountParams:
    def test_personal_sends_no_team_id(self):
        """The account follows this parameter alone, so a personal run OMITS it
        rather than sending it empty."""
        session = _session(PERSONAL, TEAM, selected="p1")

        assert session.account_params() == {}

    def test_team_sends_its_id(self):
        session = _session(PERSONAL, TEAM, selected="t1")

        assert session.account_params() == {"team_id": "t1"}

    def test_no_selection_falls_back_to_personal(self):
        session = _session(PERSONAL, TEAM)

        assert session.account_params() == {}

    def test_it_is_a_query_parameter_not_a_header(self):
        """v1 used the X-Account-Id header. v2 does not, and sending the header
        instead would silently bill the wrong account."""
        session = _session(PERSONAL, TEAM, selected="t1")
        params = session.account_params()

        assert "team_id" in params
        assert not any(key.lower().startswith("x-") for key in params)


class TestUnverifiedWall:
    """An unverified caller has free tokens waiting. Routing them to a paid page
    is the specific misroute the two-axis routing exists to prevent, and it
    hits the most common brand-new-plugin persona."""

    def test_preview_routes_an_unverified_caller_to_verification(self):
        wall = wall_for_preview(
            can_run=False,
            blocking_reason="Your balance is too low",
            is_personal=True,
            email_verified=False,
        )

        assert wall is not None
        assert wall.variant == VARIANT_VERIFY
        assert wall.action_url == VERIFY_EMAIL_URL

    def test_the_error_code_routes_there_too(self):
        wall = wall_for_error(
            "EMAIL_NOT_VERIFIED", 403, is_personal=True, email_verified=False
        )

        assert wall is not None
        assert wall.variant == VARIANT_VERIFY

    def test_verification_beats_balance_even_when_both_look_true(self):
        """The balance reason is present but the fix is free, so verification
        wins."""
        wall = wall_for_preview(
            can_run=False,
            blocking_reason="Your balance of (0) is too low",
            is_personal=False,
            email_verified=False,
        )

        assert wall is not None
        assert wall.variant == VARIANT_VERIFY


class TestBalanceWall:
    def test_a_personal_caller_can_self_serve(self):
        wall = wall_for_preview(
            can_run=False, blocking_reason=None, is_personal=True, email_verified=True
        )

        assert wall is not None
        assert wall.variant == VARIANT_GET_TOKENS
        assert wall.action_url == SUBSCRIPTIONS_URL

    def test_a_team_caller_gets_no_purchase_button(self):
        """A team budget cannot be refilled from a personal checkout, so a
        button here would dead-end them."""
        wall = wall_for_preview(
            can_run=False, blocking_reason=None, is_personal=False, email_verified=True
        )

        assert wall is not None
        assert wall.variant == VARIANT_TEAM_DEPLETED
        assert wall.action_label is None
        assert wall.action_url is None

    def test_the_servers_wording_is_shown_but_never_parsed(self):
        """blocking_reason says more than a generic line, so it is displayed.
        Branching happens on can_run, never on this text."""
        reason = "Your balance of (12) is too low to run this tool"
        wall = wall_for_preview(
            can_run=False, blocking_reason=reason, is_personal=True, email_verified=True
        )

        assert wall is not None
        assert wall.message == reason

    def test_a_402_on_generate_raises_the_same_wall(self):
        """preview-cost is advisory, not a reservation, so the balance can move
        between checking and running."""
        wall = wall_for_error(
            "INSUFFICIENT_BALANCE", 402, is_personal=True, email_verified=True
        )

        assert wall is not None
        assert wall.variant == VARIANT_GET_TOKENS


class TestNotAWall:
    def test_a_runnable_preview_produces_nothing(self):
        assert (
            wall_for_preview(
                can_run=True, blocking_reason=None, is_personal=True, email_verified=True
            )
            is None
        )

    def test_an_unrelated_error_is_not_a_token_block(self):
        """A 400 for a bad payload must not be dressed up as a billing problem."""
        assert (
            wall_for_error("INVALID_REQUEST", 400, is_personal=True, email_verified=True)
            is None
        )


class TestPluginAccessGate:
    """Both gates have to pass: USE_PLUGINS says whether plugins work at all,
    plugin_kinds says which ones. Reading them client-side is only so the panel
    can explain itself; the server enforces this regardless."""

    def test_the_wildcard_grants_this_plugin(self):
        """An account carrying "*" is allowed every kind, which is how it works
        with a plugin released after the account was set up."""
        account = Account(
            id="p1", label="Personal", kind="PERSONAL",
            permissions={"USE_PLUGINS": True}, plugin_kinds=["*", "another-kind"],
        )

        assert account.allows_this_plugin

    def test_an_enumerated_list_must_name_omniverse(self):
        """An account can list its kinds explicitly instead, and then the list
        has to name this one or the panel is denied."""
        allowed = Account(
            id="t1", label="Acme", kind="TEAM",
            permissions={"USE_PLUGINS": True},
            plugin_kinds=["omniverse", "another-kind"],
        )
        denied = Account(
            id="t2", label="Beta", kind="TEAM",
            permissions={"USE_PLUGINS": True}, plugin_kinds=["another-kind"],
        )

        assert allowed.allows_this_plugin
        assert not denied.allows_this_plugin

    def test_use_plugins_off_denies_regardless_of_kinds(self):
        account = Account(
            id="t1", label="Acme", kind="TEAM",
            permissions={"USE_PLUGINS": False}, plugin_kinds=["omniverse"],
        )

        assert not account.allows_this_plugin

    def test_an_absent_list_stays_permissive(self):
        """No published list is not a denial. The server is the authority, so
        the call is left to be the thing that refuses."""
        account = Account(id="p1", label="Personal", kind="PERSONAL")

        assert account.allows_this_plugin
