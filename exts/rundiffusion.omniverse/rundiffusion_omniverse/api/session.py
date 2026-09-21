"""Sign-in and the authenticated call surface.

The device-code flow. Its contract was read off the v2 auth endpoints rather
than assumed, because two details differ from what a reader would guess:

  - the poll is a **GET** with `?device_code=`, not a POST with a body
  - `expires_at` is Unix epoch **seconds**, not a duration

v2 also proxies the refresh exchange at POST /auth/token/refresh, so this plugin
never talks to the identity provider. That is what lets a Kit extension carry no
auth SDK at all, and it is the single largest thing v2 buys us here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from . import token_store
from .constants import PLUGIN_KIND, PLUGIN_KIND_WILDCARD, api_base_url
from .transport import ApiError, request_json_async, with_query

logger = logging.getLogger(__name__)

# Refresh a little before the token actually dies, so a long generate does not
# start with a credential that expires mid-flight.
TOKEN_REFRESH_SKEW_SECONDS = 120

#: The two ways in, which are one flow with different landings.
INTENT_SIGN_IN = "sign_in"
INTENT_SIGN_UP = "sign_up"


@dataclass
class DeviceCodePrompt:
    """What the user needs in order to authorize this device."""

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


def verification_url_for_intent(verification_url: str, intent: str) -> str:
    """The URL to open for a sign-IN versus a sign-UP.

    Both buttons run the same device flow. The difference is one query
    parameter: `intent=signup` lands a brand-new user in the web sign-up form
    instead of a login wall. Anything other than the exact token is treated as
    no intent, so the default is a normal sign-in.
    """
    if intent != INTENT_SIGN_UP:
        return verification_url
    separator = "&" if "?" in verification_url else "?"
    return f"{verification_url}{separator}intent=signup"


@dataclass
class Account:
    """One selectable account, personal or team.

    Field names follow what the API sends: `label` and `kind`, not `name` and
    `is_personal`. An earlier draft invented the latter pair and silently got
    empty values for both, which is the failure mode of guessing a payload
    instead of reading it.
    """

    id: str | None
    label: str | None
    kind: str | None
    #: The resolved permission map for this account, as /me publishes it.
    permissions: dict = field(default_factory=dict)
    #: Which plugin kinds this account may act as. The wildcard "*" grants any
    #: kind INCLUDING ones added later, which is how an account can work with a
    #: plugin released after it was set up.
    plugin_kinds: list = field(default_factory=list)

    @property
    def is_personal(self) -> bool:
        return (self.kind or "").upper() == "PERSONAL"

    @property
    def allows_this_plugin(self) -> bool:
        """Whether this account may act as the Omniverse plugin.

        Two gates, and BOTH have to pass: USE_PLUGINS says whether plugins work
        at all, and plugin_kinds says which ones. An account can list its kinds
        explicitly rather than carrying the wildcard, so an unlisted kind is
        denied even when plugins are otherwise on. The server enforces this
        either way; reading it here is what lets the panel explain itself
        instead of failing opaquely.
        """
        if not self.permissions.get("USE_PLUGINS", True):
            return False
        kinds = self.plugin_kinds
        if not kinds:
            # No list published. The server is still the authority, so this
            # stays permissive and lets the call be the one that refuses.
            return True
        return PLUGIN_KIND_WILDCARD in kinds or PLUGIN_KIND in kinds


@dataclass
class RdSession:
    """Holds the signed-in state for the life of the Kit session."""

    id_token: str | None = None
    refresh_token: str | None = None
    expires_at: int = 0
    device_id: str | None = None
    user: dict = field(default_factory=dict)
    accounts: list[Account] = field(default_factory=list)
    #: Which account a run is billed to. None means the personal account, which
    #: is also what the API means by omitting `team_id` entirely. Persisted, so
    #: the choice outlives the Kit session that made it.
    selected_account_id: str | None = None

    @property
    def is_signed_in(self) -> bool:
        return bool(self.refresh_token)

    @property
    def display_name(self) -> str | None:
        """How to name the signed-in person, or None if we do not know yet.

        `/me` publishes `email` and `uid`; there is NO `display_name` field, and
        an earlier draft looked for one and fell back to the literal string
        "Signed in", which rendered as "Signed in as Signed in". Returning None
        when the identity is genuinely unknown lets the caller say something
        true instead of something absurd.
        """
        return self.user.get("email") or self.user.get("uid") or None

    @property
    def selected_account(self) -> Account | None:
        for account in self.accounts:
            if account.id == self.selected_account_id:
                return account
        return next((a for a in self.accounts if a.is_personal), None)

    @property
    def email_verified(self) -> bool:
        """Unverified callers hit a different wall with a free fix."""
        return bool(self.user.get("email_verified"))

    def account_params(self) -> dict[str, str]:
        """Account selection as v2 spells it.

        A query parameter, NOT the X-Account-Id header v1 used, and OMITTED
        rather than sent empty for a personal account: the account follows this
        parameter alone.
        """
        account = self.selected_account
        if account is not None and not account.is_personal and account.id:
            return {"team_id": account.id}
        return {}

    # -- sign-in -----------------------------------------------------------

    async def start_device_flow(self) -> DeviceCodePrompt:
        """Ask the server for a code pair. No auth required.

        `plugin_kind` is checked against the kinds the API knows about, so a
        kind it does not recognise fails here rather than later.
        """
        payload = await request_json_async(
            "POST",
            f"{api_base_url()}/auth/device/start",
            body={"plugin_kind": PLUGIN_KIND},
        )
        return DeviceCodePrompt(
            device_code=payload["device_code"],
            user_code=payload["user_code"],
            verification_url=payload["verification_url"],
            expires_in=int(payload.get("expires_in") or 600),
            interval=int(payload.get("interval") or 5),
        )

    async def await_device_authorization(self, prompt: DeviceCodePrompt) -> None:
        """Poll until the user authorizes in the browser, then adopt the session.

        Three server answers drive the loop: 428 means keep waiting, SLOW_DOWN
        means the interval was too aggressive and must be doubled (the server
        says so explicitly, so honour it rather than hard-coding a backoff), and
        410 means the code died and the user has to start again.
        """
        interval = prompt.interval
        deadline = time.monotonic() + prompt.expires_in

        while True:
            if time.monotonic() > deadline:
                raise ApiError(
                    status=410,
                    code="DEVICE_CODE_EXPIRED",
                    message="The sign-in code expired. Start again.",
                )

            await asyncio.sleep(interval)

            try:
                payload = await request_json_async(
                    "GET",
                    with_query(
                        f"{api_base_url()}/auth/device/poll",
                        {"device_code": prompt.device_code},
                    ),
                )
            except ApiError as error:
                if error.is_authorization_pending:
                    continue
                if error.is_slow_down:
                    interval *= 2
                    continue
                raise

            self._adopt(payload)
            return

    def _adopt(self, payload: dict) -> None:
        tokens = payload.get("tokens") or {}
        self.id_token = tokens.get("id_token")
        self.refresh_token = tokens.get("refresh_token")
        self.expires_at = int(tokens.get("expires_at") or 0)
        self.device_id = payload.get("device_id")
        self.adopt_identity(payload)
        self._persist()

    def adopt_identity(self, payload: dict) -> None:
        """Take `user` and `accounts` from a device poll or a /me response.

        Both endpoints publish the same two shapes, which is why one method
        serves both and why a restored session can be filled in from /me alone.
        """
        self.user = payload.get("user") or {}
        self.accounts = [
            Account(
                id=account.get("id"),
                label=account.get("label"),
                kind=account.get("kind"),
                permissions=account.get("permissions") or {},
                plugin_kinds=account.get("plugin_kinds") or [],
            )
            for account in payload.get("accounts") or []
        ]

    def choose_account(self, account_id: str | None) -> None:
        """Bill runs to `account_id` from now on, and remember it.

        Persisted at the moment of choosing rather than at shutdown: a DCC app
        is closed by being closed, and a preference saved on the way out is a
        preference lost whenever Kit goes down badly.
        """
        self.selected_account_id = account_id
        self._persist()

    def adopt_stored_account(self, account_id: str | None) -> None:
        """Take a remembered account back, but only if it still exists.

        An id can outlive the membership that made it meaningful: someone is
        removed from a team, or the team is deleted. Falling back to personal is
        already what `selected_account` does with an unknown id, so this drops
        the dead id instead of leaving it on disk to be rewritten forever.
        """
        if account_id is None:
            return
        if any(account.id == account_id for account in self.accounts):
            self.selected_account_id = account_id
            return
        logger.info(
            "RunDiffusion: the remembered account is no longer available; "
            "billing the personal account."
        )
        self.selected_account_id = None
        self._persist()

    def _persist(self) -> None:
        if not self.refresh_token:
            return
        try:
            token_store.save(
                token_store.StoredSession(
                    refresh_token=self.refresh_token,
                    device_id=self.device_id,
                    selected_account_id=self.selected_account_id,
                )
            )
        except token_store.SecureStoreUnavailable:
            # Sign-in still worked for this Kit session; it just will not
            # survive a restart. Worth logging and worth not failing over.
            logger.exception("RunDiffusion: could not persist the session.")

    # -- restore and refresh -----------------------------------------------

    async def restore(self) -> bool:
        """Bring back a previous sign-in, if one is on disk. True if signed in.

        Raises SecureStoreUnavailable rather than returning False when the store
        exists but cannot be read, so the panel can say so instead of showing a
        sign-in screen that implies the session is gone.
        """
        stored = token_store.load()
        if stored is None:
            return False

        self.refresh_token = stored.refresh_token
        self.device_id = stored.device_id
        # Set BEFORE the refresh, which rewrites this file when the server
        # rotates the token. Restoring the account afterwards would mean
        # persisting None over the remembered choice first and then putting it
        # back, and losing it for good if anything failed in between.
        self.selected_account_id = stored.selected_account_id
        await self.refresh()

        # The disk holds credentials, not identity: `user` and `accounts` came
        # from the device poll and were never persisted. Without this call a
        # restored session is authenticated but anonymous, and the panel has
        # nothing true to display.
        self.adopt_identity(await self.fetch_me())
        # Only now can the remembered account be checked against the accounts
        # this person actually has.
        self.adopt_stored_account(stored.selected_account_id)
        return True

    async def refresh(self) -> None:
        """Exchange the refresh token for a fresh id token, server-side."""
        if not self.refresh_token:
            raise ApiError(status=401, message="Not signed in.")

        payload = await request_json_async(
            "POST",
            f"{api_base_url()}/auth/token/refresh",
            body={"refresh_token": self.refresh_token},
        )
        self.id_token = payload["id_token"]
        # The service may rotate the refresh token, and echoes it either way, so
        # storing what comes back means never having to detect which happened.
        self.refresh_token = payload.get("refresh_token") or self.refresh_token
        self.expires_at = int(payload.get("expires_at") or 0)
        self._persist()

    async def ensure_valid_token(self) -> str:
        if not self.refresh_token:
            raise ApiError(status=401, message="Not signed in.")
        if not self.id_token or time.time() >= (self.expires_at - TOKEN_REFRESH_SKEW_SECONDS):
            await self.refresh()
        return self.id_token or ""

    async def auth_headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {await self.ensure_valid_token()}"}
        if self.device_id:
            headers["X-Device-Id"] = self.device_id
        return headers

    # -- identity ----------------------------------------------------------

    async def fetch_me(self) -> dict:
        """`/me` is exempt from the plugin gate, so it answers even for a team
        whose role denies plugins. That is what lets the panel explain itself
        instead of failing opaquely."""
        return await request_json_async(
            "GET", f"{api_base_url()}/me", headers=await self.auth_headers()
        )

    # -- sign-out ----------------------------------------------------------

    async def sign_out(self) -> None:
        """Revoke this device server-side, then forget it locally.

        Local state is cleared even when the revoke call fails: the user asked
        to be signed out, and leaving a sealed refresh token on disk because the
        network was down is the wrong way to disagree with them.
        """
        try:
            if self.refresh_token and self.device_id:
                await request_json_async(
                    "POST",
                    f"{api_base_url()}/auth/device/revoke",
                    headers=await self.auth_headers(),
                )
        except Exception:  # noqa: BLE001 - best effort, never blocks sign-out
            logger.exception("RunDiffusion: device revoke failed; clearing locally anyway.")
        finally:
            self.id_token = None
            self.refresh_token = None
            self.expires_at = 0
            self.device_id = None
            self.user = {}
            self.accounts = []
            # The remembered account goes with the session that meant it. The
            # next person to sign in on this machine must not inherit a team.
            self.selected_account_id = None
            token_store.clear()
