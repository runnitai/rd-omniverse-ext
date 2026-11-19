"""Where to send a caller who cannot generate for a token, plan or role reason.

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

    PERMISSION_DENIED (403)    -> Role Wall. The team's role for this person
    naming GENERATE               does not allow generating. Nothing to buy and
                                  nothing to verify: it names the team and the
                                  administrators who manage its roles. Also
                                  drawn up front from the account list, so the
                                  panel does not wait to be refused.

    TOKEN_POLICY (403)         -> Policy Wall. The team's role caps what this
                                  person may spend, and the run is over the cap.
                                  Named with the team and its administrators,
                                  and nothing to buy: a purchase does not lift
                                  a limit the role sets.

The verify wall answers EMAIL_NOT_VERIFIED alone. The server refuses an
unverified email before anything else, with that code, so a 403 carrying any
other code is about something else, whatever the email flag the panel holds
says (it can be stale).

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
#: A selected account whose role does not allow generating.
VARIANT_ROLE_BLOCKED = "role_blocked"
#: A selected team whose role caps what this person may spend.
VARIANT_TEAM_POLICY = "team_token_policy"
#: A selected account whose role hides every tool in its catalogue.
VARIANT_NO_TOOLS = "no_tools"

#: Spending-limit refusals whose reason arrives as a code rather than a
#: sentence, said in words. Any other code is left out rather than shown.
_POLICY_REASONS = {
    "TOKEN_COOLDOWN_ACTIVE": "Your role's token allowance for this period is used up.",
}

#: The permission a run needs, spelled as the account list publishes it and as
#: a refusal names it in `details.required_permission`.
GENERATE_PERMISSION = "GENERATE"

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


def readable_reason(reason: str | None) -> str:
    """The server's reason as something a person can read, or "".

    Its reasons are sentences, except a spending limit's, which can arrive as a
    bare code: on a refused run and in an estimate's blocking_reason alike. A
    known code is put into words; an unknown one is left out rather than shown,
    since a code says nothing to the person reading it. Nothing branches on the
    result: it is display copy, the same as the sentence it stands in for.
    """
    text = (reason or "").strip()
    if text in _POLICY_REASONS:
        return _POLICY_REASONS[text]
    if text and " " not in text and text.upper() == text:
        return ""
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
    parts = [
        _sentence(readable_reason(reason)),
        f"Its plan, token budget and permissions are managed by {_ADMINISTRATORS}.",
    ]
    return TokenWall(
        message=f"{team_name(account_name)} cannot run this tool.",
        action_label=None,
        action_url=None,
        variant=VARIANT_TEAM_BLOCKED,
        detail=" ".join(part for part in parts if part),
    )


def is_generate_refusal(code: str | None, details) -> bool:
    """Whether a refusal is the role saying no to generating.

    The server checks the role before it prices or runs anything, so on a role
    without generating this arrives in place of an estimate. The code alone is
    shared with other permissions (a plugin the account has not enabled, say),
    and a wall naming the role would misdescribe those, so the permission named
    in `details` is what decides.
    """
    return (
        code == "PERMISSION_DENIED"
        and isinstance(details, dict)
        and details.get("required_permission") == GENERATE_PERMISSION
    )


def role_blocked_wall(
    *, account_name: str | None, team_label: str | None = None, is_personal: bool = False
) -> TokenWall:
    """The selected account's role does not allow generating.

    Nothing to buy and nothing to verify, so there is no button. A team's roles
    are its administrators' to change, so they are named, and so is the way out
    that needs nobody: another account under BILLED TO.
    """
    if is_personal:
        # Not something the API does to a personal account today, which carries
        # no role. Said plainly if it ever happens, rather than naming a team.
        return TokenWall(
            message="Your personal account cannot generate.",
            action_label=None,
            action_url=None,
            variant=VARIANT_ROLE_BLOCKED,
            detail="Choose another account under BILLED TO.",
        )
    return TokenWall(
        message=f"{team_name(account_name, team_label)} does not allow your role to generate.",
        action_label=None,
        action_url=None,
        variant=VARIANT_ROLE_BLOCKED,
        detail=f"Ask {_ADMINISTRATORS} to change your role, or choose another account under BILLED TO.",
    )


def role_wall(account, account_name: str | None = None) -> TokenWall | None:
    """The role wall for `account`, read from the account list alone.

    Drawn whenever Generate is held back by a role that is known, so the button
    is never unavailable without saying why. A role that is still loading has
    nothing to name yet: Generate waits for it (see `Account.can_generate`) and
    no wall is drawn over a question that has not been answered.
    """
    if account is None or not account.role_is_known or account.can_generate:
        return None
    return role_blocked_wall(
        account_name=account_name or account.label,
        team_label=account.team_label,
        is_personal=account.is_personal,
    )


def token_policy_wall(
    *, account_name: str | None, team_label: str | None = None, reason: str | None = None
) -> TokenWall:
    """The team's role caps what this person may spend, and the run is over it.

    The server's sentence says which cap (one run, one result, or a period's
    allowance), so it is quoted. One of its reasons arrives as a bare code,
    which is put into words; an unknown code is left out rather than shown.
    No button: a purchase does not lift a limit the role sets.
    """
    parts = [
        _sentence(readable_reason(reason)),
        f"Ask {_ADMINISTRATORS} to change it, or choose another account under BILLED TO.",
    ]
    return TokenWall(
        message=f"{team_name(account_name, team_label)} limits the tokens your role can spend.",
        action_label=None,
        action_url=None,
        variant=VARIANT_TEAM_POLICY,
        detail=" ".join(part for part in parts if part),
    )


def no_tools_wall(
    *, account_name: str | None, team_label: str | None = None, is_personal: bool = False
) -> TokenWall:
    """The role hides every tool this account's catalogue lists.

    Said where the tool and its inputs would be, so the Create tab does not sit
    on "Loading tools..." for good.
    """
    if is_personal:
        return TokenWall(
            message="No tools are available on your personal account.",
            action_label=None,
            action_url=None,
            variant=VARIANT_NO_TOOLS,
            detail="Choose another account under BILLED TO.",
        )
    return TokenWall(
        message=f"{team_name(account_name, team_label)} does not allow your role to use any tools.",
        action_label=None,
        action_url=None,
        variant=VARIANT_NO_TOOLS,
        detail=f"Ask {_ADMINISTRATORS} to change your role, or choose another account under BILLED TO.",
    )


def blocked_tool_message(
    *, tool_name: str, account_name: str | None, team_label: str | None = None, is_personal: bool = False
) -> str:
    """Why a tool the role blocks will not run, or will not open."""
    if is_personal:
        return f"{tool_name} is not available on your personal account."
    return f"{team_name(account_name, team_label)} does not allow your role to use {tool_name}."


def account_gone_message(*, account_name: str | None, team_label: str | None = None) -> str:
    """What to say when the selected team turned out to be gone and runs moved
    to the personal account.

    Nobody chose this, so it has to be said. It names the team that went
    rather than the account now paying, which did nothing to be blamed for.
    """
    return (
        f"{team_name(account_name, team_label)} is no longer available to you. "
        "Runs are now billed to your personal account."
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
    details: dict | None = None,
    message: str | None = None,
) -> TokenWall | None:
    """Map a failed generate onto a wall, or None if this is not a token block.

    Checked reactively, on the 403/402 from POST /generate, as well as
    proactively from preview-cost. Both routes exist because preview-cost is
    advisory and the balance can move between previewing and running.

    Routed on the server's code. `email_verified` is no longer consulted here:
    an unverified email is refused with its own code before anything else, so
    reading the flag the panel holds (which can be stale) only ever sent other
    refusals to the verify page. `message` is the server's sentence, quoted by
    the walls that name a reason.
    """
    if is_generate_refusal(code, details):
        return role_blocked_wall(account_name=account_name, is_personal=is_personal)
    if code == "EMAIL_NOT_VERIFIED":
        return unverified_wall()
    if code == "TOKEN_POLICY" and not is_personal:
        return token_policy_wall(account_name=account_name, reason=message)
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
    It goes through `readable_reason` first, the same as a refused run's: a
    spending limit's reason can be a bare code here too, and an edit after a
    refused run re-prices, which would otherwise bring the code back.
    """
    if can_run:
        return None
    if not email_verified:
        return unverified_wall()
    blocking_reason = readable_reason(blocking_reason) or None
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


def teams_worth_checking(accounts, tool=None) -> list:
    """The caller's teams whose own role would let this run through.

    A team is skipped when its role says no to this plugin or to generating at
    all: it would refuse whatever its plan, so asking it only costs a request.
    Generating needs the same explicit yes Generate itself needs, so a team that
    publishes no answer is not offered either.
    So is one whose role blocks `tool`: it would price the run, and then the
    switch would land on a team that hides the tool it carried.
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
        and account.can_generate
        and not account.blocks_tool(tool)
    ]
