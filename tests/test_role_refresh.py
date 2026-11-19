"""A role changed on the web reaches the panel without a restart.

The account list, and every role in it, used to be read once, at sign-in. A
role an administrator changed afterwards was invisible until Kit restarted, and
a refusal the panel had learned to trust kept Generate unavailable even after
the role allowed it again.

So the list is read again on every account change, before that account's
tools and kits are fetched, which means they are filtered by the role as it is
now. Generate waits for the answer. The account chosen and a form carried
across the switch stay as they are while it loads.
"""

from __future__ import annotations

import asyncio
import types

from rundiffusion_omniverse.api import token_store
from rundiffusion_omniverse.api.session import Account, RdSession
from rundiffusion_omniverse.api.token_wall import VARIANT_ROLE_BLOCKED
from rundiffusion_omniverse.api.transport import ApiError
from rundiffusion_omniverse.panel import RunDiffusionPanel, _AccountCarry


def _run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


def _account_payload(team_generate: bool = True, *, with_team: bool = True, team_label="alpha Team") -> dict:
    accounts = [
        {"id": "p1", "label": "Personal account", "kind": "PERSONAL",
         "permissions": {"USE_PLUGINS": True, "GENERATE": True}, "plugin_kinds": ["*"]},
    ]
    if with_team:
        accounts.append(
            {"id": "t1", "label": team_label, "kind": "TEAM", "team_label": "Team",
             "permissions": {"USE_PLUGINS": True, "GENERATE": team_generate},
             "plugin_kinds": ["*"]}
        )
    return {"user": {"email": "a@b.test", "email_verified": True}, "accounts": accounts}


def _session(team_generate: bool = True, selected: str | None = "t1") -> RdSession:
    session = RdSession()
    session.refresh_token = "r"
    session.adopt_identity(_account_payload(team_generate))
    session.selected_account_id = selected
    session._persist = lambda: None
    return session


class TestTheSessionRereadsItsAccounts:
    def test_a_reread_takes_the_new_role_and_keeps_the_selection(self):
        session = _session(team_generate=False)

        generation = session.begin_accounts_refresh()
        assert session.refreshing_accounts

        assert session.finish_accounts_refresh(generation, _account_payload(team_generate=True))
        assert not session.refreshing_accounts
        assert session.selected_account_id == "t1"
        assert session.selected_account.can_generate

    def test_an_answer_to_an_older_question_is_dropped(self):
        session = _session(team_generate=False)
        older = session.begin_accounts_refresh()
        session.begin_accounts_refresh()

        assert not session.finish_accounts_refresh(older, _account_payload(team_generate=True))
        assert session.refreshing_accounts
        assert not session.selected_account.can_generate

    def test_a_failed_reread_keeps_the_role_already_held(self):
        session = _session(team_generate=False)
        generation = session.begin_accounts_refresh()

        assert session.finish_accounts_refresh(generation, None)
        assert not session.refreshing_accounts
        assert session.selected_account.id == "t1"
        assert not session.selected_account.can_generate

    def test_an_account_that_is_gone_falls_back_to_personal(self, monkeypatch):
        """Removed from the team on the web since sign-in."""
        session = _session()
        saved = []
        session._persist = lambda: saved.append(session.selected_account_id)
        generation = session.begin_accounts_refresh()

        session.finish_accounts_refresh(generation, _account_payload(with_team=False))

        assert session.selected_account_id is None
        assert session.selected_account.is_personal
        assert saved == [None]

    def test_signing_out_ends_a_reread(self, monkeypatch):
        monkeypatch.setattr(token_store, "clear", lambda: None)
        session = _session()
        session.begin_accounts_refresh()

        _run(session.sign_out())

        assert not session.refreshing_accounts


class Browser:
    def __init__(self) -> None:
        self.closed = 0

    def close(self):
        self.closed += 1

    def update_tools(self, _tools):
        return None


class BookedRedraws:
    def __init__(self) -> None:
        self.booked: list = []

    def request(self, key, render) -> None:
        self.booked.append((key, render))


class FakeCombo:
    def __init__(self, index: int) -> None:
        self.index = index
        self.model = self

    def get_item_value_model(self):
        return self

    @property
    def as_int(self):
        return self.index


def _panel(session: RdSession) -> RunDiffusionPanel:
    panel = object.__new__(RunDiffusionPanel)
    panel._session = session
    panel._account_combo = FakeCombo(1)
    panel._current_tab = 0
    panel._tools = []
    panel._catalog = []
    panel._kits = []
    panel._all_kits = []
    panel._tool_tags = {}
    panel._selected_tool_id = None
    panel._tool_detail = None
    panel._account_carry = None
    panel._cost_task = None
    panel._cost_request = None
    panel._team_answers = {}
    panel._catalog_generation = 0
    panel._tool_load_generation = 0
    panel._tool_detail_pending = False
    panel._generate_button = types.SimpleNamespace(enabled=True, style=None)
    panel._tool_browser = Browser()
    panel._account_redraws = BookedRedraws()
    panel._forget_browsing = lambda: None
    panel.spawned = []
    panel._spawn = panel.spawned.append
    panel.walls = []
    panel.errors = []
    panel.account_bars = []
    panel._render_wall = panel.walls.append
    panel._clear_wall = lambda: panel.walls.append(None)
    panel._set_cost = lambda _message: None
    panel._set_error = panel.errors.append
    panel._render_account_bar = lambda: panel.account_bars.append(True)
    panel._show_tab = lambda _index: None
    panel._rebuild_surface = lambda _surface: None
    return panel


def _names(coroutines) -> list:
    names = [c.cr_code.co_name for c in coroutines]
    for coroutine in coroutines:
        coroutine.close()
    return names


class TestAnAccountChangeRereadsTheRole:
    def test_generate_waits_from_the_moment_the_account_changes(self):
        session = _session(selected=None)
        panel = _panel(session)

        panel._on_account_changed()

        assert session.selected_account_id == "t1"
        assert session.refreshing_accounts
        assert not panel._generate_allowed()

    def test_a_click_before_the_role_lands_starts_nothing(self):
        session = _session()
        session.begin_accounts_refresh()
        panel = _panel(session)
        panel._jobs = types.SimpleNamespace(submit=lambda *a, **k: (_ for _ in ()).throw(AssertionError("submitted")))

        panel._on_generate_clicked()

        assert "loading" in panel.errors[-1].lower()

    def test_the_redraw_rereads_before_it_fetches_anything(self):
        panel = _panel(_session())
        panel._session.begin_accounts_refresh()

        panel._redraw_for_account()

        assert _names(panel.spawned) == ["_reread_accounts_then_load"]


class TestTheRereadLands:
    def _rereading(self, payload_or_error, *, team_generate=False):
        session = _session(team_generate=team_generate)
        generation = session.begin_accounts_refresh()
        panel = _panel(session)

        async def fetch_me():
            if isinstance(payload_or_error, Exception):
                raise payload_or_error
            return payload_or_error

        session.fetch_me = fetch_me
        return panel, generation

    def test_a_role_allowed_again_takes_the_wall_down_and_frees_generate(self):
        panel, generation = self._rereading(_account_payload(team_generate=True))

        _run(panel._reread_accounts_then_load(generation))

        assert panel._session.selected_account.can_generate
        assert panel.walls[-1] is None
        assert panel._generate_button.enabled is True
        assert sorted(_names(panel.spawned)) == ["_load_kits", "_load_tool_tags", "_load_tools"]

    def test_a_role_turned_off_since_sign_in_is_named(self):
        panel, generation = self._rereading(_account_payload(team_generate=False), team_generate=True)

        _run(panel._reread_accounts_then_load(generation))

        assert panel.walls[-1].variant == VARIANT_ROLE_BLOCKED
        assert panel._generate_button.enabled is False
        _names(panel.spawned)

    def test_a_failed_reread_goes_on_with_the_role_already_held(self):
        panel, generation = self._rereading(ApiError(0, "UNREACHABLE", "offline"))

        _run(panel._reread_accounts_then_load(generation))

        assert not panel._session.refreshing_accounts
        assert panel.walls[-1].variant == VARIANT_ROLE_BLOCKED
        assert sorted(_names(panel.spawned)) == ["_load_kits", "_load_tool_tags", "_load_tools"]

    def test_an_answer_overtaken_by_another_account_change_does_nothing(self):
        panel, generation = self._rereading(_account_payload(team_generate=True))
        panel._session.begin_accounts_refresh()

        _run(panel._reread_accounts_then_load(generation))

        assert panel.spawned == []
        assert panel.walls == []
        assert panel._session.refreshing_accounts

    def test_the_selection_and_a_carried_form_stay_put(self):
        panel, generation = self._rereading(_account_payload(team_generate=True))
        carry = _AccountCarry(account_id="t1", tool_id="tool-1", values={"prompt": "barn"})
        panel._account_carry = carry
        panel._selected_tool_id = "tool-1"

        _run(panel._reread_accounts_then_load(generation))

        assert panel._session.selected_account_id == "t1"
        assert panel._account_carry is carry
        assert panel._selected_tool_id == "tool-1"
        _names(panel.spawned)

    def test_billed_to_is_redrawn_only_when_the_list_changed(self):
        panel, generation = self._rereading(_account_payload(team_generate=True))

        _run(panel._reread_accounts_then_load(generation))

        assert panel.account_bars == []
        _names(panel.spawned)

    def test_a_renamed_team_redraws_billed_to(self):
        panel, generation = self._rereading(_account_payload(team_generate=True, team_label="Alpha Studio"))

        _run(panel._reread_accounts_then_load(generation))

        assert panel.account_bars == [True]
        _names(panel.spawned)

    def test_a_team_that_is_gone_lands_on_personal(self):
        panel, generation = self._rereading(_account_payload(with_team=False))
        panel._session._persist = lambda: None

        _run(panel._reread_accounts_then_load(generation))

        assert panel._session.selected_account.is_personal
        assert panel.account_bars == [True]
        _names(panel.spawned)


def test_sign_in_does_not_reread():
    """The list it just received is already current."""
    session = _session()
    panel = _panel(session)
    panel._render_root = lambda: None
    panel._sign_in_task = None
    panel._device_prompt = None

    panel._render_signed_in()

    assert "_reread_accounts_then_load" not in _names(panel.spawned)
    assert isinstance(session.selected_account, Account)
