"""Where to send a caller who cannot generate for a token or plan reason.

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

    FREEMIUM_API_NOT_ALLOWED   -> Plan Wall. Plugins need a paid plan. Personal
    (403)                         is offered the plans page; a team's plan is
                                  its administrators' to change, so that branch
                                  names them and offers nothing to buy.

Getting this wrong is not cosmetic. A single "go to subscriptions" route sends
the most common brand-new-plugin persona, an unverified user with free tokens
already waiting, to a paid page; and it dead-ends a team caller on a checkout
that cannot fix their problem.

There is a third axis on top of those two: whether the person picked the wrong
account. Someone whose team is on a paid plan, generating on their free personal
account, is refused with an upgrade prompt when the fix is to choose the team
they already pay through. So before any upgrade prompt for a personal account,
the panel asks the server whether one of the caller's teams could run the same
thing, and if one can, the wall offers to switch to it instead
(`switch_wall`). Which teams are worth asking is `teams_worth_checking`.

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
#: The personal account cannot pay for this run and one of the caller's teams
#: can: the fix is choosing that team, not buying anything.
VARIANT_SWITCH_ACCOUNT = "switch_account"
#: A personal account on a free plan, which plugins cannot generate from.
VARIANT_GET_PLAN = "get_plan"
#: A selected team the server refuses, for whichever of its reasons.
VARIANT_TEAM_BLOCKED = "team_blocked"
#: A selected team on a free plan.
VARIANT_TEAM_PLAN_REQUIRED = "team_plan_required"
#: A selected team that has been deactivated.
VARIANT_TEAM_INACTIVE = "team_inactive"

#: Generate refusals that mean a personal account cannot pay for the run: its
#: plan is free, or its balance is too low. A team plan fixes both, which is
#: what makes them worth asking a team about. Anything else (an unverified
#: email, a bad request) is refused on every account alike.
PERSONAL_PAYMENT_BLOCKS = frozenset({"FREEMIUM_API_NOT_ALLOWED", "INSUFFICIENT_BALANCE"})

#: The people who manage a team's plan, budget and roles, as the web app names
#: them. Said in full because "an admin" leaves a member asking which one.
_ADMINISTRATORS = "your account administrators"


@dataclass
class TokenWall:
    """What to show when a run is blocked for a token or plan reason."""

    message: str
    #: None when there is nothing the caller can usefully click. A team caller
    #: gets no button rather than a button that cannot help.
    action_label: str | None
    action_url: str | None
    variant: str
    #: A second, quieter line: who can fix it, or what the server said.
    detail: str | None = None
    #: The account to bill instead, when the fix is a different account rather
    #: than a page on the web. The button switches in place and opens nothing.
    switch_to: str | None = None


def _sentence(text: str | None) -> str:
    """The server's reason, ready to sit in front of another sentence.

    Its wording is not stable and it does not always end with a full stop
    ("Your balance of (12) is too low to run this tool"), so one is added.
    """
    text = (text or "").strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


def team_name(label: str | None, team_label: str | None = None) -> str:
    """What to call a team in a sentence. Its own name whenever it has one.

    The fallback says what the company calls a team rather than the word
    itself: a company can rename the concept, and copy that hardcodes it reads
    wrong to everyone in that company.
    """
    return label or f"This {team_label or 'Team'}"


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


def balance_wall(
    *, is_personal: bool, reason: str | None = None, account_name: str | None = None
) -> TokenWall:
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
        message=f"{team_name(account_name)} does not have enough tokens to run this tool.",
        action_label=None,
        action_url=None,
        variant=VARIANT_TEAM_DEPLETED,
        detail=f"Ask {_ADMINISTRATORS} to add more.",
    )


def plan_wall(*, is_personal: bool, account_name: str | None = None) -> TokenWall:
    """Plugins need a paid plan, and this account is on a free one."""
    if is_personal:
        return TokenWall(
            message="A paid plan is required to generate from plugins.",
            action_label="See plans",
            action_url=SUBSCRIPTIONS_URL,
            variant=VARIANT_GET_PLAN,
        )
    return TokenWall(
        message=f"{team_name(account_name)} needs a paid plan to generate from plugins.",
        action_label=None,
        action_url=None,
        variant=VARIANT_TEAM_PLAN_REQUIRED,
        detail=f"Ask {_ADMINISTRATORS} to upgrade it.",
    )


def team_blocked_wall(*, account_name: str | None, reason: str | None) -> TokenWall:
    """A selected team the estimate refuses, before anything is spent.

    The server's sentence says why. It carries no code, so the reasons (the
    plan, the budget, the role's limits, a deactivated team) cannot be told
    apart here, and every one of them is the administrators' to change. So the
    team is named, the server is quoted, and the people who can fix it are
    named, without guessing which of the reasons it is.
    """
    parts = [_sentence(reason), f"Its plan, token budget and permissions are managed by {_ADMINISTRATORS}."]
    return TokenWall(
        message=f"{team_name(account_name)} cannot run this tool.",
        action_label=None,
        action_url=None,
        variant=VARIANT_TEAM_BLOCKED,
        detail=" ".join(part for part in parts if part),
    )


def inactive_team_wall(*, account_name: str | None) -> TokenWall:
    return TokenWall(
        message=f"{team_name(account_name)} has been deactivated, so it cannot start new runs.",
        action_label=None,
        action_url=None,
        variant=VARIANT_TEAM_INACTIVE,
        detail=f"Choose another account under BILLED TO, or ask {_ADMINISTRATORS} to reactivate it.",
    )


def switch_wall(*, team_id: str, account_name: str) -> TokenWall:
    """The personal account cannot pay for this run, and `account_name` can.

    Says nothing about upgrading. The person already has a plan that covers
    this, through the team, and an upgrade prompt would have them buy a second
    one.
    """
    return TokenWall(
        message="Your personal account cannot run this tool.",
        action_label=f"Switch to {account_name}",
        action_url=None,
        variant=VARIANT_SWITCH_ACCOUNT,
        detail=f"{account_name} can. Switch to it to generate on its plan.",
        switch_to=team_id,
    )


def wall_for_error(
    code: str | None,
    status: int,
    *,
    is_personal: bool,
    email_verified: bool,
    account_name: str | None = None,
) -> TokenWall | None:
    """Map a failed generate onto a wall, or None if this is not a token block.

    Checked reactively, on the 403/402 from POST /generate, as well as
    proactively from preview-cost. Both routes exist because preview-cost is
    advisory and the balance can move between previewing and running.
    """
    if code == "EMAIL_NOT_VERIFIED" or status == 403 and not email_verified:
        return unverified_wall()
    if code == "FREEMIUM_API_NOT_ALLOWED":
        return plan_wall(is_personal=is_personal, account_name=account_name)
    if code == "TEAM_INACTIVE" and not is_personal:
        return inactive_team_wall(account_name=account_name)
    if code == "INSUFFICIENT_BALANCE" or status == 402:
        return balance_wall(is_personal=is_personal, account_name=account_name)
    return None


def wall_for_preview(
    *,
    can_run: bool,
    blocking_reason: str | None,
    is_personal: bool,
    email_verified: bool,
    account_name: str | None = None,
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
    if is_personal:
        wall = balance_wall(is_personal=True, reason=blocking_reason)
        if blocking_reason:
            # The reason may be the balance or the plan, and it is not parsed,
            # so the button names both rather than answering one of them under
            # a sentence about the other. The page sells both.
            wall.action_label = "See plans and tokens"
        return wall
    return team_blocked_wall(account_name=account_name, reason=blocking_reason)


def personal_cannot_pay(
    *,
    is_personal: bool,
    email_verified: bool,
    blocked_by_preview: bool = False,
    error_code: str | None = None,
    status: int | None = None,
) -> bool:
    """Whether another account could fix this block, so a team is worth asking.

    Only a verified personal account qualifies. An unverified caller is refused
    on every account until they verify, so asking a team would spend a request
    on the same answer, and a team that is refused is already the account its
    administrators chose.
    """
    if not is_personal or not email_verified:
        return False
    if blocked_by_preview:
        return True
    return error_code in PERSONAL_PAYMENT_BLOCKS or status == 402


def teams_worth_checking(accounts) -> list:
    """The caller's teams whose own role would let this run through.

    A team is skipped when its role says no to this plugin or to generating at
    all: it would refuse whatever its plan, so asking it only costs a request.
    Everything else is decided by the server, for the exact run on the form, so
    every remaining team is asked in the order the server lists them. Any of
    them may be the plan the person meant to use.
    """
    return [
        account
        for account in accounts
        if not account.is_personal
        and account.id
        and account.allows_this_plugin
        and account.permissions.get("GENERATE", True) is not False
    ]
