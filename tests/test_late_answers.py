"""Answers that land after the question stopped mattering are not applied.

Every request here is a network round trip, and the person keeps working while
it is in the air: they change account, they sign out, the next person signs
in, a run is refused. An answer that lands after one of those used to be
applied anyway:

- a token refresh wrote the credentials back after sign-out had cleared them;
- a refusal for the person who signed out turned generating off for the next
  person on the same team, and an old refusal overrode a newer reading of the
  role;
- kits fetched for one account landed in the tool browser reopened on another;
- after the selected team turned out to be gone, Library and Uploads went on
  showing what was fetched for it;
- an estimate asked before a run was refused cleared the wall that refusal had
  put up, leaving Generate unavailable with no reason given.

Each answer now carries what it was asked under, and is dropped when that has
moved on. Also here: a spending-limit reason that arrives as a bare code is put
into words on the estimate too, not only on a refused run.
"""

from __future__ import annotations

import asyncio
import time
import types

import pytest

from rundiffusion_omniverse import dialogs as dialogs_module
from rundiffusion_omniverse import panel as panel_module
from rundiffusion_omniverse.api import generate as generate_api
from rundiffusion_omniverse.api import kits as kits_api
from rundiffusion_omniverse.api import session as session_module
from rundiffusion_omniverse.api import token_store
from rundiffusion_omniverse.api import tools as tools_api
from rundiffusion_omniverse.api.kits import Kit, KitTool
from rundiffusion_omniverse.api.session import RdSession
from rundiffusion_omniverse.api.token_wall import VARIANT_ROLE_BLOCKED, VARIANT_TEAM_POLICY
from rundiffusion_omniverse.api.tools import ToolDetail, ToolSummary
from rundiffusion_omniverse.api.transport import ApiError
from rundiffusion_omniverse.panel import RunDiffusionPanel

TOOL = ToolDetail(id="tool-1", name="Flux 2 Dev", tool_fields_hash="h1")

COOLDOWN = "TOKEN_COOLDOWN_ACTIVE"


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


def _accounts(*, team_generate: bool = True, with_team: bool = True) -> dict:
    accounts = [
        {"id": "p1", "label": "Personal account", "kind": "PERSONAL",
         "permissions": {"USE_PLUGINS": True, "GENERATE": True}, "plugin_kinds": ["*"]},
    ]
    if with_team:
        accounts.append(
            {"id": "t1", "label": "alpha Team", "kind": "TEAM", "team_label": "Team",
             "permissions": {"USE_PLUGINS": True, "GENERATE": team_generate},
             "plugin_kinds": ["*"]}
        )
    return {"user": {"email": "a@b.test", "email_verified": True}, "accounts": accounts}


def _session(*, team_generate: bool = True, selected: str | None = "t1") -> RdSession:
    session = RdSession()
    session.refresh_token = "r"
    session.id_token = "id"
    session.expires_at = int(time.time()) + 3600
    session.device_id = "device-1"
    session.adopt_identity(_accounts(team_generate=team_generate))
    session.selected_account_id = selected
    session._persist = lambda: None
    return session


@pytest.fixture
def offline(monkeypatch):
    """No disk, no network: sign-out's revoke and the store are stood in for."""
    saved = []
    monkeypatch.setattr(token_store, "save", saved.append)
    monkeypatch.setattr(token_store, "clear", lambda: None)

    async def answer(_method, url, body=None, headers=None):
        return None

    monkeypatch.setattr(session_module, "request_json_async", answer)
    return saved


def _generate_refusal() -> ApiError:
    return ApiError(
        403, "PERMISSION_DENIED", "Permission GENERATE is required for this action.",
        {"required_permission": "GENERATE"},
    )


# -- a refresh that lands after sign-out ------------------------------------


class TestARefreshAfterSignOut:
    def _slow_refresh(self, monkeypatch, release):
        async def answer(_method, url, body=None, headers=None):
            if url.endswith("/auth/token/refresh"):
                await release.wait()
                return {"id_token": "new-id", "refresh_token": "new-r", "expires_at": 9999999999}
            return None

        monkeypatch.setattr(session_module, "request_json_async", answer)

    def test_it_writes_nothing_back(self, offline, monkeypatch):
        session = _session()
        session._persist = RdSession._persist.__get__(session)

        async def scenario():
            release = asyncio.Event()
            self._slow_refresh(monkeypatch, release)
            refreshing = asyncio.ensure_future(session.refresh())
            await asyncio.sleep(0)
            await session.sign_out()
            release.set()
            with pytest.raises(ApiError):
                await refreshing

        _run(scenario())

        assert session.refresh_token is None
        assert session.id_token is None
        assert session.device_id is None
        assert offline == [], "nothing is persisted for a session that has ended"

    def test_one_that_lands_first_is_taken(self, offline, monkeypatch):
        session = _session()

        async def scenario():
            release = asyncio.Event()
            self._slow_refresh(monkeypatch, release)
            refreshing = asyncio.ensure_future(session.refresh())
            await asyncio.sleep(0)
            release.set()
            await refreshing

        _run(scenario())

        assert session.id_token == "new-id"
        assert session.refresh_token == "new-r"


# -- a refusal from an earlier session or an earlier reading ------------------


class TestARefusalThatIsOutOfDate:
    def test_one_from_before_a_sign_out_is_not_held_for_the_next_person(self, offline):
        session = _session()
        team = session.selected_account
        stamp = session.role_stamp()

        _run(session.sign_out())
        session.refresh_token = "someone-else"
        session.adopt_identity(_accounts(team_generate=True))
        session.selected_account_id = "t1"

        assert session.note_generate_refused(team, stamp) is False
        assert session.selected_account.can_generate

    def test_one_asked_before_the_latest_reading_of_the_role_is_ignored(self):
        session = _session()
        team = session.selected_account
        stamp = session.role_stamp()

        generation = session.begin_accounts_refresh()
        session.finish_accounts_refresh(generation, _accounts(team_generate=True))

        assert session.note_generate_refused(team, stamp) is False
        assert session.selected_account.can_generate

    def test_a_failed_reading_does_not_make_it_old(self):
        """Nothing newer was learned, so the refusal is the newest answer."""
        session = _session()
        team = session.selected_account
        stamp = session.role_stamp()

        generation = session.begin_accounts_refresh()
        session.finish_accounts_refresh(generation, None)

        assert session.note_generate_refused(team, stamp) is True
        assert not session.selected_account.can_generate

    def test_a_current_one_is_held(self):
        session = _session()

        assert session.note_generate_refused(session.selected_account, session.role_stamp())
        assert not session.selected_account.can_generate


# -- the panel ----------------------------------------------------------------


class FakeForm:
    def collect(self):
        return {"prompt": "a red barn"}

    def image_selections(self):
        return {}

    def export_image_state(self):
        return {}


class ImmediateRedraws:
    def request(self, _key, render) -> None:
        render()


def _panel(session: RdSession) -> RunDiffusionPanel:
    panel = object.__new__(RunDiffusionPanel)
    panel._session = session
    panel._tool_detail = TOOL
    panel._tools = [ToolSummary(id=TOOL.id, name=TOOL.name)]
    panel._catalog = list(panel._tools)
    panel._kits = []
    panel._all_kits = []
    panel._selected_tool_id = TOOL.id
    panel._team_answers = {}
    panel._redraws = ImmediateRedraws()
    panel._account_redraws = ImmediateRedraws()
    panel._cost_task = None
    panel._cost_request = None
    panel._estimate_generation = 0
    panel._catalog_generation = 0
    panel._tool_load_generation = 0
    panel._tool_detail_pending = False
    panel._form = FakeForm()
    panel._result_count = 1
    panel._account_carry = None
    panel._generate_button = types.SimpleNamespace(enabled=True, style=None)
    panel._jobs = types.SimpleNamespace(update=lambda *_a, **_k: None)
    panel._current_tab = panel_module.TAB_CREATE
    panel.walls = []
    panel.cost = []
    panel.errors = []
    panel._render_wall = panel.walls.append
    panel._clear_wall = lambda: panel.walls.append(None)
    panel._set_cost = panel.cost.append
    panel._set_error = panel.errors.append
    panel._set_status = lambda _m: None
    return panel


def _no_inputs(monkeypatch):
    monkeypatch.setattr(tools_api, "build_inputs", lambda *a, **k: {})
    monkeypatch.setattr(tools_api, "missing_required_fields", lambda *a, **k: [])


class TestARefusedRunFromASignedOutSession:
    def test_the_next_person_on_the_same_team_keeps_generating(self, offline, monkeypatch):
        session = _session()
        team = session.selected_account
        panel = _panel(session)
        _no_inputs(monkeypatch)

        async def refuse(*_a, **_k):
            # The person signs out and someone else signs in on the same team
            # while the run is in the air.
            await session.sign_out()
            session.refresh_token = "someone-else"
            session.adopt_identity(_accounts(team_generate=True))
            session.selected_account_id = "t1"
            raise _generate_refusal()

        monkeypatch.setattr(generate_api, "submit", refuse)

        with pytest.raises(RuntimeError):
            _run(panel._run_job(object(), TOOL, {}, {}, 1, {}, team))

        assert session.selected_account.can_generate
        assert panel.walls == [], "no wall for a person who never asked"
        assert panel._generate_button.enabled is True


class TestAnEstimateOvertakenByARefusedRun:
    def test_the_role_wall_stays_up(self, monkeypatch):
        """The estimate was asked while the role still allowed generating and
        answers after a run was refused for it."""
        session = _session()
        team = session.selected_account
        panel = _panel(session)
        _no_inputs(monkeypatch)

        async def refuse(*_a, **_k):
            raise _generate_refusal()

        async def priced(*_a, **_k):
            with pytest.raises(RuntimeError):
                await panel._run_job(object(), TOOL, {}, {}, 1, {}, team)
            return {"can_run": True, "blocking_reason": None, "total_tokens": 4}

        monkeypatch.setattr(generate_api, "submit", refuse)
        monkeypatch.setattr(generate_api, "preview_cost", priced)

        _run(panel._preview_cost())

        assert panel.walls[-1].variant == VARIANT_ROLE_BLOCKED
        assert None not in panel.walls
        assert panel.cost == []
        assert panel._generate_button.enabled is False


# -- kits that land late ------------------------------------------------------


class FakeWindow:
    def __init__(self) -> None:
        self.visible = True


def _kit(tool_id: str) -> Kit:
    return Kit(id=tool_id, label=tool_id, tools=(KitTool(tool=ToolSummary(id=tool_id, name=tool_id)),))


class TestKitsThatLandLate:
    def _browser(self, tmp_path):
        browser = dialogs_module.ToolBrowser(session=None, cache_dir=tmp_path)
        browser._window = FakeWindow()
        browser.renders = []
        browser._render = lambda: browser.renders.append(True)
        return browser

    def _slow_kits(self, monkeypatch, release, kits):
        async def list_kits(_session):
            await release.wait()
            return kits

        monkeypatch.setattr(kits_api, "list_kits", list_kits)

    def test_kits_for_a_closed_browser_are_dropped(self, tmp_path, monkeypatch):
        browser = self._browser(tmp_path)

        async def scenario():
            release = asyncio.Event()
            self._slow_kits(monkeypatch, release, [_kit("from-the-old-account")])
            loading = asyncio.ensure_future(browser._load_kits())
            await asyncio.sleep(0)
            browser.close()
            # Reopened on the new account, with its own kits.
            browser._kits = [_kit("from-the-new-account")]
            release.set()
            await loading

        _run(scenario())

        assert [kit.id for kit in browser._kits] == ["from-the-new-account"]
        assert browser.renders == []

    def test_kits_for_the_browser_still_open_are_taken(self, tmp_path, monkeypatch):
        browser = self._browser(tmp_path)

        async def scenario():
            release = asyncio.Event()
            self._slow_kits(monkeypatch, release, [_kit("current")])
            loading = asyncio.ensure_future(browser._load_kits())
            await asyncio.sleep(0)
            release.set()
            await loading

        _run(scenario())

        assert [kit.id for kit in browser._kits] == ["current"]

    def test_a_closed_browser_reports_no_choice(self, tmp_path, monkeypatch):
        browser = self._browser(tmp_path)
        picked = []
        browser._on_pick = picked.append
        monkeypatch.setattr(dialogs_module.prefs, "remember_tool", lambda _tool_id: None)

        browser.close()
        browser._choose(ToolSummary(id="a", name="A"))

        assert picked == []

    def test_the_panels_own_kits_for_an_account_since_left_are_dropped(self, monkeypatch):
        panel = _panel(_session())
        panel._kits = ["the new account's kits"]

        async def list_kits(_session):
            panel._catalog_generation += 1
            return [_kit("from-the-old-account")]

        monkeypatch.setattr(kits_api, "list_kits", list_kits)

        _run(panel._load_kits())

        assert panel._kits == ["the new account's kits"]


# -- a team that is gone ------------------------------------------------------


class TestFallingBackToPersonal:
    """The team was removed on the web, so the re-read lands on Personal. Library
    and Uploads were already asked for the team's pages before it landed."""

    def _rereading(self, payload):
        session = _session()
        generation = session.begin_accounts_refresh()
        panel = _panel(session)

        async def fetch_me():
            return payload

        session.fetch_me = fetch_me
        panel._library_generation = 3
        panel._library_items = ["the team's render"]
        panel._library_cursor = "cursor"
        panel._library_has_more = True
        panel._uploads_generation = 5
        panel._uploads_items = ["the team's upload"]
        panel._uploads_cursor = "cursor"
        panel._uploads_has_more = True
        panel._selected_asset = {"library": "the team's render"}
        panel.rebuilt = []
        panel._rebuild_surface = panel.rebuilt.append
        panel._render_account_bar = lambda: None
        panel._show_tab = lambda _index: None
        panel._tool_browser = types.SimpleNamespace(close=lambda: None, update_tools=lambda _t: None)
        panel.spawned = []
        panel._spawn = panel.spawned.append
        return panel, generation

    def _close_spawned(self, panel):
        for coroutine in panel.spawned:
            coroutine.close()

    def test_library_and_uploads_drop_the_teams_pages_and_load_again(self):
        panel, generation = self._rereading(_accounts(with_team=False))

        _run(panel._reread_accounts_then_load(generation))
        self._close_spawned(panel)

        assert panel._session.selected_account.is_personal
        # Bumped, so a page the team asked for is dropped when it lands.
        assert panel._library_generation > 3
        assert panel._uploads_generation > 5
        assert panel._library_items == [] and panel._uploads_items == []
        assert panel._library_cursor is None and panel._uploads_cursor is None
        assert panel._selected_asset == {}
        assert sorted(panel.rebuilt) == sorted(panel_module.POPPABLE_SURFACES)

    def test_a_team_that_is_still_there_leaves_them_alone(self):
        panel, generation = self._rereading(_accounts(team_generate=True))

        _run(panel._reread_accounts_then_load(generation))
        self._close_spawned(panel)

        assert panel._library_generation == 3
        assert panel._library_items == ["the team's render"]
        assert panel.rebuilt == []


# -- a spending limit given as a code, on an estimate ----------------------------


class TestASpendingLimitOnAnEstimate:
    def test_the_estimate_says_it_in_words(self, monkeypatch):
        panel = _panel(_session())

        async def priced(*_a, **_k):
            return {"can_run": False, "blocking_reason": COOLDOWN, "total_tokens": 4}

        monkeypatch.setattr(generate_api, "preview_cost", priced)

        _run(panel._preview_cost())

        copy = " ".join(part for part in (panel.walls[-1].message, panel.walls[-1].detail) if part)
        assert COOLDOWN not in copy
        assert "Your role's token allowance for this period is used up." in copy

    def test_so_does_a_refused_run(self, monkeypatch):
        session = _session()
        panel = _panel(session)
        _no_inputs(monkeypatch)

        async def refuse(*_a, **_k):
            raise ApiError(403, "TOKEN_POLICY", COOLDOWN)

        monkeypatch.setattr(generate_api, "submit", refuse)

        with pytest.raises(RuntimeError):
            _run(panel._run_job(object(), TOOL, {}, {}, 1, {}, session.selected_account))

        wall = panel.walls[-1]
        assert wall.variant == VARIANT_TEAM_POLICY
        assert COOLDOWN not in f"{wall.message} {wall.detail}"
