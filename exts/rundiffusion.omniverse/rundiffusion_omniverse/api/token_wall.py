"""Where to send a caller who cannot generate for a token reason.

The fix is chosen along TWO axes, not one. The server error code says
which wall this is, and the account kind says whether the caller can do anything
about it themselves.

    EMAIL_NOT_VERIFIED (403)   -> Unverified Wall. The fix is FREE: verifying
                                  email unlocks the free daily allotment. Route
                                  to the verification page, never to a paid one.

    INSUFFICIENT_BALANCE (402, -> Balance Wall, for an already-verified caller.
    or preview-cost's            Personal is self-serve, so offer the
    blocking_reason)             subscriptions page. A Team caller CANNOT refill
                                  a team budget from a personal checkout, so the
                                  team branch is informational and offers no
                                  purchase button at all.

Getting this wrong is not cosmetic. A single "go to subscriptions" route sends
the most common brand-new-plugin persona, an unverified user with free tokens
already waiting, to a paid page; and it dead-ends a team caller on a checkout
that cannot fix their problem.

This logic is deliberately pure so it can be tested without a UI: the panel
renders whatever this returns.
"""

from __future__ import annotations

from dataclasses import dataclass

WEB_BASE_URL = "https://app.rundiffusion.com"

VERIFY_EMAIL_URL = f"{WEB_BASE_URL}/account"
SUBSCRIPTIONS_URL = f"{WEB_BASE_URL}/account/subscription"

#: Analytics variants named by the ADR, so the three surfaces stay comparable.
VARIANT_VERIFY = "verify"
VARIANT_GET_TOKENS = "get_tokens"
VARIANT_TEAM_DEPLETED = "team_depleted"


@dataclass
class TokenWall:
    """What to show when a run is blocked for a token reason."""

    message: str
    #: None when there is nothing the caller can usefully click. A team caller
    #: gets no button rather than a button that cannot help.
    action_label: str | None
    action_url: str | None
    variant: str


def unverified_wall() -> TokenWall:
    return TokenWall(
        message=(
            "Verify your email to unlock your free daily tokens. "
            "It takes a moment and costs nothing."
        ),
        action_label="Verify email",
        action_url=VERIFY_EMAIL_URL,
        variant=VARIANT_VERIFY,
    )


def balance_wall(*, is_personal: bool, reason: str | None = None) -> TokenWall:
    if is_personal:
        return TokenWall(
            message=reason or "You do not have enough tokens to run this tool.",
            action_label="Get tokens",
            action_url=SUBSCRIPTIONS_URL,
            variant=VARIANT_GET_TOKENS,
        )

    # No button. A team budget cannot be topped up from a personal checkout, so
    # offering one would route the caller somewhere that cannot fix this.
    return TokenWall(
        message=(
            reason
            or "This team does not have enough tokens to run this tool. "
            "Contact your account admin to add more."
        ),
        action_label=None,
        action_url=None,
        variant=VARIANT_TEAM_DEPLETED,
    )


def wall_for_error(
    code: str | None, status: int, *, is_personal: bool, email_verified: bool
) -> TokenWall | None:
    """Map a failed generate onto a wall, or None if this is not a token block.

    Checked reactively, on the 403/402 from POST /generate, as well as
    proactively from preview-cost. Both routes exist because preview-cost is
    advisory and the balance can move between previewing and running.
    """
    if code == "EMAIL_NOT_VERIFIED" or status == 403 and not email_verified:
        return unverified_wall()
    if code == "INSUFFICIENT_BALANCE" or status == 402:
        return balance_wall(is_personal=is_personal)
    return None


def wall_for_preview(
    *, can_run: bool, blocking_reason: str | None, is_personal: bool, email_verified: bool
) -> TokenWall | None:
    """Map a preview-cost result onto a wall.

    Branch on `can_run`, never on the wording of `blocking_reason`: that string
    is a display message with no stable vocabulary. It is passed through to the
    user because it says more than a generic line would, but it is never parsed.
    """
    if can_run:
        return None
    if not email_verified:
        return unverified_wall()
    return balance_wall(is_personal=is_personal, reason=blocking_reason)
