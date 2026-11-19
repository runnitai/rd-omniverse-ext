"""The panel's reading of an account, and of a refusal, says what the server does.

Two places where the panel used to be more generous than the server, and one
where it guessed at the reason for a refusal:

- An account was taken to allow this plugin when it published no USE_PLUGINS
  and no plugin kinds. The server denies a team account in both cases, so the
  panel offered a team, and priced runs on it, that the server would refuse.
- A team that published no GENERATE was asked whether it could run something,
  while Generate itself needs an explicit yes. The two now agree.
- Any 403 for a caller whose email looked unverified was sent to the verify
  page. The server refuses an unverified email first, with its own code, so a
  403 with any other code is about something else, and a spending limit set by
  the team's role was one of them.
"""

from __future__ import annotations

import pytest

from rundiffusion_omniverse.api.session import Account
from rundiffusion_omniverse.api.token_wall import (
    VARIANT_GET_PLAN,
    VARIANT_ROLE_BLOCKED,
    VARIANT_TEAM_POLICY,
    VARIANT_VERIFY,
    teams_worth_checking,
    token_policy_wall,
    wall_for_error,
    wall_for_preview,
)


def _team(**permissions) -> Account:
    kinds = permissions.pop("plugin_kinds", ["*"])
    return Account(
        id="t1", label="alpha Team", kind="TEAM",
        permissions=permissions, plugin_kinds=kinds, team_label="Team",
    )


def _all_copy(wall) -> str:
    return " ".join(part for part in (wall.message, wall.detail, wall.action_label) if part)


class TestWhoMayUseThisPlugin:
    def test_a_personal_account_is_never_gated(self):
        """The server has no role to consult for a personal account, so it lets
        every plugin through whatever the account publishes."""
        assert Account(id="p1", label="Personal", kind="PERSONAL").allows_this_plugin

    def test_a_team_without_use_plugins_is_denied(self):
        assert not _team(GENERATE=True).allows_this_plugin

    def test_a_team_with_no_plugin_kinds_is_denied(self):
        assert not _team(USE_PLUGINS=True, plugin_kinds=[]).allows_this_plugin

    def test_a_team_with_both_is_allowed(self):
        assert _team(USE_PLUGINS=True, plugin_kinds=["*"]).allows_this_plugin
        assert _team(USE_PLUGINS=True, plugin_kinds=["omniverse"]).allows_this_plugin


class TestWhichTeamsAreAskedToRunIt:
    def test_a_team_that_publishes_no_generate_is_not_asked(self):
        """The same explicit yes Generate needs. Asking it would spend a request
        on a team the panel would then not let the person generate on."""
        assert teams_worth_checking([_team(USE_PLUGINS=True)]) == []

    def test_a_team_that_says_yes_is(self):
        team = _team(USE_PLUGINS=True, GENERATE=True)

        assert teams_worth_checking([team]) == [team]


class TestTheVerifyWallIsForTheVerifyRefusal:
    def test_the_code_goes_to_verification(self):
        wall = wall_for_error("EMAIL_NOT_VERIFIED", 403, is_personal=True, email_verified=False)

        assert wall.variant == VARIANT_VERIFY

    def test_another_403_is_not_sent_to_verification(self):
        """The email flag the panel holds can be stale; the server's code is not."""
        assert wall_for_error("FORBIDDEN", 403, is_personal=False, email_verified=False) is None

    def test_a_plan_refusal_is_about_the_plan(self):
        wall = wall_for_error(
            "FREEMIUM_API_NOT_ALLOWED", 403, is_personal=True, email_verified=False
        )

        assert wall.variant == VARIANT_GET_PLAN


class TestTokenPolicyRefusals:
    REASON = "This run exceeds your total run token limit"

    def test_a_teams_spending_limit_is_named_with_no_purchase(self):
        wall = wall_for_error(
            "TOKEN_POLICY", 403, is_personal=False, email_verified=True,
            account_name="alpha Team", message=self.REASON,
        )

        assert wall.variant == VARIANT_TEAM_POLICY
        assert wall.message == "alpha Team limits the tokens your role can spend."
        assert wall.detail.startswith(self.REASON + ". ")
        assert "your account administrators" in wall.detail
        assert wall.action_label is None and wall.action_url is None
        assert wall.switch_to is None

    def test_an_unverified_looking_caller_still_gets_the_policy(self):
        wall = wall_for_error(
            "TOKEN_POLICY", 403, is_personal=False, email_verified=False,
            account_name="alpha Team", message=self.REASON,
        )

        assert wall.variant == VARIANT_TEAM_POLICY

    def test_a_reason_that_is_only_a_code_is_said_in_words(self):
        wall = token_policy_wall(account_name="alpha Team", reason="TOKEN_COOLDOWN_ACTIVE")

        assert "TOKEN_COOLDOWN_ACTIVE" not in _all_copy(wall)
        assert wall.detail.startswith("Your role's token allowance for this period is used up. ")

    def test_an_unknown_code_is_left_out_rather_than_shown(self):
        wall = token_policy_wall(account_name="alpha Team", reason="SOME_NEW_CODE")

        assert "SOME_NEW_CODE" not in _all_copy(wall)
        assert wall.detail.startswith("Ask your account administrators")

    def test_a_nameless_team_is_called_what_its_company_calls_teams(self):
        assert token_policy_wall(account_name=None, team_label="Studio").message.startswith(
            "This Studio limits"
        )

    def test_an_estimate_refused_for_the_same_reason_says_it_in_words_too(self):
        """The estimate carries the same reason in its blocking_reason, and an
        edit after a refused run re-prices: the code must not come back then."""
        wall = wall_for_preview(
            can_run=False, blocking_reason="TOKEN_COOLDOWN_ACTIVE",
            is_personal=False, email_verified=True, account_name="alpha Team",
        )

        assert "TOKEN_COOLDOWN_ACTIVE" not in _all_copy(wall)
        assert wall.detail.startswith("Your role's token allowance for this period is used up. ")

    def test_an_estimate_never_shows_a_bare_code_on_a_personal_account_either(self):
        wall = wall_for_preview(
            can_run=False, blocking_reason="SOME_NEW_CODE",
            is_personal=True, email_verified=True,
        )

        assert "SOME_NEW_CODE" not in _all_copy(wall)
        assert wall.message

    def test_an_estimates_sentence_is_still_quoted(self):
        wall = wall_for_preview(
            can_run=False, blocking_reason=self.REASON,
            is_personal=False, email_verified=True, account_name="alpha Team",
        )

        assert wall.detail.startswith(self.REASON + ". ")

    def test_a_personal_account_is_left_to_the_servers_message(self):
        """A personal account carries no role to set a limit."""
        assert (
            wall_for_error(
                "TOKEN_POLICY", 403, is_personal=True, email_verified=True, message=self.REASON
            )
            is None
        )


class TestPermissionDeniedRefusals:
    def test_the_generate_permission_is_the_role_wall(self):
        wall = wall_for_error(
            "PERMISSION_DENIED", 403, details={"required_permission": "GENERATE"},
            is_personal=False, email_verified=False, account_name="alpha Team",
        )

        assert wall.variant == VARIANT_ROLE_BLOCKED

    @pytest.mark.parametrize("verified", [True, False])
    def test_any_other_is_left_to_the_servers_message(self, verified):
        """A run-count limit, say. It names no permission, and neither a role
        wall nor the verify page would describe it."""
        assert (
            wall_for_error(
                "PERMISSION_DENIED", 403, details=None,
                is_personal=False, email_verified=verified, account_name="alpha Team",
            )
            is None
        )


def test_no_new_copy_has_an_em_dash():
    walls = [
        token_policy_wall(account_name="alpha Team", reason="This run exceeds your total run token limit"),
        token_policy_wall(account_name=None, reason="TOKEN_COOLDOWN_ACTIVE"),
    ]
    for wall in walls:
        assert chr(0x2014) not in _all_copy(wall)
