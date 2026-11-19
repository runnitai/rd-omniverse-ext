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
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Callable

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
    #: What this account's company calls a team ("Team" unless it renamed the
    #: concept, to "Studio" say). Copy that names a team without naming it uses
    #: this rather than the word, which would read wrong to that company.
    team_label: str | None = None
    #: The role's restriction settings, published beside the permission map.
    #: Only `restrict_tools_settings` is read here, for `blocks_tool`.
    permission_settings: dict = field(default_factory=dict)

    @property
    def is_personal(self) -> bool:
        return (self.kind or "").upper() == "PERSONAL"

    @property
    def role_is_known(self) -> bool:
        """Whether this account's permission map has arrived.

        The API publishes a full map for every account, personal ones
        included, so an empty one means it has not been read yet rather than
        that the role grants nothing.
        """
        return bool(self.permissions)

    @property
    def can_generate(self) -> bool:
        """Whether this account's role lets the caller start a run.

        An explicit yes is required, so a role that is still loading is a no:
        Generate waits for the role rather than finding out from a refusal. The
        server enforces this regardless; reading it here is what lets the panel
        say who can change it before anything is sent.
        """
        return self.permissions.get("GENERATE") is True

    def blocks_tool(self, tool) -> bool:
        """Whether this account's role hides `tool` from the person holding it.

        A role blocks tools two ways: by listing their ids, and by listing tags,
        which blocks every tool carrying one of them. Nothing else is consulted.

        Only while BLOCK_TOOLS is on and its settings are present. The server
        already sends the settings only for an active restriction; checking
        again means a stale payload can never hide a tool the role allows.
        """
        if tool is None or self.permissions.get("BLOCK_TOOLS") is not True:
            return False
        settings = self.permission_settings.get("restrict_tools_settings")
        if not isinstance(settings, dict):
            return False
        tool_id = getattr(tool, "id", None)
        blocked_ids = settings.get("tool_ids")
        if tool_id and isinstance(blocked_ids, list) and tool_id in blocked_ids:
            return True
        blocked_tags = settings.get("tool_tags")
        if not isinstance(blocked_tags, dict):
            return False
        return any(blocked_tags.get(tag) is True for tag in getattr(tool, "tool_tag_ids", ()))

    @property
    def allows_this_plugin(self) -> bool:
        """Whether this account may act as the Omniverse plugin.

        Two gates, and BOTH have to pass: USE_PLUGINS says whether plugins work
        at all, and plugin_kinds says which ones. An account can list its kinds
        explicitly rather than carrying the wildcard, so an unlisted kind is
        denied even when plugins are otherwise on. The server enforces this
        either way; reading it here is what lets the panel explain itself
        instead of failing opaquely.

        Read the way the server reads it. A personal account has no role to
        consult, so the server never gates it. A team account needs an explicit
        USE_PLUGINS and a list naming this kind (or the wildcard): the server
        denies one that publishes neither, so the panel does too, rather than
        offering a team the first request would refuse.
        """
        if self.is_personal:
            return True
        if self.permissions.get("USE_PLUGINS") is not True:
            return False
        kinds = self.plugin_kinds
        return PLUGIN_KIND_WILDCARD in kinds or PLUGIN_KIND in kinds


@dataclass(frozen=True)
class RoleStamp:
    """Which sign-in, and which reading of the roles, a request was sent under."""

    session: int
    reading: int


def _settings(raw) -> dict:
    """An account's restriction settings, or an empty map for anything else.

    A personal account publishes null here, and `blocks_tool` reads the map
    with `.get`, so whatever is not a map is read as no settings at all.
    """
    return raw if isinstance(raw, dict) else {}


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
    #: Whether the account list is being read again. A role read before that
    #: answer lands may be out of date, so Generate waits for it.
    refreshing_accounts: bool = False
    #: Which re-read is the newest, so an older answer landing late is dropped.
    _accounts_generation: int = field(default=0, repr=False)
    #: Which sign-in this is. Moved on by every sign-out, so an answer to a
    #: request made before it (a token refresh, a refused run) is not applied
    #: to the next person, or to nobody.
    session_epoch: int = 0
    #: How many times the account list, and the roles in it, has been read.
    #: A refusal asked before the latest reading is older than what it says.
    _accounts_read: int = field(default=0, repr=False)
    #: Told when the server says the selected team is no longer the caller's
    #: to use, with that team and the number of the re-read of the account
    #: list this starts. Whoever draws the accounts sets it, and has to finish
    #: that re-read: Generate waits until it does.
    on_account_gone: Callable[[Account, int], None] | None = field(default=None, repr=False)
    #: Held while the id token is being refreshed, so the calls that find it
    #: expired at the same moment share one refresh. See `ensure_valid_token`.
    _refresh_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False, compare=False
    )

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

    def account_params(self, account: Account | None = None) -> dict[str, str]:
        """Account selection as v2 spells it, for `account` or the selected one.

        A query parameter, NOT the X-Account-Id header v1 used, and OMITTED
        rather than sent empty for a personal account: the account follows this
        parameter alone.

        `account` asks about one that is not selected, which is how the panel
        finds out whether a team could run what the personal account cannot
        without switching to it first.
        """
        account = account if account is not None else self.selected_account
        if account is not None and not account.is_personal and account.id:
            return {"team_id": account.id}
        return {}

    @contextmanager
    def account_request(self, account: Account | None = None):
        """Around one request made on `account`, or on the selected account.

        The server checks the account on every request, so any of them can be
        the first to find that a team is gone: the person was removed from it,
        or it was deleted, while the panel was open. The account list is
        otherwise read only at sign-in and on an account change, so until one
        of those the panel would go on asking a team that refuses everything.
        Every request that names an account goes out inside this, and one
        refused that way asks for the list to be read again (see
        `on_account_gone`), which is what moves the selection to personal.

        The refusal still reaches the caller, which reports it as it does any
        other failure. /me names no account, so it is never made inside this
        and cannot ask for itself.
        """
        account = account if account is not None else self.selected_account
        epoch = self.session_epoch
        try:
            yield
        except ApiError as error:
            if error.is_account_gone:
                self._note_account_gone(account, epoch)
            raise

    def _note_account_gone(self, account: Account | None, epoch: int) -> None:
        """A request sent on `account` during sign-in number `epoch` was
        refused because that team is no longer the caller's to use.

        Acted on only while it is still about what the panel shows:

        - not after a sign-out, when it answers for someone who is no longer
          here and the next person may hold the same team;
        - not for an account that is no longer selected, such as the status of
          a run started before a switch, or a price asked of another team;
        - not while the list is already being read again, so refusals that
          land together read it once. A read that fails leaves the selection
          where it was, and the next refusal asks again.
        """
        if epoch != self.session_epoch:
            return
        if account is None or account.is_personal or account.id != self.selected_account_id:
            return
        if self.refreshing_accounts or self.on_account_gone is None:
            return
        self.on_account_gone(account, self.begin_accounts_refresh())

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
        self._accounts_read += 1
        self.user = payload.get("user") or {}
        self.accounts = [
            Account(
                id=account.get("id"),
                label=account.get("label"),
                kind=account.get("kind"),
                permissions=account.get("permissions") or {},
                plugin_kinds=account.get("plugin_kinds") or [],
                team_label=account.get("team_label"),
                permission_settings=_settings(account.get("permission_settings")),
            )
            for account in payload.get("accounts") or []
        ]

    def begin_accounts_refresh(self) -> int:
        """Start reading the account list again. Returns the re-read's number.

        The roles in it come from /me, which is otherwise read only at sign-in,
        so a role an administrator changed on the web would be invisible until
        a restart. Until the answer lands the held role is not trusted.
        """
        self._accounts_generation += 1
        self.refreshing_accounts = True
        return self._accounts_generation

    def finish_accounts_refresh(self, generation: int, payload: dict | None) -> bool:
        """Take the answer to re-read number `generation`. False if overtaken.

        `payload` is the /me response, or None when it could not be read, in
        which case the list already held stays: the server is still the
        authority, and a failed refresh is no reason to stop the person working.
        The selected account is kept by id, and falls back to personal only if
        it is no longer in the list.
        """
        if generation != self._accounts_generation:
            return False
        self.refreshing_accounts = False
        if payload is not None:
            selected = self.selected_account_id
            self.adopt_identity(payload)
            self.adopt_stored_account(selected)
        return True

    def role_stamp(self) -> RoleStamp:
        """When a request that can answer about the role is sent. Taken before
        it goes out and handed back with its answer to `note_generate_refused`."""
        return RoleStamp(session=self.session_epoch, reading=self._accounts_read)

    def same_session(self, stamp: RoleStamp) -> bool:
        """Whether `stamp` was taken during this sign-in."""
        return stamp.session == self.session_epoch

    def note_generate_refused(self, account: Account | None, stamp: RoleStamp) -> bool:
        """The server refused a run on `account` because its role does not
        allow generating, though the account list said it did. True if held.

        The server's answer is newer than the list, so it is held as the role
        until the list is read again: otherwise Generate goes on offering a run
        that is refused every time. Replaced rather than edited in place, so
        nothing holding the old object has it change underneath it.

        Not held when `stamp`, taken as the request was sent, is out of date:
        a sign-out since means it answers for someone else (the next person may
        hold the same team with a role that allows it), and a reading of the
        roles since is newer than the request was.
        """
        if stamp != self.role_stamp():
            return False
        for index, held in enumerate(self.accounts):
            same = held.is_personal if account is None else held.id == account.id
            if same:
                self.accounts[index] = replace(
                    held, permissions={**held.permissions, "GENERATE": False}
                )
        return True

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

        epoch = self.session_epoch
        payload = await request_json_async(
            "POST",
            f"{api_base_url()}/auth/token/refresh",
            body={"refresh_token": self.refresh_token},
        )
        if epoch != self.session_epoch:
            # Signed out while the exchange was in the air. Taking its answer
            # would put the credentials back, and save them to disk, for a
            # session the person just ended.
            raise ApiError(status=401, message="Not signed in.")
        self.id_token = payload["id_token"]
        # The service may rotate the refresh token, and echoes it either way, so
        # storing what comes back means never having to detect which happened.
        self.refresh_token = payload.get("refresh_token") or self.refresh_token
        self.expires_at = int(payload.get("expires_at") or 0)
        self._persist()

    async def ensure_valid_token(self) -> str:
        """The id token, refreshed first if it is about to expire.

        One refresh at a time. Every request the panel has going when the token
        runs out (an estimate, a page of the library, the poll of a run) arrives
        here together, and without the lock each of them would exchange the
        same refresh token for a new one of its own. The first refreshes; the
        rest wait for it, look again, and find the token fresh. If that refresh
        failed, the next one in line tries once more for itself.
        """
        if not self.refresh_token:
            raise ApiError(status=401, message="Not signed in.")
        if self._token_needs_refresh():
            async with self._refresh_lock:
                # Looked at again now that it is this call's turn: whoever held
                # the lock may already have done the refresh.
                if self._token_needs_refresh():
                    await self.refresh()
        return self.id_token or ""

    def _token_needs_refresh(self) -> bool:
        return not self.id_token or time.time() >= (
            self.expires_at - TOKEN_REFRESH_SKEW_SECONDS
        )

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
            # A re-read still in the air answers for the person signing out.
            self.refreshing_accounts = False
            self._accounts_generation += 1
            # So does everything else still in the air: a token refresh, a
            # refused run. See `session_epoch`.
            self.session_epoch += 1
            token_store.clear()
